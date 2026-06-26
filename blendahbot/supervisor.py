"""Keep Blender alive across the build: detect a crash or hang and bring it back.

The build loop talks to Blender both out-of-band (:class:`~blendahbot.blender.BlenderClient`)
and through the MCP bridge, and *both* open a fresh socket per call — so once a
replacement Blender is listening on the same port, the agent and the orchestrator
reconnect transparently with no need to restart the MCP server or the persistent
``ClaudeSDKClient``. This module owns the *process*: it watches health and, on a

* **crash** (the process exited, or the port refuses connections), or
* **hang** (the main thread froze: the socket accepts but never replies),

kills the stale instance and relaunches Blender, reopening the latest checkpoint
``.blend`` so the in-progress build resumes from where it was rather than from an
empty scene.

Health is only checked when Blender is expected to be idle (between rounds), so a
legitimately busy main thread — a long render, a heavy modifier eval — is never
mistaken for a hang.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

from .blender import BlenderClient, launch_blender
from .config import BotConfig
from .discovery import DiscoveryError, find_blender_executable
from .ui import Console


# A listening TCP socket has a wildcard FOREIGN address. This is locale-independent,
# unlike the State column ("LISTENING"), which netstat localizes ("ABHÖREN", "À L'ÉCOUTE").
_WILDCARD_FOREIGN = {"0.0.0.0:0", "[::]:0", "*:*", "0.0.0.0:*", "[::]:*"}


def _parse_netstat_pids(output: str, port: int) -> set[int]:
    """Pull PIDs of sockets *listening* on ``port`` from ``netstat -ano`` output."""
    pids: set[int] = set()
    needle = f":{port}"
    for line in output.splitlines():
        parts = line.split()
        # Expected columns:  TCP  <local>  <foreign>  <state>  <pid>
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if not parts[1].endswith(needle):
            continue
        # Identify the listening server socket (not a transient client connection) by
        # its wildcard foreign address — robust to localized/space-containing State text.
        if parts[2] not in _WILDCARD_FOREIGN and parts[3].upper() != "LISTENING":
            continue
        pid = parts[-1]
        if pid.isdigit():
            pids.add(int(pid))
    return pids


def find_pids_on_port(port: int) -> list[int]:
    """Best-effort PIDs bound to ``port`` so we can kill a Blender we didn't launch.

    Uses ``netstat -ano`` on Windows and ``lsof`` elsewhere. Returns ``[]`` when
    nothing is found or the tool is unavailable. Never raises.
    """
    pids: set[int] = set()
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            pids |= _parse_netstat_pids(proc.stdout, port)
        else:
            proc = subprocess.run(
                ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=10,
            )
            for tok in proc.stdout.split():
                if tok.strip().isdigit():
                    pids.add(int(tok.strip()))
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(pids)


def _pid_running(pid: int) -> bool:
    """Best-effort check whether ``pid`` is still alive. Conservative on uncertainty."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=8,
            ).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but we can't signal it
    except (OSError, subprocess.SubprocessError):
        return False


def kill_pid(pid: int, timeout: float = 8.0) -> bool:
    """Force-kill a process by PID. Returns whether it is actually gone afterward.

    ``taskkill`` exit code 128 conflates "not found" with "access denied", so it
    can't be trusted as success — verify the process is really gone instead.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, text=True, timeout=timeout,
            )
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        return False
    return not _pid_running(pid)


class BlenderSupervisor:
    """Watches one Blender instance and restarts it on crash/hang.

    ``proc`` is the :class:`subprocess.Popen` handle when *we* launched Blender (it
    lets us kill cleanly); it is ``None`` when the user opened Blender themselves,
    in which case a hung instance is found and killed by port.
    """

    def __init__(self, config: BotConfig, console: Console, *, proc: "subprocess.Popen | None" = None) -> None:
        self.config = config
        self.console = console
        self.proc: subprocess.Popen | None = proc
        self.client = BlenderClient(config.blender_host, config.blender_port)
        self.restarts = 0
        self._exe: str | None = config.blender_path

    def adopt(self, proc: "subprocess.Popen", exe: str | None = None) -> None:
        """Record the handle (and resolved path) of a Blender we just launched."""
        self.proc = proc
        if exe:
            self._exe = exe

    # -- detection ---------------------------------------------------------

    def check(self, timeout: float | None = None) -> str:
        """Return the current health state: ``ok`` | ``crashed`` | ``hung`` | ``unreachable``."""
        # A process we own that has exited is unambiguously a crash, no socket needed.
        if self.proc is not None and self.proc.poll() is not None:
            return "crashed"
        timeout = self.config.blender_health_timeout if timeout is None else timeout
        state, _detail = self.client.health_check(timeout)
        return state

    # -- recovery ----------------------------------------------------------

    def _resolve_exe(self) -> str | None:
        if self._exe and Path(self._exe).exists():
            return self._exe
        try:
            self._exe = find_blender_executable(self.config.blender_path)
        except DiscoveryError as ex:
            self.console.warn(f"cannot relaunch Blender — {ex}")
            return None
        return self._exe

    def _kill_stale(self) -> None:
        """Ensure no old/hung Blender is left holding the port before we relaunch."""
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=8)
            except (OSError, subprocess.SubprocessError):
                pass
            self.proc = None
        # A manually-opened, hung Blender has no handle here — find it by port.
        for pid in find_pids_on_port(self.config.blender_port):
            kill_pid(pid)

    def restart(self, *, checkpoint: Path | None = None) -> bool:
        """Kill the stale instance, relaunch Blender, and wait for its server.

        Reopens ``checkpoint`` (a ``.blend``) when given so progress is preserved.
        Returns ``True`` once Blender is reachable again.
        """
        exe = self._resolve_exe()
        if exe is None:
            return False
        self._kill_stale()
        # Give the OS a moment to release the port before we rebind to it.
        time.sleep(1.0)
        blend = str(checkpoint) if checkpoint and Path(checkpoint).exists() else None
        if blend:
            self.console.info(f"reopening last checkpoint: {checkpoint}")
        try:
            self.proc = launch_blender(exe, self.config.blender_port, blend_file=blend)
        except OSError as ex:
            self.console.warn(f"could not relaunch Blender: {ex}")
            return False
        self.console.info("waiting for Blender to come back up…")
        ok, detail = self.client.wait_until_ready(timeout=self.config.blender_launch_timeout)
        if ok:
            self.restarts += 1
            self.console.success(f"Blender restarted and reconnected: {detail}")
        else:
            self.console.warn(f"Blender relaunched but its server didn't come up: {detail}")
        return ok

    def ensure_healthy(self, *, checkpoint: Path | None = None) -> tuple[bool, bool]:
        """Verify Blender is responsive; restart it if not.

        Returns ``(healthy, restarted)``. When auto-restart is disabled this only
        reports health and never touches the process. Bounded by
        ``config.blender_restart_attempts``.
        """
        state = self.check()
        if state == "ok":
            return True, False
        if not self.config.auto_restart_blender:
            return False, False

        attempts = max(1, self.config.blender_restart_attempts)
        for i in range(attempts):
            label = {
                "crashed": "has crashed",
                "hung": "stopped responding",
            }.get(state, "is unreachable")
            suffix = f" (attempt {i + 1}/{attempts})" if attempts > 1 else ""
            self.console.warn(f"Blender {label} — restarting it{suffix}…")
            if self.restart(checkpoint=checkpoint):
                return True, True
            state = self.check()
            if state == "ok":
                return True, True
        return False, True

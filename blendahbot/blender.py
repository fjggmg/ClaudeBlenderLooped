"""Direct socket client for the Blender MCP add-on.

The blendahbot agent normally talks to Blender through the ``blender-mcp`` MCP
server. This module is the *out-of-band* path the orchestrator uses for things
that must not depend on the model's cooperation:

* a fast pre-flight connectivity check,
* a guaranteed render-to-file fallback (so the critic always has an image), and
* saving the final ``.blend``.

The wire protocol is the add-on's own: a JSON request terminated by a NUL byte,
``{"type": "execute", "code": <python>, "strict_json": <bool>}``, and a JSON
response (also NUL-terminated) of the form
``{"status": "ok"|"error", "result": ..., "message": ..., "stdout": ..., "stderr": ...}``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class BlenderUnavailable(RuntimeError):
    """Raised when Blender cannot be reached or returns an unusable response."""


@dataclass
class BlenderClient:
    host: str = "localhost"
    port: int = 9876
    timeout: float = 300.0

    # -- low level ---------------------------------------------------------

    def _send(self, payload: dict[str, object]) -> dict[str, object]:
        request = (json.dumps(payload) + "\0").encode("utf-8")
        buf = bytearray()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                sock.sendall(request)
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\0" in buf:
                        break
        except ConnectionRefusedError as ex:
            raise BlenderUnavailable(
                f"Cannot connect to Blender at {self.host}:{self.port}. Open Blender, "
                "enable the Blender MCP add-on and start its server."
            ) from ex
        except OSError as ex:
            raise BlenderUnavailable(
                f"Socket error talking to Blender at {self.host}:{self.port}: {ex}"
            ) from ex

        if not buf:
            raise BlenderUnavailable("Empty response from Blender.")
        line, _sep, _rest = buf.partition(b"\0")
        try:
            return json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as ex:
            raise BlenderUnavailable(f"Invalid response from Blender: {ex}") from ex

    def execute(self, code: str, *, strict_json: bool = False) -> dict[str, object]:
        """Run ``code`` in Blender and return the add-on's response dict.

        Raises :class:`BlenderUnavailable` on a transport problem; a Python error
        *inside* Blender comes back as ``{"status": "error", ...}`` instead.
        """
        return self._send({"type": "execute", "code": code, "strict_json": strict_json})

    # -- high level --------------------------------------------------------

    def ping(self) -> tuple[bool, str]:
        """Return ``(ok, detail)``. Never raises."""
        try:
            resp = self.execute(
                "import bpy; print('blender', bpy.app.version_string)"
            )
        except BlenderUnavailable as ex:
            return False, str(ex)
        if resp.get("status") == "ok":
            out = str(resp.get("stdout") or resp.get("result") or "").strip()
            return True, out or "connected"
        return False, str(resp.get("message") or "Blender returned an error")

    def wait_until_ready(
        self,
        timeout: float = 90.0,
        interval: float = 1.5,
        on_progress: Callable[[str], None] | None = None,
        probe_timeout: float = 5.0,
    ) -> tuple[bool, str]:
        """Poll Blender until it answers or ``timeout`` seconds elapse.

        Used right after launching Blender, which takes a few seconds to boot and
        bring its add-on server up. Each probe uses :meth:`health_check` with a
        *bounded* ``probe_timeout`` — crucially NOT the long operational timeout,
        because on some systems a connect to a not-yet-open port blocks until the
        socket timeout instead of refusing fast, which would otherwise defeat the
        poll interval. Never raises; returns ``(ok, last_detail)``.
        """
        deadline = time.monotonic() + timeout
        last = "Blender did not become reachable in time"
        while True:
            remaining = deadline - time.monotonic()
            pt = max(0.2, min(probe_timeout, remaining)) if remaining > 0 else 0.2
            state, detail = self.health_check(timeout=pt)
            if state == "ok":
                return True, detail
            last = detail
            if on_progress is not None:
                on_progress(detail)
            if time.monotonic() >= deadline:
                return False, last
            time.sleep(interval)

    def render_still(self, filepath: str, *, width: int = 1280, height: int = 720) -> tuple[bool, str]:
        """Render the scene to ``filepath`` (PNG), creating a camera/light if needed.

        Returns ``(ok, detail)`` where ``detail`` is the absolute output path on
        success or the failure reason otherwise. This is the safety net used when
        the agent did not leave a usable render behind; it is deliberately
        forgiving rather than artful. ``filepath`` is resolved to an absolute path
        because Blender runs in a different process with its own cwd.
        """
        path = str(Path(filepath).resolve()).replace("\\", "/")
        code = _RENDER_TEMPLATE.format(
            path=json.dumps(path), width=int(width), height=int(height)
        )
        resp = self.execute(code)
        if resp.get("status") == "ok" and "BB_RENDER_OK" in str(resp.get("stdout", "")):
            return True, path
        reason = str(resp.get("message") or resp.get("stderr") or "render did not complete")
        return False, reason.strip() or "render did not complete"

    def save_blend(self, filepath: str) -> bool:
        """Save the current scene to ``filepath``. Returns ``True`` on success."""
        path = str(Path(filepath).resolve()).replace("\\", "/")
        code = (
            "import bpy\n"
            f"bpy.ops.wm.save_as_mainfile(filepath={json.dumps(path)})\n"
            "print('BB_SAVE_OK')\n"
        )
        resp = self.execute(code)
        return resp.get("status") == "ok" and "BB_SAVE_OK" in str(resp.get("stdout", ""))

    def health_check(self, timeout: float = 8.0) -> tuple[str, str]:
        """Classify the live connection with a SHORT deadline. Returns ``(state, detail)``.

        Distinct from :meth:`ping` (which uses the long *operational* timeout): this
        is for spotting a Blender that has *stopped responding*. The add-on services
        its socket from a main-thread ``bpy.app.timers`` callback, so when Blender's
        main thread freezes the OS still completes the TCP handshake but no reply ever
        arrives — only a short read deadline catches that. ``state`` is one of:

        * ``"ok"``          — Blender replied (it is alive and responsive).
        * ``"crashed"``     — connection refused: the process is gone or the server stopped.
        * ``"hung"``        — connected but produced no reply within ``timeout``.
        * ``"unreachable"`` — any other transport error.

        Never raises. Use a generous ``timeout`` and only call this when Blender is
        expected to be idle (e.g. between rounds), so a legitimately busy main thread
        isn't misread as a hang.
        """
        request = (
            json.dumps(
                {
                    "type": "execute",
                    "code": "import bpy; print('bb_health', bpy.app.version_string)",
                    "strict_json": False,
                }
            )
            + "\0"
        ).encode("utf-8")
        buf = bytearray()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect((self.host, self.port))
                sock.sendall(request)
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\0" in buf:
                        break
        except ConnectionRefusedError:
            return "crashed", f"connection refused at {self.host}:{self.port}"
        except socket.timeout:
            return "hung", f"no response within {timeout:.0f}s at {self.host}:{self.port}"
        except OSError as ex:
            return "unreachable", f"socket error at {self.host}:{self.port}: {ex}"
        if not buf:
            # Connected and sent, but the peer closed without replying.
            return "hung", f"connection closed without a response at {self.host}:{self.port}"
        line, _sep, _rest = buf.partition(b"\0")
        try:
            resp = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "hung", f"invalid response from Blender at {self.host}:{self.port}"
        # A reply of *any* status means the main thread is servicing the socket: a
        # Python error inside the probe still proves Blender is alive and responsive.
        detail = str(resp.get("stdout") or resp.get("result") or resp.get("message") or "").strip()
        return "ok", detail or "connected"

    def scene_digest(self) -> str:
        """A short human-readable summary of the scene, or '' if unavailable."""
        code = (
            "import bpy\n"
            "lines=[]\n"
            "for o in bpy.context.scene.objects:\n"
            "    lines.append('%s (%s)' % (o.name, o.type))\n"
            "print('OBJECTS: ' + ', '.join(lines) if lines else 'OBJECTS: (none)')\n"
            "print('FRAME: %d' % bpy.context.scene.frame_current)\n"
        )
        try:
            resp = self.execute(code)
        except BlenderUnavailable:
            return ""
        if resp.get("status") == "ok":
            return str(resp.get("stdout", "")).strip()
        return ""


# Runs at Blender startup (via --python), targeting the official Blender Lab
# "blender-mcp" add-on. That add-on already auto-starts its socket server ~1s
# after a GUI launch *when online access is enabled* (we pass --online-mode), so
# this script is mainly a fallback: enable the add-on if needed, set the port on
# its preferences, and call the start operator. Every step is best-effort — a
# wrong module guess or an already-running server just no-ops, and if none of it
# works the caller's wait simply times out and falls back to asking the user to
# start the server by hand. A timer defers the work until the UI/context is ready
# (the operator needs a window).
_STARTUP_TEMPLATE = """\
import bpy

# Suppress the Windows "Blender has stopped working" crash dialog for THIS process.
# Without this a hard crash pops a modal box and SUSPENDS Blender until someone clicks
# it — so it never frees the port and looks like a hang forever. With it, a crash
# terminates cleanly and the supervisor detects the dead process and restarts it.
try:
    import os as _os
    if _os.name == "nt":
        import ctypes as _ctypes
        # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
        _ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except Exception:
    pass


def _bb_start_mcp():
    try:
        keys = list(bpy.context.preferences.addons.keys())
    except Exception:
        keys = []
    if not any("mcp" in k.lower() for k in keys):
        # Extension module ids vary by repo; try the known ones, newest install
        # convention first. An already-enabled add-on or a wrong guess just errors.
        for mod in ("bl_ext.lab_blender_org.mcp", "bl_ext.user_default.mcp",
                    "bl_ext.blender_org.mcp", "blender_mcp", "addon"):
            try:
                bpy.ops.preferences.addon_enable(module=mod)
                break
            except Exception:
                continue
    # The port lives on the add-on preferences (an AddonPreferences property),
    # not on the scene. Set it on whichever enabled add-on looks like the bridge.
    for key in list(bpy.context.preferences.addons.keys()):
        if "mcp" not in key.lower():
            continue
        try:
            prefs = bpy.context.preferences.addons[key].preferences
            if getattr(prefs, "port", {port}) != {port}:
                prefs.port = {port}
        except Exception:
            pass
    # The add-on auto-starts its server on the DEFAULT port (9876) shortly after
    # launch, and start() refuses to rebind a running server. So when a non-default
    # port is configured, stop that auto-started server first, then start on our
    # port. For the default port we leave the auto-started server alone and just
    # ensure it's up (start is a harmless no-op if already running) — this avoids a
    # stop that, were the following start to fail, would leave NO server running.
    if {port} != 9876:
        try:
            bpy.ops.blmcp.server_stop()
        except Exception:
            pass
    # Official operator id is ``blmcp.server_start``; starting an already-running
    # server raises, which is fine.
    try:
        bpy.ops.blmcp.server_start()
    except Exception:
        pass
    return None  # one-shot: do not reschedule


# NOTE: the kwarg is ``first_interval`` (NOT ``first_delay``); the wrong name raises
# a TypeError that would abort this whole startup script. Wrapped so a future API
# change degrades gracefully rather than killing the launch.
try:
    bpy.app.timers.register(_bb_start_mcp, first_interval=2.0)
except Exception:
    _bb_start_mcp()
"""


def launch_blender(
    executable: str,
    port: int = 9876,
    blend_file: str | None = None,
) -> "subprocess.Popen[bytes]":
    """Open Blender (detached) and best-effort start its MCP add-on server.

    The process is started detached so it outlives the blendahbot console. The
    startup script is written to a temp file and passed via ``--python`` (more
    robust than inlining a multi-line ``--python-expr``). ``--online-mode`` is
    required: the official add-on refuses to start its socket server unless
    ``bpy.app.online_access`` is true. When ``blend_file`` is given it is opened on
    launch (used to restore a checkpoint after a restart). Raises ``OSError`` if
    the executable can't be spawned.
    """
    fd, script_path = tempfile.mkstemp(prefix="blendahbot_start_", suffix=".py")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(_STARTUP_TEMPLATE.format(port=int(port)))

    # Order matters: a positional .blend is loaded first, then --python runs in the
    # context of that file. --online-mode enables the add-on's socket server.
    args = [executable, "--online-mode"]
    if blend_file:
        args.append(str(blend_file))
    args += ["--python", script_path]
    kwargs: dict[str, object] = {}
    if os.name == "nt":
        # Detach from this console so quitting blendahbot doesn't close Blender.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "DETACHED_PROCESS", 0x00000008
        )
    else:
        kwargs["start_new_session"] = True

    # A child inherits the caller's error mode at spawn, so disable the Windows
    # crash dialog across the spawn (and restore ours after). This covers a crash
    # during Blender's own startup, before the --python script can set it itself.
    prev_mode = _suppress_crash_dialog()
    try:
        return subprocess.Popen(args, **kwargs)  # noqa: S603 - executable is discovered/configured
    finally:
        _restore_error_mode(prev_mode)


def _suppress_crash_dialog() -> int | None:
    """Disable the Windows fault dialog for the next child process; return the prior mode.

    ``None`` on non-Windows or if the call fails. A child process inherits the
    caller's error mode at creation, so setting ``SEM_NOGPFAULTERRORBOX`` here makes
    a crashing Blender terminate instead of blocking on a modal "stopped working" box.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        return int(ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000))
    except Exception:
        return None


def _restore_error_mode(prev: int | None) -> None:
    if prev is None or os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(prev)
    except Exception:
        pass


# bpy executed inside Blender. Frames a dedicated camera over the actual geometry
# and guarantees light/world so the fallback render is never a black frame. The
# user's active camera and film setting are restored afterwards.
_RENDER_TEMPLATE = """
import bpy, mathutils
from math import radians, atan, tan, sin

scene = bpy.context.scene
geo = [o for o in scene.objects
       if o.type in {{'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}} and not o.hide_render]

_prev_cam = scene.camera
_prev_film = scene.render.film_transparent

# Always frame a dedicated camera, so an existing or mis-aimed camera cannot
# leave us rendering empty space.
try:
    cam = bpy.data.objects.get("BB_AutoCam")
    if cam is None or cam.type != 'CAMERA':
        cam = bpy.data.objects.new("BB_AutoCam", bpy.data.cameras.new("BB_AutoCam"))
        scene.collection.objects.link(cam)
    if geo:
        corners = []
        for o in geo:
            for c in o.bound_box:
                corners.append(o.matrix_world @ mathutils.Vector(c))
        mn = mathutils.Vector((min(c.x for c in corners),
                               min(c.y for c in corners),
                               min(c.z for c in corners)))
        mx = mathutils.Vector((max(c.x for c in corners),
                               max(c.y for c in corners),
                               max(c.z for c in corners)))
        center = (mn + mx) / 2.0
        radius = max((mx - mn).length / 2.0, 0.5)
        lens = cam.data.lens; sensor = cam.data.sensor_width
        hfov = 2.0 * atan((sensor / 2.0) / lens)
        vfov = 2.0 * atan(tan(hfov / 2.0) / ({width} / {height}))
        dist = (radius * 1.35) / sin(min(hfov, vfov) / 2.0)
        cam.data.clip_end = max(cam.data.clip_end, dist * 4.0)
        d = mathutils.Vector((1.0, -1.0, 0.6)).normalized()
        cam.location = center + d * dist
        cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()
    else:
        cam.location = (7.0, -7.0, 5.0)
        cam.rotation_euler = (radians(63), 0.0, radians(46))
    scene.camera = cam
except Exception as _frame_err:
    print("BB_FRAME_WARN", _frame_err)


def _contributes(o):
    return (o.type == 'LIGHT' and not o.hide_render
            and getattr(o.data, 'energy', 0.0) > 0.0)


if not any(_contributes(o) for o in scene.objects):
    light_data = bpy.data.lights.new("BB_AutoSun", 'SUN')
    light_data.energy = 3.0
    light = bpy.data.objects.new("BB_AutoSun", light_data)
    scene.collection.objects.link(light)
    light.rotation_euler = (radians(50), radians(15), radians(30))

if scene.world is None:
    world = bpy.data.worlds.new("BB_AutoWorld")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs[0].default_value = (0.05, 0.05, 0.06, 1.0)

r = scene.render
r.film_transparent = False
r.filepath = {path}
r.image_settings.file_format = 'PNG'
r.image_settings.color_mode = 'RGB'
r.resolution_x = {width}
r.resolution_y = {height}
r.resolution_percentage = 100
bpy.ops.render.render(write_still=True)

scene.camera = _prev_cam if _prev_cam is not None else scene.camera
scene.render.film_transparent = _prev_film
print("BB_RENDER_OK")
"""

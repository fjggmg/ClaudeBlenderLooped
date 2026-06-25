"""Locate the external programs blendahbot drives.

Two things must be found at runtime:

* the ``claude`` CLI (the Claude Agent SDK spawns it; it is what gives us
  subscription auth, web search, file tools, etc.), and
* the ``blender-mcp`` stdio server (the bridge to the running Blender add-on).

Neither is guaranteed to be on ``PATH`` on a Claude Desktop install, so we probe
the well-known Claude install locations as well.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path


class DiscoveryError(RuntimeError):
    """Raised when a required external program cannot be located."""


def _appdata() -> Path | None:
    p = os.environ.get("APPDATA")
    return Path(p) if p else None


def _version_key(path: Path) -> list[int]:
    """Sort key from the integer components of a name like ``2.1.181``."""
    nums = re.findall(r"\d+", path.name)
    return [int(n) for n in nums] if nums else [0]


def find_claude_cli(override: str | None = None) -> str:
    """Return a path to the ``claude`` executable.

    Search order: explicit override -> env vars -> ``PATH`` -> the per-user
    local install -> the newest Claude Desktop managed CLI.
    """
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    for env in ("BLENDAHBOT_CLAUDE_CLI", "CLAUDE_CLI_PATH"):
        if (v := os.environ.get(env)):
            candidates.append(Path(v))
    if (which := shutil.which("claude")):
        candidates.append(Path(which))

    home = Path.home()
    for rel in (".claude/local/claude.exe", ".claude/local/claude.cmd", ".claude/local/claude"):
        candidates.append(home / rel)

    appdata = _appdata()
    if appdata:
        base = appdata / "Claude" / "claude-code"
        if base.is_dir():
            versions = sorted(
                (d for d in base.iterdir() if d.is_dir()),
                key=_version_key,
                reverse=True,
            )
            for d in versions:
                candidates.append(d / "claude.exe")

    for c in candidates:
        if c.exists() and c.is_file():
            return str(c)

    raise DiscoveryError(
        "Could not locate the `claude` CLI. Install Claude Code, or set the "
        "BLENDAHBOT_CLAUDE_CLI environment variable to its full path."
    )


def find_blender_mcp_command(override: list[str] | None = None) -> list[str]:
    """Return the command (argv list) that launches the blender-mcp stdio server.

    Prefers the self-contained ``blender-mcp.exe`` shipped with the official
    Blender connector (no PATH or ``uv`` dependency), then falls back to a
    ``blender-mcp`` on PATH, then to ``uv run``.
    """
    if override:
        return list(override)
    if (v := os.environ.get("BLENDER_MCP_SERVER_CMD")):
        return shlex.split(v, posix=False)

    appdata = _appdata()
    connector = (
        appdata / "Claude" / "Claude Extensions" / "ant.dir.gh.blender.blender-mcp"
        if appdata
        else None
    )
    if connector is not None:
        exe = connector / ".venv" / "Scripts" / "blender-mcp.exe"
        if exe.exists():
            return [str(exe)]

    if (which := shutil.which("blender-mcp")):
        return [which]

    uv = shutil.which("uv")
    if uv and connector is not None and connector.is_dir():
        return [uv, "run", "--project", str(connector), "blender-mcp"]

    raise DiscoveryError(
        "Could not locate the blender-mcp server. Install the Blender MCP "
        "connector in Claude, `pip install blender-mcp`, or set "
        "BLENDER_MCP_SERVER_CMD to the launch command."
    )

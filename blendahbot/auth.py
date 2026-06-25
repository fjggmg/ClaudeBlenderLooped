"""Subscription auth for blendahbot — mirrors the proven tradingbot9000 approach.

The standalone ``claude`` CLI that the Agent SDK spawns will NOT reuse the Claude
Desktop app's login. The robust, hands-off path is a long-lived OAuth token:

* ``claude setup-token`` opens a browser ("approve"), mints a ~1-year token tied to
  your subscription, and prints the BARE token to **stdout** (the URL/prompts go to
  stderr). It does *not* write ``credentials.json``.
* That stdout is **soft-wrapped at the terminal width (~80 cols)**, so the token is
  split across lines — naive capture corrupts it. We rejoin it.
* We save the token and feed it to every spawned CLI via the ``CLAUDE_CODE_OAUTH_TOKEN``
  environment variable, whose precedence beats the expiring ``credentials.json`` login.

After a one-time ``blendahbot --auth`` (or ``start.bat auth``), every run is hands-off.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .discovery import DiscoveryError, find_claude_cli

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
# Claude tokens look like ``sk-ant-oat...``; guard against a chopped capture.
_MIN_TOKEN_LEN = 90


def token_path() -> Path:
    base = os.environ.get("BLENDAHBOT_HOME")
    root = Path(base) if base else (Path.home() / ".blendahbot")
    return root / "oauth_token"


# -- token file -----------------------------------------------------------

def load_saved_token() -> str | None:
    path = token_path()
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                return line
    except OSError:
        return None
    return None


def save_token(tok: str) -> Path:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tok.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best-effort; no-op on most Windows filesystems
    except OSError:
        pass
    return path


# -- token plumbing -------------------------------------------------------

def extract_token(stdout: str) -> str | None:
    """Rebuild the token from ``claude setup-token`` stdout.

    The token is bare but soft-wrapped (newlines/CRs inserted mid-token). Take
    everything from the ``sk-ant-`` prefix to the first blank line and strip all
    interior whitespace, preserving the base64/JWT chars a regex would chop.
    """
    out = stdout or ""
    i = out.find("sk-ant-")
    if i < 0:
        return None
    block = out[i:].split("\n\n", 1)[0]
    tok = "".join(block.split())
    return tok if len(tok) >= _MIN_TOKEN_LEN else None


def have_credential() -> bool:
    return bool(
        os.environ.get(TOKEN_ENV)
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or load_saved_token()
    )


def load_into_env() -> str | None:
    """Make a saved token available to spawned CLIs. Returns the token in effect."""
    if os.environ.get(TOKEN_ENV):
        return os.environ[TOKEN_ENV]
    tok = load_saved_token()
    if tok:
        os.environ[TOKEN_ENV] = tok
    return tok


def auth_env() -> dict[str, str]:
    """The env mapping to pass to ClaudeAgentOptions so the child CLI authenticates."""
    tok = os.environ.get(TOKEN_ENV) or load_saved_token()
    return {TOKEN_ENV: tok} if tok else {}


# -- interactive setup ----------------------------------------------------

def _say(console, msg: str) -> None:
    if console is not None:
        console.info(msg)
    else:
        print(msg, file=sys.stderr, flush=True)


def _warn_conflicts(console) -> None:
    for var in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(var):
            _say(console, f"NOTE: {var} is set and OUTRANKS the OAuth token — unset it if auth still fails.")


def run_setup_token(cli_path: str | None, console) -> str | None:
    try:
        claude = find_claude_cli(cli_path)
    except DiscoveryError as ex:
        _say(console, f"claude CLI not found: {ex}")
        return None
    _say(console, "Launching `claude setup-token` — a browser will open; click Approve.")
    _say(console, "One-time step: the ~1-year subscription token is saved and reused.")
    # Wide terminal hint avoids the soft-wrap up front; we rejoin regardless. Drop
    # nested-session vars so the child talks to the real OAuth endpoint.
    env = dict(os.environ, COLUMNS="4096", LINES="200")
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    try:
        # stdin/stderr inherit so the user sees the URL and can approve; stdout is
        # captured because setup-token prints the bare token there.
        proc = subprocess.run([claude, "setup-token"], stdout=subprocess.PIPE, text=True, env=env)
    except OSError as ex:
        _say(console, f"setup-token failed to run: {ex}")
        return None
    tok = extract_token(proc.stdout)
    if not tok:
        _say(console, "Could not read a token from setup-token output.")
        _say(console, f"Run `claude setup-token` yourself and paste the token into {token_path()}")
    return tok


def setup(cli_path: str | None = None, console=None) -> int:
    """Force a fresh login and persist the token. Returns a process exit code."""
    _warn_conflicts(console)
    os.environ.pop(TOKEN_ENV, None)  # don't let a stale env token mask the new one
    tok = run_setup_token(cli_path, console)
    if not tok:
        return 2
    path = save_token(tok)
    os.environ[TOKEN_ENV] = tok
    _say(console, f"Token saved to {path} ({len(tok)} chars, prefix {tok[:13]}…). Future runs are hands-off.")
    return 0


def ensure(cli_path: str | None = None, console=None) -> int:
    """Guarantee a usable credential: reuse env/saved token, else one-time setup."""
    if have_credential():
        load_into_env()
        return 0
    _say(console, "No saved credential yet — starting the one-time subscription login.")
    return setup(cli_path, console)

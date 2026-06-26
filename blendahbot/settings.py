"""Persistent user settings + an interactive editor (start.bat -> Settings).

Saved to ``~/.blendahbot/settings.json``. These become the defaults for every
build (CLI flags still override them). Lets the user set an API key, a budget, a
model, and quality knobs once instead of typing flags each time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Settings keys that map directly onto BotConfig fields.
_CONFIG_KEYS = (
    "budget_usd", "model", "score_threshold", "max_rounds", "patience", "refs",
    "use_critic", "steer",
)

# (key, label, kind) in menu order. kind drives parsing/formatting.
_MENU = [
    ("anthropic_api_key", "Anthropic API key (pay-per-token; blank = subscription)", "secret"),
    ("budget_usd", "Budget per build (USD)", "float"),
    ("model", "Model id (e.g. claude-opus-4-8)", "str"),
    ("score_threshold", "Quality score needed to finish (0-100)", "int"),
    ("max_rounds", "Max rounds (blank = unlimited)", "int_or_none"),
    ("patience", "Stop after N rounds with no improvement (0 = never)", "int"),
    ("refs", "Reference images to fetch (0 = off)", "int"),
    ("use_critic", "Independent critic", "bool"),
    ("steer", "Live steering", "bool"),
]


def settings_path() -> Path:
    base = os.environ.get("BLENDAHBOT_HOME")
    root = Path(base) if base else (Path.home() / ".blendahbot")
    return root / "settings.json"


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # contains an API key; lock down where supported
    except OSError:
        pass
    return path


def apply_to_env(data: dict) -> None:
    """Put a saved API key into the environment (does not clobber an explicit one)."""
    key = data.get("anthropic_api_key")
    if key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = str(key)


def config_overrides(data: dict) -> dict:
    """The subset of settings that override BotConfig defaults (skips unset/None)."""
    return {k: data[k] for k in _CONFIG_KEYS if data.get(k) is not None}


# --------------------------------------------------------------------------
# interactive editor
# --------------------------------------------------------------------------

def _fmt(data: dict, field: str, kind: str) -> str:
    value = data.get(field)
    if kind == "secret":
        return f"set (…{str(value)[-4:]})" if value else "(not set — using subscription login)"
    if value is None:
        return "unlimited" if field == "max_rounds" else "(default)"
    if kind == "bool":
        return "on" if value else "off"
    return str(value)


def _edit(data: dict, field: str, label: str, kind: str) -> None:
    if kind == "bool":
        ans = input(f"  {label} — on/off (blank = keep): ").strip().lower()
        if ans in ("on", "yes", "y", "true", "1"):
            data[field] = True
        elif ans in ("off", "no", "n", "false", "0"):
            data[field] = False
        return
    if kind == "secret":
        from getpass import getpass

        raw = getpass(f"  {label} — paste key (blank = keep, '-' to clear, hidden): ").strip()
    else:
        raw = input(f"  {label} — new value (blank = keep, '-' to clear): ").strip()
    if raw == "":
        return
    if raw == "-":
        data.pop(field, None)
        return
    try:
        if kind in ("int", "int_or_none"):
            data[field] = int(raw)
        elif kind == "float":
            data[field] = float(raw)
        else:
            data[field] = raw
    except ValueError:
        print("  ? invalid value — left unchanged.")


def run_settings_menu() -> int:
    data = load_settings()
    print(f"\nblendahbot settings  ({settings_path()})")
    while True:
        print()
        for i, (field, label, kind) in enumerate(_MENU, 1):
            print(f"  {i}) {label}: {_fmt(data, field, kind)}")
        print("  s) save and exit     q) cancel")
        choice = input("Choose a number to change (or s/q): ").strip().lower()
        if choice in ("s", ""):
            path = save_settings(data)
            print(f"Saved to {path}")
            return 0
        if choice in ("q", "c"):
            print("No changes saved.")
            return 0
        if not choice.isdigit() or not (1 <= int(choice) <= len(_MENU)):
            print("  ? enter a listed number, 's' to save, or 'q' to cancel.")
            continue
        _edit(data, *_MENU[int(choice) - 1])

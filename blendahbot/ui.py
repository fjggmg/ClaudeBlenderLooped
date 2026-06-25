"""Console output. Uses `rich` when available, falls back to plain text."""

from __future__ import annotations

import sys

try:  # optional, but listed as a dependency
    from rich.console import Console as _RichConsole
    from rich.markup import escape as _rich_escape
    from rich.panel import Panel
    from rich.text import Text

    _HAVE_RICH = True
except Exception:  # pragma: no cover - rich missing
    _HAVE_RICH = False


def _truncate(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Console:
    def __init__(self, plain: bool = False) -> None:
        # Windows consoles default to cp1252, which cannot encode the glyphs we
        # use (✓, …, →). Force UTF-8 with a replacement fallback so output never
        # crashes regardless of terminal or redirection.
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (ValueError, OSError):
                    pass
        self.plain = plain or not _HAVE_RICH
        self._rich = None if self.plain else _RichConsole()

    # -- primitives --------------------------------------------------------

    def _print(self, msg: str = "") -> None:
        if self._rich is not None:
            self._rich.print(msg)
        else:
            print(_strip_markup(msg), file=sys.stdout, flush=True)

    def _esc(self, s: object) -> str:
        """Escape model-derived text so rich does not interpret its brackets as
        markup (which otherwise raises MarkupError on sequences like ``[/x]``)."""
        if self._rich is not None:
            return _rich_escape(str(s))
        return str(s)

    def rule(self, title: str) -> None:
        if self._rich is not None:
            self._rich.rule(f"[bold]{self._esc(title)}[/bold]")
        else:
            print(f"\n=== {title} ===", flush=True)

    # -- semantic helpers --------------------------------------------------

    def info(self, msg: str) -> None:
        self._print(f"[cyan]{self._esc(msg)}[/cyan]")

    def success(self, msg: str) -> None:
        self._print(f"[bold green]✓ {self._esc(msg)}[/bold green]")

    def warn(self, msg: str) -> None:
        self._print(f"[yellow]! {self._esc(msg)}[/yellow]")

    def error(self, msg: str) -> None:
        self._print(f"[bold red]✗ {self._esc(msg)}[/bold red]")

    def agent(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._rich is not None:
            self._rich.print(Panel(Text(text), border_style="blue", title="claude"))
        else:
            print(f"\n[claude] {text}", flush=True)

    def tool_call(self, name: str, brief: str = "") -> None:
        brief = _truncate(brief, 160)
        self._print(f"  [magenta]→ {self._esc(name)}[/magenta] [dim]{self._esc(brief)}[/dim]")

    def tool_result(self, brief: str, is_error: bool = False) -> None:
        brief = _truncate(brief, 200)
        colour = "red" if is_error else "dim"
        self._print(f"    [{colour}]{self._esc(brief)}[/{colour}]")

    def thinking(self, text: str) -> None:
        if not text.strip():
            return
        self._print(f"  [dim italic]thinking: {self._esc(_truncate(text, 200))}[/dim italic]")


def _strip_markup(msg: str) -> str:
    """Remove rich-style [tags] for plain output."""
    import re

    return re.sub(r"\[/?[a-zA-Z0-9_ #]+\]", "", msg)

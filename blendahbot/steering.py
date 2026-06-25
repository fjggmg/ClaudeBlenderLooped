"""Live steering: let the user type extra instructions while a build runs.

A daemon thread reads stdin and pushes lines onto an asyncio queue (thread-safe
via the event loop). The builder drains the queue between rounds and, for
immediate redirection, an in-round watcher interrupts the agent the moment a line
arrives so the instruction is incorporated without waiting for the round to end.
"""

from __future__ import annotations

import asyncio
import sys
import threading

# Lines that mean "wrap things up now" rather than "here's an instruction".
STOP_TOKENS = {"/stop", "stop", "/quit", "/exit", "/done"}


def _stdin_usable() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.readable()
    except Exception:
        return False


class Steering:
    """Collects user instructions typed during a run."""

    def __init__(self, loop: asyncio.AbstractEventLoop, enabled: bool = True) -> None:
        self.loop = loop
        self.enabled = enabled
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.stop_requested = False
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        if not self.enabled or self._started:
            return
        if not _stdin_usable():
            self.enabled = False
            return
        self._started = True
        self._thread = threading.Thread(
            target=self._reader, name="blendahbot-stdin", daemon=True
        )
        self._thread.start()

    def _reader(self) -> None:
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if line:
                    self.loop.call_soon_threadsafe(self._enqueue, line)
        except Exception:
            pass

    def _enqueue(self, line: str) -> None:
        if line.lower() in STOP_TOKENS:
            self.stop_requested = True
        self.queue.put_nowait(line)

    def drain(self) -> list[str]:
        """Return all queued instructions (excluding stop tokens)."""
        out: list[str] = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return [m for m in out if m.lower() not in STOP_TOKENS]

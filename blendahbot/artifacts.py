"""Run artifacts: transcript logging, image collection, final report."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any


class Transcript:
    """Append-only JSONL log of every SDK message across the run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def write(self, source: str, message: Any) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "kind": type(message).__name__,
            "data": _serialise(message),
        }
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def _serialise(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _serialise(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_serialise(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def collect_pngs(directory: Path) -> list[Path]:
    """All PNGs directly under ``directory``, newest first."""
    if not directory.is_dir():
        return []
    pngs = [p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".png"]
    pngs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return pngs


def write_report(path: Path, *, request: str, rounds: list[dict[str, Any]], outcome: str,
                 total_cost: float, blend_path: str | None, final_render: str | None) -> None:
    lines = [
        "# blendahbot build report",
        "",
        f"**Request:** {request}",
        "",
        f"**Outcome:** {outcome}",
        f"**Rounds:** {len(rounds)}",
        f"**Total cost (USD):** {total_cost:.4f}" if total_cost else "**Total cost (USD):** n/a",
    ]
    if blend_path:
        lines.append(f"**Saved .blend:** `{blend_path}`")
    if final_render:
        lines.append(f"**Final render:** `{final_render}`")
    lines.append("")
    lines.append("## Rounds")
    for r in rounds:
        lines.append("")
        lines.append(f"### Round {r['round']}")
        lines.append(f"- builder self-score: {r.get('self_score', 'n/a')}")
        lines.append(f"- critic score: {r.get('critic_score', 'n/a')}")
        lines.append(f"- satisfied: {r.get('satisfied', False)}")
        if r.get("summary"):
            lines.append(f"- summary: {r['summary']}")
        if r.get("issues"):
            lines.append("- issues:")
            lines.extend(f"  - {i}" for i in r["issues"])
        if r.get("render"):
            lines.append(f"- render: `{r['render']}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

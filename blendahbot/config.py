"""Runtime configuration for a blendahbot build run."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def slugify(text: str, max_len: int = 40) -> str:
    """Turn a free-form request into a filesystem-safe slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    s = s[:max_len].strip("-")
    return s or "creation"


@dataclass
class BotConfig:
    """Everything a single build run needs.

    The only required field is ``request``. Everything else has a sensible
    default that can be overridden from the CLI or environment.
    """

    request: str

    # Where run artifacts (renders, the .blend, the report) are written.
    out_root: Path = Path("runs")

    # Loop bounds. max_rounds None = keep going until the reviewer is satisfied
    # (or the score plateaus for `patience` rounds, or the budget runs out).
    max_rounds: int | None = None
    patience: int = 3
    max_turns_per_round: int = 150
    budget_usd: float | None = None

    # Quality gate.
    score_threshold: int = 80
    use_critic: bool = True

    # Opt-in independent vetting of generated assets (gen3d --vet). When on, the builder
    # is told to pass --vet to gen3d so an independent critic gates every generated mesh
    # in isolation and auto-regenerates a bad one before it reaches the scene.
    vet_assets: bool = False
    vet_max_attempts: int = 2
    vet_score_threshold: int = 55

    # Model + brain.
    model: str | None = None
    setting_sources: list[str] = field(default_factory=list)
    cli_path: str | None = None

    # Blender connection.
    blender_host: str = "localhost"
    blender_port: int = 9876
    blender_mcp_cmd: list[str] | None = None
    require_blender: bool = True

    # Auto-launch: if Blender isn't reachable at preflight, open it ourselves.
    # blender_path is the executable to open (None = auto-detect, and we remember
    # what we find). blender_launch_timeout bounds how long we wait for it to boot
    # and bring its add-on server up.
    auto_launch_blender: bool = True
    blender_path: str | None = None
    blender_launch_timeout: float = 90.0

    # Auto-restart: if Blender crashes or stops responding mid-build, kill the
    # stale instance and relaunch it (reopening the last checkpoint .blend so work
    # isn't lost). blender_health_timeout bounds the between-rounds liveness probe
    # used to detect a hang; blender_restart_attempts caps recovery tries per stall.
    auto_restart_blender: bool = True
    blender_health_timeout: float = 8.0
    blender_restart_attempts: int = 2

    # Reference images downloaded up front to ground the build (0 disables).
    refs: int = 6

    # Let the builder download + install Blender extensions/add-ons, asset libraries
    # and Python packages on demand (via `python -m blendahbot.addons`). On by default;
    # turn off to forbid autonomous installation of third-party code.
    addons: bool = True

    # Interaction.
    steer: bool = True

    # Ask the user (interactively) whether they have their own reference images
    # before building. Skipped automatically when stdin isn't a terminal.
    ask_refs: bool = True
    # Reference image files/folders supplied non-interactively (e.g. CLI --ref).
    # When set, the interactive prompt is skipped.
    ref_paths: list[str] = field(default_factory=list)

    # Presentation.
    plain: bool = False
    verbose: bool = False

    # Computed at construction time.
    run_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        # Resolve to an absolute path: these paths are handed to Blender, which
        # is a SEPARATE process with its own working directory. A relative path
        # would be written relative to Blender's cwd, not ours.
        self.out_root = Path(self.out_root).resolve()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = (self.out_root / f"{stamp}_{slugify(self.request)}").resolve()

    # -- derived paths -----------------------------------------------------

    def round_dir(self, n: int) -> Path:
        return self.run_dir / f"round_{n:02d}"

    @property
    def final_dir(self) -> Path:
        return self.run_dir / "final"

    @property
    def transcript_path(self) -> Path:
        return self.run_dir / "transcript.jsonl"

    # -- construction helpers ---------------------------------------------

    @classmethod
    def from_env(cls, request: str, **overrides: object) -> "BotConfig":
        """Build a config, layering env vars then explicit overrides.

        ``overrides`` values that are ``None`` are ignored so callers can pass
        argparse results directly without clobbering defaults.
        """
        kwargs: dict[str, object] = {"request": request}
        if (v := os.environ.get("BLENDER_MCP_HOST")):
            kwargs["blender_host"] = v
        if (v := os.environ.get("BLENDER_MCP_PORT")):
            kwargs["blender_port"] = int(v)
        if (v := os.environ.get("BLENDAHBOT_MODEL")):
            kwargs["model"] = v
        if (v := os.environ.get("BLENDAHBOT_OUT")):
            kwargs["out_root"] = Path(v)
        if (v := os.environ.get("BLENDAHBOT_BLENDER")):
            kwargs["blender_path"] = v
        if os.environ.get("BLENDAHBOT_NO_AUTO_BLENDER"):
            kwargs["auto_launch_blender"] = False
        if os.environ.get("BLENDAHBOT_NO_AUTO_RESTART"):
            kwargs["auto_restart_blender"] = False
        if os.environ.get("BLENDAHBOT_VET_ASSETS"):
            kwargs["vet_assets"] = True
        if os.environ.get("BLENDAHBOT_NO_ADDONS"):
            kwargs["addons"] = False
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)  # type: ignore[arg-type]

"""Command-line interface for blendahbot."""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import __version__
from . import auth, settings
from .blender import BlenderUnavailable
from .builder import run_selftest
from .config import BotConfig
from .discovery import DiscoveryError
from .errors import AUTH_REMEDIATION, AuthError
from .loop import build, preflight, report_preflight
from .ui import Console


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="blendahbot",
        description="Autonomously build what you describe in a live Blender session, "
        "looping until an independent reviewer agrees it's good.",
    )
    p.add_argument("request", nargs="*", help="What to create, e.g. \"a cozy low-poly cabin in a pine forest at dusk\".")
    p.add_argument("--check", action="store_true", help="Run preflight checks and exit.")
    p.add_argument("--auth", action="store_true", help="Log in with your Claude subscription (one-time browser approve), save the token, and exit.")
    p.add_argument("--settings", action="store_true", help="Open the interactive settings editor (API key, budget, model, quality), then exit.")
    p.add_argument("--selftest", action="store_true", help="Verify the claude CLI authenticates (tiny model call), then exit.")
    p.add_argument("--out", default=None, help="Output root directory (default: ./runs).")
    p.add_argument("--max-rounds", type=int, default=None, help="Hard cap on rounds (default: unlimited — runs until the reviewer is satisfied or quality plateaus).")
    p.add_argument("--patience", type=int, default=None, help="Stop after this many rounds with no score improvement (default: 3; 0 = never).")
    p.add_argument("--max-turns", type=int, default=None, help="Max agent turns per round (default: 80).")
    p.add_argument("--budget", type=float, default=None, help="Hard USD spend cap for the build.")
    p.add_argument("--model", default=None, help="Model id (e.g. claude-opus-4-8). Default: CLI default.")
    p.add_argument("--threshold", type=int, default=None, help="Critic score (0-100) required to finish (default: 80).")
    p.add_argument("--no-critic", action="store_true", help="Skip the independent critic; trust the builder's self-assessment.")
    p.add_argument("--no-steer", action="store_true", help="Disable live steering (don't read stdin for mid-build instructions).")
    p.add_argument("--refs", type=int, default=None, help="Reference photos to fetch up front to ground the build (default: 6).")
    p.add_argument("--no-refs", action="store_true", help="Don't fetch reference images.")
    p.add_argument("--blender-host", default=None, help="Blender add-on host (default: localhost).")
    p.add_argument("--blender-port", type=int, default=None, help="Blender add-on port (default: 9876).")
    p.add_argument("--allow-no-blender", action="store_true", help="Proceed even if Blender is unreachable at start.")
    p.add_argument("--plain", action="store_true", help="Plain text output (no colour/panels).")
    p.add_argument("--verbose", action="store_true", help="Show CLI stderr and extra detail.")
    p.add_argument("--version", action="version", version=f"blendahbot {__version__}")
    return p


def _config_from_args(args: argparse.Namespace, request: str) -> BotConfig:
    # Precedence (low -> high): BotConfig defaults < env < saved settings < CLI flags.
    saved = settings.load_settings()
    settings.apply_to_env(saved)  # exports ANTHROPIC_API_KEY if the user set one
    overrides: dict[str, object] = settings.config_overrides(saved)

    cli = {
        "out_root": args.out,
        "max_rounds": args.max_rounds,
        "patience": args.patience,
        "max_turns_per_round": args.max_turns,
        "budget_usd": args.budget,
        "model": args.model,
        "score_threshold": args.threshold,
        "blender_host": args.blender_host,
        "blender_port": args.blender_port,
        "plain": args.plain or None,
        "verbose": args.verbose or None,
        "refs": args.refs,
    }
    overrides.update({k: v for k, v in cli.items() if v is not None})
    if args.no_critic:
        overrides["use_critic"] = False
    if args.no_steer:
        overrides["steer"] = False
    if args.no_refs:
        overrides["refs"] = 0
    if args.allow_no_blender:
        overrides["require_blender"] = False
    return BotConfig.from_env(request, **overrides)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console(plain=args.plain)

    if args.check:
        cfg = _config_from_args(args, "(preflight check)")
        report_preflight(preflight(cfg), console)
        return 0

    if args.settings:
        return settings.run_settings_menu()

    if args.auth:
        cfg = _config_from_args(args, "(auth)")
        console.rule("auth")
        return auth.setup(cfg.cli_path, console)

    if args.selftest:
        cfg = _config_from_args(args, "(selftest)")
        console.rule("selftest")
        auth.ensure(cfg.cli_path, console)

        def _cb(line: str) -> None:
            if args.verbose:
                console.thinking(f"[cli stderr] {line}")

        try:
            ok, detail = asyncio.run(run_selftest(cfg, _cb))
        except DiscoveryError as ex:
            console.error(str(ex))
            return 2
        if ok:
            console.success(f"auth OK — model replied: {detail!r}")
            return 0
        if detail == "auth":
            console.error("authentication failed.")
            console.info(AUTH_REMEDIATION)
            return 3
        console.error(f"selftest failed: {detail}")
        return 1

    request = " ".join(args.request).strip()
    if not request:
        console.error("Please describe what to build, e.g.  blendahbot \"a neon city street at night\"")
        return 2

    config = _config_from_args(args, request)

    console.rule("blendahbot")
    console.info(f"request: {request}")
    console.info(f"run dir: {config.run_dir}")

    # Ensure a subscription credential is in place (one-time browser approve).
    if auth.ensure(config.cli_path, console) != 0:
        console.error("Could not establish authentication.")
        console.info(AUTH_REMEDIATION)
        return 3

    try:
        result = asyncio.run(build(config, console))
    except KeyboardInterrupt:
        console.warn("interrupted by user.")
        return 130
    except AuthError as ex:
        console.error("authentication failed.")
        console.info(str(ex))
        return 3
    except (DiscoveryError, BlenderUnavailable) as ex:
        console.error(str(ex))
        return 2
    except Exception as ex:  # noqa: BLE001 - surface a clean message, details in logs
        console.error(f"build failed: {ex}")
        return 1

    console.rule("done")
    status = "SATISFIED ✓" if result.satisfied else f"stopped: {result.outcome}"
    console.info(f"outcome: {status}")
    console.info(f"rounds: {len(result.rounds)} | total cost: ${result.total_cost:.3f}")
    if result.final_render:
        console.success(f"final render: {result.final_render}")
    if result.blend_path:
        console.success(f"saved .blend: {result.blend_path}")
    console.info(f"report: {config.run_dir / 'report.md'}")
    return 0 if result.satisfied else 1


if __name__ == "__main__":
    sys.exit(main())

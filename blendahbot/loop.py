"""The build orchestrator: preflight, the round loop, the quality gate, finalize."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from .artifacts import Transcript, collect_pngs, write_report
from .blender import BlenderClient, BlenderUnavailable
from .builder import RoundResult, Verdict, run_critic, run_round
from .config import BotConfig
from .discovery import DiscoveryError, find_blender_mcp_command, find_claude_cli
from .errors import AUTH_REMEDIATION, AuthError
from .options import build_builder_options
from .prompts import first_round_prompt, revision_prompt
from .refs import fetch_references
from .skills import ensure_seed_skills
from .steering import Steering
from .tools import CompletionState, make_tools_server
from .ui import Console

# Absolute backstop so an unsatisfiable goal can't loop forever even with no
# budget set. Normal stops are: satisfied, plateaued, budget, or user /stop.
_HARD_ROUND_CAP = 60


@dataclass
class Preflight:
    claude_cli: str | None = None
    blender_cmd: list[str] | None = None
    blender_ok: bool = False
    blender_detail: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return self.claude_cli is not None and self.blender_cmd is not None


@dataclass
class RunResult:
    satisfied: bool
    outcome: str
    run_dir: Path
    rounds: list[dict[str, Any]]
    total_cost: float
    blend_path: str | None = None
    final_render: str | None = None


def preflight(config: BotConfig) -> Preflight:
    """Resolve external programs and test the Blender connection. Never raises."""
    pf = Preflight()
    try:
        pf.claude_cli = find_claude_cli(config.cli_path)
    except DiscoveryError as ex:
        pf.errors.append(str(ex))
    try:
        pf.blender_cmd = find_blender_mcp_command(config.blender_mcp_cmd)
    except DiscoveryError as ex:
        pf.errors.append(str(ex))
    ok, detail = BlenderClient(config.blender_host, config.blender_port).ping()
    pf.blender_ok = ok
    pf.blender_detail = detail
    return pf


def report_preflight(pf: Preflight, console: Console) -> None:
    console.rule("preflight")
    if pf.claude_cli:
        console.success(f"claude CLI: {pf.claude_cli}")
    else:
        console.error("claude CLI not found")
    if pf.blender_cmd:
        console.success(f"blender-mcp: {' '.join(pf.blender_cmd)}")
    else:
        console.error("blender-mcp server not found")
    if pf.blender_ok:
        console.success(f"Blender connected: {pf.blender_detail}")
    else:
        console.warn(f"Blender not reachable: {pf.blender_detail}")
    for err in pf.errors:
        console.error(err)


async def build(config: BotConfig, console: Console) -> RunResult:
    """Run the full build loop. Returns a :class:`RunResult`."""
    pf = preflight(config)
    report_preflight(pf, console)
    if not pf.ready:
        raise DiscoveryError("; ".join(pf.errors) or "preflight failed")
    if not pf.blender_ok and config.require_blender:
        raise BlenderUnavailable(
            f"Blender is not reachable ({pf.blender_detail}). Open Blender, enable the "
            "Blender MCP add-on and start its server, then retry. (Use --allow-no-blender "
            "to proceed anyway.)"
        )

    blender = BlenderClient(config.blender_host, config.blender_port)
    config.run_dir.mkdir(parents=True, exist_ok=True)
    (config.run_dir / "request.txt").write_text(config.request, encoding="utf-8")
    rounds: list[dict[str, Any]] = []
    total_cost = 0.0
    satisfied = False
    final_render: Path | None = None
    last_verdict: Verdict | None = None
    outcome = "exhausted rounds without reaching the quality bar"

    transcript: Transcript | None = None
    stderr_log = None
    client: ClaudeSDKClient | None = None
    steering = Steering(asyncio.get_running_loop(), enabled=config.steer)

    try:
        transcript = Transcript(config.transcript_path)
        stderr_log = (config.run_dir / "claude_stderr.log").open("a", encoding="utf-8")

        def stderr_cb(line: str) -> None:
            stderr_log.write(line + "\n")
            stderr_log.flush()
            if config.verbose:
                console.thinking(f"[cli stderr] {line}")

        state = CompletionState()
        tools_server = make_tools_server(state)
        options = build_builder_options(config, tools_server, stderr_cb)

        client = ClaudeSDKClient(options)
        await client.connect()

        steering.start()
        if steering.enabled:
            console.info("💬 Steering on: type instructions any time + Enter to redirect. '/stop' to finish early.")

        # Seed the modelling skills library (idempotent) so the builder can consult it.
        ensure_seed_skills()

        # Ground the build in real reference photos before round 1.
        reference_dir = config.run_dir / "reference"
        reference_paths: list[Path] = []
        if config.refs > 0:
            console.info(f"gathering up to {config.refs} reference image(s)…")
            reference_paths = await asyncio.to_thread(
                fetch_references, config.request, reference_dir, config.refs
            )
            if reference_paths:
                console.success(f"got {len(reference_paths)} reference image(s) → {reference_dir}")
            else:
                console.warn("no references fetched; the builder will research on its own.")
        ref_dir_str = str(reference_dir).replace("\\", "/")
        ref_path_strs = [str(p).replace("\\", "/") for p in reference_paths]

        best_score = -1
        best_render: Path | None = None
        stale_rounds = 0
        n = 0
        cap_label = str(config.max_rounds) if config.max_rounds else "∞"
        while True:
            n += 1
            if config.max_rounds is not None and n > config.max_rounds:
                outcome = f"reached the {config.max_rounds}-round limit"
                break
            if n > _HARD_ROUND_CAP:
                outcome = f"stopped at the {_HARD_ROUND_CAP}-round safety cap"
                console.warn(outcome)
                break

            console.rule(f"round {n} / {cap_label}")
            round_dir = config.round_dir(n)
            round_dir.mkdir(parents=True, exist_ok=True)
            render_path = round_dir / "render.png"
            state.reset()

            if n == 1 or last_verdict is None:
                prompt = first_round_prompt(
                    config.request, str(render_path).replace("\\", "/"), ref_dir_str, ref_path_strs
                )
            else:
                prompt = revision_prompt(
                    config.request, str(render_path).replace("\\", "/"), last_verdict, ref_dir_str
                )

            # Fold in anything typed between rounds.
            pending = steering.drain()
            if pending:
                prompt += "\n\n[The user also added these instructions:]\n" + "\n".join(pending)

            result = await run_round(client, prompt, console, transcript, steering)
            total_cost += result.cost_usd or 0.0

            if result.auth_failed:
                raise AuthError(AUTH_REMEDIATION)

            if result.user_stopped or steering.stop_requested:
                console.warn("stopping at your request.")
                images = _gather_images(blender, state, render_path, round_dir, console)
                if images:
                    best_render = images[0]
                outcome = "stopped by user"
                break

            stop = _interpret_result(result, console)
            if stop == "fatal":
                outcome = f"builder failed to start ({result.subtype})"
                raise RuntimeError(
                    "The builder agent did not run any turns. This usually means the "
                    "claude CLI could not start or authenticate. See claude_stderr.log."
                )

            images = _gather_images(blender, state, render_path, round_dir, console)

            if stop == "budget":
                # The hard USD cap is reached; don't spend more on a critic pass.
                verdict = Verdict(
                    satisfied=False,
                    score=state.self_score,
                    summary="Stopped before review: USD budget cap reached.",
                )
            else:
                digest = _safe_digest(blender)
                verdict = await _judge(
                    config, images, digest, round_dir, state, console, transcript,
                    stderr_cb, reference_paths, best_render,
                )
            last_verdict = verdict
            total_cost += verdict.cost_usd  # the critic costs money too

            render_str = str(images[0].resolve()) if images else None
            record = {
                "round": n,
                "self_score": state.self_score,
                "summary": state.summary or verdict.summary,
                "critic_score": verdict.score,
                "satisfied": verdict.satisfied,
                "issues": verdict.issues,
                "render": render_str,
            }
            rounds.append(record)
            (round_dir / "verdict.json").write_text(
                json.dumps(record, indent=2, default=str), encoding="utf-8"
            )
            _report_verdict(verdict, console)

            # Keep the best result so we return it even if a later round regresses.
            if verdict.score > best_score:
                best_score = verdict.score
                if images:
                    best_render = images[0]
                stale_rounds = 0
            else:
                stale_rounds += 1

            if verdict.satisfied and verdict.score >= config.score_threshold:
                satisfied = True
                outcome = "satisfied the request"
                if images:
                    best_render = images[0]
                break

            if stop == "budget":
                outcome = "stopped: budget reached"
                break

            # Plateau = "done improving". This is the main bound when no round cap
            # is set; disable with --patience 0 (then only satisfied/budget/stop end it).
            if config.patience and stale_rounds >= config.patience:
                outcome = (
                    f"stopped: quality plateaued at {best_score}/100 after "
                    f"{config.patience} rounds with no improvement"
                )
                console.warn(outcome)
                break

            # Real cumulative budget ceiling (the SDK's per-round cap isn't enough).
            if config.budget_usd and total_cost >= config.budget_usd:
                outcome = f"stopped: budget reached (spent ${total_cost:.2f} of ${config.budget_usd:.2f})"
                console.warn(outcome)
                break

        final_render = best_render
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if transcript is not None:
            transcript.close()
        if stderr_log is not None:
            stderr_log.close()

    blend_path = _finalize(config, blender, final_render, console)
    final_render_str = None
    if final_render and Path(final_render).exists():
        dest = config.final_dir / "final_render.png"
        try:
            config.final_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_render, dest)
            final_render_str = str(dest)
        except OSError:
            final_render_str = str(final_render)

    write_report(
        config.run_dir / "report.md",
        request=config.request,
        rounds=rounds,
        outcome=outcome,
        total_cost=total_cost,
        blend_path=blend_path,
        final_render=final_render_str,
    )

    return RunResult(
        satisfied=satisfied,
        outcome=outcome,
        run_dir=config.run_dir,
        rounds=rounds,
        total_cost=total_cost,
        blend_path=blend_path,
        final_render=final_render_str,
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _interpret_result(result: RoundResult, console: Console) -> str:
    """Classify a round's ResultMessage: '', 'budget', or 'fatal'."""
    if not result.is_error:
        return ""
    sub = (result.subtype or "").lower()
    if "budget" in sub:
        console.warn("the agent reached its USD budget cap.")
        return "budget"
    if result.num_turns == 0:
        # The builder never ran a turn — a crash/auth/transport failure. Stop
        # rather than silently churning through the remaining rounds.
        return "fatal"
    console.warn(f"round hit a limit ({result.subtype}); continuing with what exists.")
    return ""


def _gather_images(
    blender: BlenderClient,
    state: CompletionState,
    render_path: Path,
    round_dir: Path,
    console: Console,
) -> list[Path]:
    """Find renders this round; fall back to an out-of-band render if needed."""
    candidates: list[Path] = []
    if state.render_path:
        p = Path(state.render_path)
        if p.exists():
            candidates.append(p)
    if render_path.exists():
        candidates.append(render_path)
    candidates.extend(collect_pngs(round_dir))

    # Dedup, keep order, only existing files.
    seen: set[str] = set()
    images: list[Path] = []
    for p in candidates:
        key = str(p.resolve())
        if key not in seen and p.exists():
            seen.add(key)
            images.append(p)

    if not images:
        console.warn("no render found this round — taking a fallback render of the scene.")
        fallback = round_dir / "auto_render.png"
        try:
            ok, detail = blender.render_still(str(fallback))
            if ok:
                images.append(fallback)
                console.info(f"fallback render: {fallback}")
            else:
                console.warn(f"fallback render did not complete: {detail}")
        except BlenderUnavailable as ex:
            console.warn(f"fallback render unavailable: {ex}")
    return images


def _safe_digest(blender: BlenderClient) -> str:
    try:
        return blender.scene_digest()
    except BlenderUnavailable:
        return ""


async def _judge(
    config: BotConfig,
    images: list[Path],
    digest: str,
    round_dir: Path,
    state: CompletionState,
    console: Console,
    transcript: Transcript,
    stderr_cb,
    reference_paths: list[Path] | None = None,
    prev_render: Path | None = None,
) -> Verdict:
    if not config.use_critic:
        # Trust the builder's own declaration — but only with a render to show.
        if not images:
            return Verdict(
                satisfied=False,
                score=min(state.self_score, 40),
                summary="Builder declared done but produced no render.",
                issues=["The round produced no viewable render of the scene."],
                suggestions=["Render the scene to the requested path and verify the file exists."],
            )
        return Verdict(
            satisfied=state.declared and state.self_score >= config.score_threshold,
            score=state.self_score,
            summary=state.summary or "(no critic; builder self-assessment)",
        )
    if not images:
        return Verdict(
            satisfied=False,
            score=min(state.self_score, 40),
            summary="No render was available for review.",
            issues=["The round produced no viewable render of the scene."],
            suggestions=["Render the scene to the requested path and verify it visually."],
        )
    console.info("handing the render to an independent reviewer…")
    # Sandbox the critic: copy ONLY the images into a clean review dir it can see,
    # so it can't read the builder's scripts/logs/self-assessment and be influenced.
    review_dir = round_dir / "review"
    clean_images, clean_refs, prev_clean = _prep_review_dir(
        review_dir, images, reference_paths, prev_render
    )
    console.info("handing the render to an independent reviewer…")
    return await run_critic(
        config, config.request, clean_images, digest, review_dir, console, transcript,
        stderr_cb, clean_refs, prev_clean,
    )


def _prep_review_dir(
    review_dir: Path,
    images: list[Path],
    reference_paths: list[Path] | None,
    prev_render: Path | None,
) -> tuple[list[Path], list[Path], Path | None]:
    """Copy just the images the critic should see into an isolated review folder."""
    review_dir.mkdir(parents=True, exist_ok=True)
    clean_images: list[Path] = []
    for i, p in enumerate(images):
        dest = review_dir / f"render_{i:02d}{p.suffix or '.png'}"
        try:
            shutil.copy2(p, dest)
            clean_images.append(dest)
        except OSError:
            clean_images.append(p)
    clean_refs: list[Path] = []
    for i, p in enumerate(reference_paths or []):
        src = Path(p)
        try:
            dest = review_dir / f"reference_{i:02d}{src.suffix or '.jpg'}"
            shutil.copy2(src, dest)
            clean_refs.append(dest)
        except OSError:
            pass
    prev_clean: Path | None = None
    if prev_render is not None:
        src = Path(prev_render)
        if src.exists():
            try:
                dest = review_dir / f"previous_best{src.suffix or '.png'}"
                shutil.copy2(src, dest)
                prev_clean = dest
            except OSError:
                prev_clean = None
    return clean_images, clean_refs, prev_clean


def _report_verdict(verdict: Verdict, console: Console) -> None:
    state = "SATISFIED" if verdict.satisfied else "not satisfied"
    console.info(f"reviewer: {verdict.score}/100 — {state}. {verdict.summary}")
    for issue in verdict.issues[:6]:
        console.warn(f"  · {issue}")


def _finalize(
    config: BotConfig, blender: BlenderClient, final_render: Path | None, console: Console
) -> str | None:
    """Save the .blend out-of-band so the work is never lost."""
    config.final_dir.mkdir(parents=True, exist_ok=True)
    blend_path = config.final_dir / "scene.blend"
    try:
        if blender.save_blend(str(blend_path)):
            console.success(f"saved scene: {blend_path}")
            return str(blend_path)
        console.warn("could not save the .blend (Blender may be unavailable).")
    except BlenderUnavailable as ex:
        console.warn(f"could not save the .blend: {ex}")
    return None

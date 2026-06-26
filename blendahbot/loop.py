"""The build orchestrator: preflight, the round loop, the quality gate, finalize."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from . import settings
from .artifacts import Transcript, collect_pngs, write_report
from .blender import BlenderClient, BlenderUnavailable, launch_blender
from .builder import RoundResult, Verdict, run_critic, run_round
from .config import BotConfig
from .discovery import (
    DiscoveryError,
    find_blender_executable,
    find_blender_mcp_command,
    find_claude_cli,
)
from .errors import AUTH_REMEDIATION, AuthError
from .options import build_builder_options
from .prompts import first_round_prompt, revision_prompt
from .refs import (
    fetch_references,
    ingest_user_references,
    parse_path_tokens,
    resolve_reference_specs,
)
from .skills import ensure_seed_skills
from .steering import Steering
from .supervisor import BlenderSupervisor
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


def ensure_blender(
    config: BotConfig, pf: Preflight, console: Console, supervisor: BlenderSupervisor
) -> Preflight:
    """Open Blender if it isn't running, wait for it, and remember where it lives.

    Mutates and returns ``pf`` with a refreshed Blender status, and hands the
    launched process to ``supervisor`` so it can be restarted later. A no-op when
    Blender is already reachable or auto-launch is disabled. Never raises — on any
    failure it leaves ``pf.blender_ok`` False for the caller to handle.
    """
    if pf.blender_ok or not config.auto_launch_blender:
        return pf

    try:
        exe = find_blender_executable(config.blender_path)
    except DiscoveryError as ex:
        console.warn(str(ex))
        return pf

    console.info(f"Blender isn't running — opening it: {exe}")
    try:
        proc = launch_blender(exe, config.blender_port)
    except OSError as ex:
        console.warn(f"could not launch Blender: {ex}")
        return pf
    supervisor.adopt(proc, exe)

    # Remember where we found it (only if the user hadn't pinned a path already).
    if config.blender_path is None:
        try:
            if settings.remember_blender_path(exe):
                console.info(f"saved Blender location for next time → {settings.settings_path()}")
        except OSError:
            pass

    console.info("waiting for Blender to start its MCP server…")
    ok, detail = supervisor.client.wait_until_ready(timeout=config.blender_launch_timeout)
    pf.blender_ok = ok
    pf.blender_detail = detail
    if ok:
        console.success(f"Blender connected: {detail}")
    else:
        console.warn(
            "Blender is open but its MCP server isn't reachable yet. In Blender, open the "
            "sidebar (press N) → BlenderMCP and click 'Start MCP Server', then retry."
        )
    return pf


async def build(config: BotConfig, console: Console) -> RunResult:
    """Run the full build loop. Returns a :class:`RunResult`."""
    pf = preflight(config)
    report_preflight(pf, console)
    supervisor = BlenderSupervisor(config, console)
    if not pf.blender_ok and config.require_blender:
        pf = ensure_blender(config, pf, console, supervisor)
    if not pf.ready:
        raise DiscoveryError("; ".join(pf.errors) or "preflight failed")
    if not pf.blender_ok and config.require_blender:
        raise BlenderUnavailable(
            f"Blender is not reachable ({pf.blender_detail}). Open Blender, enable the "
            "Blender MCP add-on and start its server, then retry. (Use --allow-no-blender "
            "to proceed anyway.)"
        )

    blender = supervisor.client
    # Keep Blender alive across the build: probe it between rounds and restart a
    # crashed/hung instance, reopening this checkpoint so progress isn't lost.
    restart_enabled = config.auto_restart_blender and (config.require_blender or pf.blender_ok)
    checkpoint_path = config.run_dir / "checkpoint.blend"
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

        # Seed the modelling skills library (idempotent) so the builder can consult it.
        ensure_seed_skills()

        # Ground the build in references before round 1. Ask for the user's OWN
        # images first — while stdin is still ours, before steering claims it —
        # then top up with fetched stock photos.
        reference_dir = config.run_dir / "reference"
        user_refs = await _collect_user_references(config, reference_dir, console)

        # Live steering owns stdin from here on, so only start it once the
        # reference prompt has returned.
        steering.start()
        if steering.enabled:
            console.info("💬 Steering on: type instructions any time + Enter to redirect. '/stop' to finish early.")

        web_refs: list[Path] = []
        if config.refs > 0:
            console.info(f"gathering up to {config.refs} reference image(s)…")
            web_refs = await asyncio.to_thread(
                fetch_references, config.request, reference_dir, config.refs
            )
            if web_refs:
                console.success(f"got {len(web_refs)} reference image(s) → {reference_dir}")
            elif not user_refs:
                console.warn("no references fetched; the builder will research on its own.")
        reference_paths = user_refs + web_refs
        ref_dir_str = str(reference_dir).replace("\\", "/")
        ref_path_strs = [str(p).replace("\\", "/") for p in reference_paths]
        user_ref_strs = [str(p).replace("\\", "/") for p in user_refs]

        best_score = -1
        best_render: Path | None = None
        stale_rounds = 0
        n = 0
        pending_restart_note = False
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

            # Don't spend a round's agent turns on a Blender that died since last
            # round — probe it first and restart (reopening the checkpoint) if needed.
            if restart_enabled:
                healthy, restarted = supervisor.ensure_healthy(
                    checkpoint=checkpoint_path if checkpoint_path.exists() else None
                )
                pending_restart_note = pending_restart_note or restarted
                if not healthy and config.require_blender:
                    outcome = "stopped: Blender was unavailable and could not be restarted"
                    console.error(outcome)
                    break
                if not healthy:
                    # --allow-no-blender: the user opted to tolerate a missing Blender,
                    # so degrade to running without it rather than hard-stopping.
                    console.warn("Blender is unavailable; continuing without it.")

            if n == 1 or last_verdict is None:
                prompt = first_round_prompt(
                    config.request, str(render_path).replace("\\", "/"), ref_dir_str,
                    ref_path_strs, user_ref_strs,
                )
            else:
                prompt = revision_prompt(
                    config.request, str(render_path).replace("\\", "/"), last_verdict, ref_dir_str
                )

            # Tell the builder when the scene was just rolled back by a restart, so
            # it re-checks state instead of assuming an interrupted step survived.
            if pending_restart_note:
                prompt += (
                    "\n\n[System note: Blender was just restarted (it had crashed or stopped "
                    "responding) and the scene was restored from the last saved checkpoint. "
                    "Work from an interrupted step may be missing — inspect the current scene "
                    "before continuing.]"
                )
                pending_restart_note = False

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

            # If Blender died mid-build, restart it now — before the out-of-band
            # render/digest below, which would otherwise block on the dead socket.
            blender_alive = True
            scene_rolled_back = False
            if restart_enabled:
                blender_alive, restarted = supervisor.ensure_healthy(
                    checkpoint=checkpoint_path if checkpoint_path.exists() else None
                )
                pending_restart_note = pending_restart_note or restarted
                # A successful restart reopened the checkpoint, so the LIVE scene is
                # rolled back to the end of the last good round — this round's render
                # no longer reflects what's in Blender.
                scene_rolled_back = restarted and blender_alive
                if not blender_alive:
                    console.warn("Blender is unavailable and could not be restarted; "
                                 "finishing with whatever render already exists.")

            # A restart rolled the scene back, so this round's render is stale: don't
            # bank it as a result (and don't bother gathering it). Record the
            # interruption and let the agent rebuild from the restored scene next round.
            if scene_rolled_back:
                console.warn("scene was rolled back to the last checkpoint; redoing this round.")
                rounds.append({
                    "round": n,
                    "self_score": state.self_score,
                    "summary": "Blender restarted mid-round; scene rolled back to the last "
                               "checkpoint and the round will be redone.",
                    "critic_score": 0,
                    "satisfied": False,
                    "issues": [],
                    "render": None,
                })
                if config.budget_usd and total_cost >= config.budget_usd:
                    outcome = f"stopped: budget reached (spent ${total_cost:.2f} of ${config.budget_usd:.2f})"
                    console.warn(outcome)
                    break
                continue

            # When Blender is dead, use only renders already on disk — never trigger the
            # out-of-band fallback render, which would block on the dead socket (~300s).
            images = _gather_images(
                blender, state, render_path, round_dir, console, allow_fallback=blender_alive
            )

            # Checkpoint the (healthy) scene so a later crash/hang can resume from here.
            if blender_alive and stop != "budget":
                _save_checkpoint(blender, checkpoint_path)

            if stop == "budget":
                # The hard USD cap is reached; don't spend more on a critic pass.
                verdict = Verdict(
                    satisfied=False,
                    score=state.self_score,
                    summary="Stopped before review: USD budget cap reached.",
                )
            else:
                # Skip the scene digest when Blender is dead — it would block on the
                # socket; the critic can still judge the render we already have.
                digest = _safe_digest(blender) if blender_alive else ""
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

            # Blender is dead and unrecoverable: we've captured this round's render
            # above, so stop now rather than churn more rounds against a dead socket.
            if not blender_alive:
                outcome = "stopped: Blender was unavailable and could not be restarted"
                console.error(outcome)
                break

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

async def _collect_user_references(
    config: BotConfig, reference_dir: Path, console: Console
) -> list[Path]:
    """Gather the user's OWN reference images before fetching stock photos.

    Uses CLI-supplied paths (``--ref``) when given; otherwise asks interactively,
    but only when stdin is a real terminal (so scheduled/piped runs don't block).
    Returns the files copied into ``reference_dir`` (possibly empty).
    """
    if config.ref_paths:
        images, unusable = resolve_reference_specs(config.ref_paths)
        for bad in unusable:
            console.warn(f"--ref ignored (not an image file/folder): {bad}")
        saved = await asyncio.to_thread(ingest_user_references, images, reference_dir)
        if saved:
            console.success(f"using {len(saved)} reference image(s) you provided.")
        return saved
    if config.ask_refs and _stdin_is_tty():
        return await asyncio.to_thread(_prompt_user_references, reference_dir, console)
    return []


def _stdin_is_tty() -> bool:
    """True only when stdin is an interactive terminal we can safely prompt on."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def _prompt_user_references(reference_dir: Path, console: Console) -> list[Path]:
    """Ask, on the terminal, for the user's own reference image files (blocking)."""
    console.info(
        "Got any reference images for this build? Drag in or paste file paths "
        "(a folder works too), then press Enter. Blank line = skip / done."
    )
    saved: list[Path] = []
    while True:
        try:
            line = input("  reference> ").strip()
        except EOFError:
            break
        if not line:
            break
        images, unusable = resolve_reference_specs(parse_path_tokens(line))
        if not images and unusable:
            # An unquoted path with spaces would split badly — retry the whole line.
            retry_images, retry_unusable = resolve_reference_specs([line])
            if retry_images:
                images, unusable = retry_images, retry_unusable
        for bad in unusable:
            console.warn(f"skipped (not an image file/folder): {bad}")
        if images:
            new = ingest_user_references(images, reference_dir, start_index=len(saved))
            saved.extend(new)
            console.success(f"added {len(new)} image(s) — {len(saved)} reference(s) so far.")
    if saved:
        console.info(f"using {len(saved)} of your reference image(s) → {reference_dir}")
    return saved


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
    allow_fallback: bool = True,
) -> list[Path]:
    """Find renders this round; fall back to an out-of-band render if needed.

    ``allow_fallback=False`` skips the out-of-band render entirely (used when Blender
    is known dead, so we don't block on the socket) — only on-disk renders are used.
    """
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

    if not images and not allow_fallback:
        console.warn("no render found this round and Blender is unavailable — skipping fallback render.")
        return images

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


def _save_checkpoint(blender: BlenderClient, path: Path) -> bool:
    """Best-effort out-of-band save so a later restart can resume from here.

    Silent on success (it runs every round); a failure just means the previous
    checkpoint stands. Never raises.
    """
    try:
        return blender.save_blend(str(path))
    except BlenderUnavailable:
        return False


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

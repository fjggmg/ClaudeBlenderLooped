"""Opt-in: vet a generated asset with an INDEPENDENT critic and auto-regenerate.

The default preview gate (see :mod:`blendahbot.gen3d.preview`) has the builder
agent look at an isolated contact sheet and decide for itself. This module adds the
robust, autonomous tier the builder can opt into (``gen3d --vet`` / a build run with
``--vet-assets``): a sandboxed, read-only critic — the asset-stage twin of
``builder.run_critic`` — judges the isolated preview against the intended prompt and
returns a strict-JSON verdict, wrapped in a bounded ACCEPT / REGENERATE / keep-best
loop that reseeds (and lightly re-prompts) until the asset passes or attempts run out.

It does NOT rubber-stamp the generator: the critic is a fresh ``query()`` that can
only Read the preview images, mirroring the independent full-scene critic's
isolation discipline. A free geometry heuristic (:func:`heuristic_floor`) rejects
obvious garbage first, so the paid model call only runs on plausible assets.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .base import Gen3DError, GenRequest, GenResult, ProgressFn, get_backend
from .preview import AssetStats, heuristic_floor, render_isolated_preview


@dataclass
class VetResult:
    result: GenResult
    verdict: "object | None"  # builder.Verdict, imported lazily to avoid eager SDK load
    attempts: list[dict] = field(default_factory=list)
    vetted: bool = False


def tweak_prompt(prompt: str, suggestions: list[str], *, limit: int = 60) -> str:
    """Fold the critic's hint into a fresh, short, front-loaded regeneration prompt.

    The local text->3D server truncates to ~60 chars and an unlucky seed is often the
    real culprit, so this stays conservative: if the hint just says reseed, keep the
    prompt unchanged (the loop varies the seed); otherwise nudge it toward a single
    clean object and re-truncate from the front.
    """
    hint = " ".join(s.strip() for s in (suggestions or []) if s and s.strip())
    base = prompt.strip().rstrip(".,")
    if not hint or "reseed" in hint.lower():
        return base[:limit].strip().rstrip(",")
    merged = base if "single object" in base.lower() else base + ", single object"
    return merged[:limit].strip().rstrip(",")


def _verdict_module():
    """Lazy import of the SDK-backed verdict type/parser (keeps the light path light)."""
    from ..builder import Verdict, parse_verdict

    return Verdict, parse_verdict


def _prep_review_dir(review_dir: Path, sheet_paths: list[Path], reference_paths: list[Path] | None):
    """Copy ONLY the preview images (+refs) into a clean dir the critic is sandboxed to."""
    review_dir.mkdir(parents=True, exist_ok=True)
    clean_sheets: list[Path] = []
    for i, p in enumerate(sheet_paths):
        src = Path(p)
        dest = review_dir / f"preview_{i:02d}{src.suffix or '.png'}"
        try:
            shutil.copy2(src, dest)
            clean_sheets.append(dest)
        except OSError:
            clean_sheets.append(src)
    clean_refs: list[Path] = []
    for i, p in enumerate(reference_paths or []):
        src = Path(p)
        if not src.exists():
            continue
        try:
            dest = review_dir / f"reference_{i:02d}{src.suffix or '.jpg'}"
            shutil.copy2(src, dest)
            clean_refs.append(dest)
        except OSError:
            pass
    return clean_sheets, clean_refs


def _asset_critic_prompt(intended: str, sheet_paths: list[Path], stats: AssetStats | None,
                         reference_paths: list[Path] | None = None) -> str:
    imgs = "\n".join(f"  - {Path(p).resolve()}" for p in sheet_paths) or "  (no preview images)"
    refs = ""
    if reference_paths:
        rl = "\n".join(f"  - {Path(p).resolve()}" for p in reference_paths)
        refs = ("\nReference photos of the intended subject (Read these too and compare the "
                "shape/material/proportions):\n" + rl + "\n")
    facts = "(geometry stats unavailable)"
    if stats is not None:
        facts = (f"meshes={stats.mesh_count}, faces={stats.face_count}, "
                 f"textured={stats.has_materials}, uvs={stats.has_uvs}, "
                 f"textures={stats.texture_count}@{stats.max_texture_px}px, "
                 f"bbox_aspect={stats.bbox_aspect}")
    return f"""\
A 3D mesh was just generated from this description:

    {intended}

It was rendered ALONE on a neutral backdrop from several angles. Factual geometry
from the import (judge the PIXELS, not these labels):
    {facts}

Preview images to review (use the Read tool to open and look at each):
{imgs}
{refs}
Decide whether this single generated mesh is good enough to place in a scene, and
reply with the strict JSON verdict described in your instructions.
"""


async def run_asset_critic(config, intended: str, sheet_paths: list[Path], stats: AssetStats | None,
                           review_dir: Path, stderr_cb, reference_paths: list[Path] | None = None):
    """Independent, sandboxed read-only critic for ONE isolated asset. Returns a Verdict."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

    from ..errors import AUTH_REMEDIATION, AuthError, looks_like_auth_failure
    from ..options import build_asset_critic_options

    Verdict, parse_verdict = _verdict_module()
    clean_sheets, clean_refs = _prep_review_dir(review_dir, sheet_paths, reference_paths)
    options = build_asset_critic_options(config, review_dir, stderr_cb)
    prompt = _asset_critic_prompt(intended, clean_sheets, stats, clean_refs)

    chunks: list[str] = []
    err = ""
    cost = 0.0
    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(message, ResultMessage):
                cost += message.total_cost_usd or 0.0
    except Exception as ex:  # noqa: BLE001 - classified below
        err = str(ex)
    text = "\n".join(chunks)
    if looks_like_auth_failure(text + " " + err):
        raise AuthError(AUTH_REMEDIATION)
    if err and not text.strip():
        v = Verdict(satisfied=False, score=0, summary=f"asset critic unreachable: {err}",
                    parse_failed=True, raw=err)
        v.cost_usd = cost
        return v
    verdict = parse_verdict(text)
    verdict.cost_usd = cost
    return verdict


async def vet_and_generate(
    prompt: str,
    out: str | Path,
    *,
    config,
    work_dir: str | Path,
    blender: str | None = None,
    backend: str | None = None,
    image_path: str | None = None,
    texture: bool = True,
    seed: int | None = None,
    face_count: int | None = None,
    reference_paths: list[Path] | None = None,
    max_attempts: int = 2,
    accept_threshold: int = 55,
    shots=None,
    on_progress: ProgressFn | None = None,
    stderr_cb=None,
    timeout: float = 600.0,
) -> VetResult:
    """Generate -> isolated preview -> vet -> ACCEPT / REGENERATE / keep-best, bounded.

    Reseeds each attempt (deterministically when no explicit seed is given) and folds
    the critic's hint into the prompt. Returns the accepted asset, or the best-scoring
    attempt flagged ``vetted=False`` with its unresolved issues if none passed.
    """
    Verdict, _ = _verdict_module()
    out = Path(out)
    work_dir = Path(work_dir)
    intended = prompt
    be = get_backend(backend)
    cur_prompt = prompt
    best: tuple[int, GenResult, object, list[Path]] | None = None  # (score, result, verdict, sheets)
    attempts: list[dict] = []

    for i in range(1, max(1, max_attempts) + 1):
        attempt_seed = seed if seed is not None else i  # deterministic, reproducible
        req = GenRequest(prompt=cur_prompt, image_path=image_path, texture=texture,
                         seed=attempt_seed, face_count=face_count)
        ok, reason = be.available(req)
        if not ok:
            raise Gen3DError(f"backend '{be.name}' not usable: {reason}")
        attempt_out = out if max_attempts == 1 else out.with_name(
            f"{out.stem}_try{i}{out.suffix or '.glb'}")
        attempt_out.parent.mkdir(parents=True, exist_ok=True)
        result = await asyncio.to_thread(
            be.generate, req, attempt_out, on_progress, timeout)

        preview = render_isolated_preview(result.path, work_dir / f"try{i}", blender=blender, shots=shots)
        if not preview.ok:
            verdict = Verdict(satisfied=False, score=0,
                              summary=f"could not render isolated preview: {preview.detail}",
                              issues=[preview.detail])
        else:
            passed, reasons = heuristic_floor(preview.stats, want_texture=texture)
            if not passed:
                verdict = Verdict(satisfied=False, score=10, summary="; ".join(reasons),
                                  issues=reasons, suggestions=["tighten the prompt to one clean object"])
            else:
                verdict = await run_asset_critic(
                    config, intended, preview.sheet_paths, preview.stats,
                    work_dir / f"try{i}" / "review", stderr_cb, reference_paths)

        attempts.append({
            "attempt": i, "seed": attempt_seed, "prompt": cur_prompt,
            "glb": str(result.path), "score": verdict.score,
            "satisfied": verdict.satisfied, "summary": verdict.summary,
            "issues": list(verdict.issues),
        })
        if on_progress is not None:
            verb = "ACCEPT" if (verdict.satisfied and verdict.score >= accept_threshold) else "reject"
            on_progress(f"vet attempt {i}/{max_attempts}: {verb} score={verdict.score} — {verdict.summary}")

        if best is None or verdict.score > best[0]:
            best = (verdict.score, result, verdict, preview.sheet_paths)

        if verdict.satisfied and verdict.score >= accept_threshold:
            final = _commit(result, out)
            final.vetted = True
            return VetResult(result=final, verdict=verdict, attempts=attempts, vetted=True)

        cur_prompt = tweak_prompt(prompt, getattr(verdict, "suggestions", []) or [])

    # Exhausted without an accept: return the best attempt, flagged unvetted.
    score, result, verdict, _sheets = best  # best is always set (>=1 attempt)
    final = _commit(result, out)
    final.vetted = False
    final.issues = list(getattr(verdict, "issues", []) or [])
    return VetResult(result=final, verdict=verdict, attempts=attempts, vetted=False)


def _commit(result: GenResult, out: Path) -> GenResult:
    """Copy the chosen attempt to the caller's requested path and return its GenResult."""
    src = Path(result.path)
    if src.resolve() != out.resolve():
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, out)
        except OSError:
            out = src  # fall back to the attempt path if the copy fails
    return GenResult(path=out, backend=result.backend)

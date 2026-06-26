"""Generate the same asset with several backends so the builder can pick the best.

The builder agent calls this to get two (or more) candidate meshes from different
generators, then imports each, renders them, and keeps the best — that comparison
*is* the "LLM chooses" step (the builder already has Blender + vision + auth).

Generation is **sequential on purpose**: two big local models (Hunyuan + TRELLIS)
cannot share a 16 GB GPU at once, so running them in parallel would OOM. A backend
that isn't available is skipped (with a note), and a backend that errors mid-run is
dropped rather than sinking the whole comparison.
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    Gen3DError,
    GenRequest,
    GenResult,
    ProgressFn,
    generate_asset,
    get_backend,
)


def candidate_path(out: str | Path, backend: str) -> Path:
    """`asset.glb` + backend `trellis` -> `asset.trellis.glb`."""
    out = Path(out)
    return out.with_name(f"{out.stem}.{backend}{out.suffix}")


def generate_candidates(
    prompt: str = "",
    image_path: str | None = None,
    out: str | Path = "asset.glb",
    backends: list[str] | tuple[str, ...] = (),
    texture: bool = True,
    seed: int | None = None,
    face_count: int | None = None,
    on_progress: ProgressFn | None = None,
    timeout: float = 600.0,
) -> list[GenResult]:
    """Run each backend in turn, writing a per-backend GLB; return the successes."""
    names = list(dict.fromkeys(backends))  # dedup, keep order
    if not names:
        raise Gen3DError("no backends given to --compare")

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    probe = GenRequest(
        prompt=prompt, image_path=image_path, texture=texture, seed=seed, face_count=face_count
    )

    results: list[GenResult] = []
    for name in names:
        be = get_backend(name)
        ok, reason = be.available(probe)
        if not ok:
            if on_progress:
                on_progress(f"skip {name}: {reason}")
            continue
        dest = candidate_path(out, name)
        try:
            res = generate_asset(
                prompt=prompt, image_path=image_path, out=dest, backend=name,
                texture=texture, seed=seed, face_count=face_count,
                on_progress=(lambda s, n=name: on_progress(f"{n}: {s}")) if on_progress else None,
                timeout=timeout,
            )
            results.append(res)
        except Gen3DError as ex:
            if on_progress:
                on_progress(f"{name} failed: {ex}")

    if not results:
        raise Gen3DError(
            f"no candidate succeeded from {names}. Start the relevant server(s) or check keys."
        )
    return results

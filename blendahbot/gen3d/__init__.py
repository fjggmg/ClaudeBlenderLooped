"""Generate 3D assets (text/image -> textured GLB) via a swappable backend.

The builder calls this instead of hand-modelling organic / detail-dense props:

    python -m blendahbot.gen3d "a wooden barrel" --image reference/barrel.png --out assets/barrel.glb

Backends sit behind a common ABC so API vs local is interchangeable:
- ``hunyuan``  — a local Hunyuan3D-2.1 server on your GPU (default; free, offline, true PBR).
- ``trellis``  — a local TRELLIS.2 server (MIT; newer geometry + PBR, image->3D only).
- ``hunyuan3`` — hosted Hunyuan3D 3.1 via Replicate (needs REPLICATE_API_TOKEN); the
                 "max-quality" tier — newer geometry + PBR, text+image, costs credits.
- ``tripo``    — the Tripo hosted API (needs TRIPO_API_KEY); good text+image fallback.

``generate_candidates`` runs several backends in turn (e.g. hunyuan + trellis) so the
builder can render each and keep the best.
"""

from .base import Gen3DBackend, Gen3DError, GenRequest, GenResult, generate_asset, get_backend
from .compare import candidate_path, generate_candidates

__all__ = [
    "generate_asset",
    "generate_candidates",
    "candidate_path",
    "get_backend",
    "Gen3DBackend",
    "GenRequest",
    "GenResult",
    "Gen3DError",
    # preview gate (lazy: see below)
    "render_isolated_preview",
    "PreviewResult",
    "AssetStats",
    "heuristic_floor",
    "parse_preview_stats",
    # vet loop (lazy: pulls in the Agent SDK only when used)
    "vet_and_generate",
    "run_asset_critic",
    "tweak_prompt",
    "VetResult",
]

# Resolve preview/vet symbols lazily (PEP 562). Two reasons: (1) keeps `import
# blendahbot.gen3d` from eagerly importing the submodules — so `python -m
# blendahbot.gen3d.preview` doesn't trip runpy's "already in sys.modules" warning;
# (2) the vet loop pulls in the Agent SDK (builder/options), which the common
# generate/preview path should not pay for.
_LAZY = {
    "render_isolated_preview": "preview",
    "PreviewResult": "preview",
    "AssetStats": "preview",
    "heuristic_floor": "preview",
    "parse_preview_stats": "preview",
    "vet_and_generate": "vet",
    "run_asset_critic": "vet",
    "tweak_prompt": "vet",
    "VetResult": "vet",
}


def __getattr__(name: str):  # PEP 562 module-level lazy attribute
    mod = _LAZY.get(name)
    if mod is not None:
        from importlib import import_module

        return getattr(import_module(f"{__name__}.{mod}"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

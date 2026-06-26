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
from .preview import (
    AssetStats,
    PreviewResult,
    heuristic_floor,
    parse_preview_stats,
    render_isolated_preview,
)

__all__ = [
    "generate_asset",
    "generate_candidates",
    "candidate_path",
    "get_backend",
    "Gen3DBackend",
    "GenRequest",
    "GenResult",
    "Gen3DError",
    # preview gate (light: no SDK import)
    "render_isolated_preview",
    "PreviewResult",
    "AssetStats",
    "heuristic_floor",
    "parse_preview_stats",
    # vet loop (lazy below: pulls in the Agent SDK only when used)
    "vet_and_generate",
    "run_asset_critic",
    "tweak_prompt",
    "VetResult",
]

# The vet loop imports the Agent SDK (via builder/options). Expose it lazily so the
# common `import blendahbot.gen3d` / preview-only path stays SDK-free and fast.
_LAZY = {"vet_and_generate", "run_asset_critic", "tweak_prompt", "VetResult"}


def __getattr__(name: str):  # PEP 562 module-level lazy attribute
    if name in _LAZY:
        from . import vet

        return getattr(vet, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

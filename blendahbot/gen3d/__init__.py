"""Generate 3D assets (text/image -> textured GLB) via a swappable backend.

The builder calls this instead of hand-modelling organic / detail-dense props:

    python -m blendahbot.gen3d "a wooden barrel" --image reference/barrel.png --out assets/barrel.glb

Backends sit behind a common ABC so API vs local is interchangeable:
- ``hunyuan`` — a local Hunyuan3D-2.1 server on your GPU (default; free, offline, true PBR).
- ``tripo``   — the Tripo hosted API (needs TRIPO_API_KEY); good text+image fallback.
"""

from .base import Gen3DBackend, Gen3DError, GenRequest, GenResult, generate_asset, get_backend

__all__ = [
    "generate_asset",
    "get_backend",
    "Gen3DBackend",
    "GenRequest",
    "GenResult",
    "Gen3DError",
]

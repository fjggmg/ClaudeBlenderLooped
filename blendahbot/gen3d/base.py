"""Backend-agnostic 3D generation: the ABC, request/result types, and selection."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class Gen3DError(RuntimeError):
    """Raised when generation can't be performed or fails."""


@dataclass
class GenRequest:
    prompt: str = ""
    image_path: str | None = None
    texture: bool = True
    seed: int | None = None
    face_count: int | None = None


@dataclass
class GenResult:
    path: Path
    backend: str
    # Set by the opt-in vet loop (vet.py). ``vetted`` is False when an asset was
    # returned without passing the independent asset-critic (e.g. attempts ran out);
    # ``issues`` then carries the unresolved defects so the caller can react.
    vetted: bool = True
    issues: list[str] = field(default_factory=list)


ProgressFn = Callable[[str], None]


class Gen3DBackend(ABC):
    name = "base"

    @abstractmethod
    def available(self, req: GenRequest) -> tuple[bool, str]:
        """Return ``(ok, reason)`` for whether this backend can serve ``req`` now."""

    @abstractmethod
    def generate(
        self, req: GenRequest, out: Path, on_progress: ProgressFn | None = None, timeout: float = 600.0
    ) -> GenResult:
        """Generate an asset and write a GLB to ``out``. Raise Gen3DError on failure."""


def _backends() -> dict[str, Gen3DBackend]:
    from .hunyuan3d import Hunyuan3DReplicateBackend
    from .hunyuan_local import HunyuanLocalBackend
    from .trellis_local import TrellisLocalBackend
    from .tripo import TripoBackend

    return {
        "hunyuan": HunyuanLocalBackend(),
        "trellis": TrellisLocalBackend(),
        "hunyuan3": Hunyuan3DReplicateBackend(),
        "tripo": TripoBackend(),
    }


def get_backend(name: str | None = None) -> Gen3DBackend:
    """Pick a backend: explicit name -> $BLENDAHBOT_GEN3D_BACKEND -> local-then-API auto."""
    backends = _backends()
    name = name or os.environ.get("BLENDAHBOT_GEN3D_BACKEND")
    if name:
        if name not in backends:
            raise Gen3DError(f"unknown backend '{name}'; choose from {sorted(backends)}")
        return backends[name]

    # Auto: prefer the free local Hunyuan server if it answers, then the hosted fallbacks
    # whose keys are set (Tripo, then Hunyuan3D 3.1). Hosted backends cost credits, so they
    # only activate when their key is present and the local server isn't reachable.
    hy = backends["hunyuan"]
    ok, _ = hy.available(GenRequest(image_path="(probe)"))
    if ok:
        return hy
    for fallback in ("tripo", "hunyuan3"):
        ok, _ = backends[fallback].available(GenRequest(prompt="(probe)"))
        if ok:
            return backends[fallback]
    raise Gen3DError(
        "No 3D-gen backend available. Start the local Hunyuan3D server "
        "(BLENDAHBOT_HUNYUAN_URL, default http://localhost:8081), or set TRIPO_API_KEY, "
        "or set REPLICATE_API_TOKEN (for hosted Hunyuan3D 3.1, --backend hunyuan3)."
    )


def generate_asset(
    prompt: str = "",
    image_path: str | None = None,
    out: str | Path = "asset.glb",
    backend: str | None = None,
    texture: bool = True,
    seed: int | None = None,
    face_count: int | None = None,
    on_progress: ProgressFn | None = None,
    timeout: float = 600.0,
) -> GenResult:
    req = GenRequest(
        prompt=prompt, image_path=image_path, texture=texture, seed=seed, face_count=face_count
    )
    be = get_backend(backend)
    ok, reason = be.available(req)
    if not ok:
        raise Gen3DError(f"backend '{be.name}' not usable: {reason}")
    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return be.generate(req, out_path, on_progress=on_progress, timeout=timeout)

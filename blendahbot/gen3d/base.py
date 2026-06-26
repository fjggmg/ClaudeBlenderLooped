"""Backend-agnostic 3D generation: the ABC, request/result types, and selection."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
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
    from .hunyuan_local import HunyuanLocalBackend
    from .tripo import TripoBackend

    return {"hunyuan": HunyuanLocalBackend(), "tripo": TripoBackend()}


def get_backend(name: str | None = None) -> Gen3DBackend:
    """Pick a backend: explicit name -> $BLENDAHBOT_GEN3D_BACKEND -> local-then-API auto."""
    backends = _backends()
    name = name or os.environ.get("BLENDAHBOT_GEN3D_BACKEND")
    if name:
        if name not in backends:
            raise Gen3DError(f"unknown backend '{name}'; choose from {sorted(backends)}")
        return backends[name]

    # Auto: prefer the local Hunyuan server if it answers, else Tripo if a key is set.
    hy = backends["hunyuan"]
    ok, _ = hy.available(GenRequest(image_path="(probe)"))
    if ok:
        return hy
    tp = backends["tripo"]
    ok, _ = tp.available(GenRequest(prompt="(probe)"))
    if ok:
        return tp
    raise Gen3DError(
        "No 3D-gen backend available. Start the local Hunyuan3D server "
        "(BLENDAHBOT_HUNYUAN_URL, default http://localhost:8081) or set TRIPO_API_KEY."
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

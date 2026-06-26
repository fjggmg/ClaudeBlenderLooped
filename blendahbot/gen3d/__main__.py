"""CLI: generate a 3D asset and print the GLB path.

    python -m blendahbot.gen3d "a wooden barrel" --image ref.png --out assets/barrel.glb
"""

from __future__ import annotations

import argparse
import sys

from .base import Gen3DError, generate_asset


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="blendahbot.gen3d", description="Generate a 3D asset (textured GLB).")
    p.add_argument("prompt", nargs="?", default="", help="Text prompt (text->3D backends).")
    p.add_argument("--image", default=None, help="Reference image for image->3D (best quality).")
    p.add_argument("--out", default="asset.glb", help="Output .glb path (default: asset.glb).")
    p.add_argument("--backend", default=None, help="hunyuan | tripo (default: auto-detect).")
    p.add_argument("--no-texture", action="store_true", help="Geometry only, no texture.")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--face-count", type=int, default=None, help="Target face count (backend permitting).")
    p.add_argument("--timeout", type=float, default=600.0)
    args = p.parse_args(argv)

    if not args.prompt and not args.image:
        p.error("provide a text prompt or --image")

    def progress(stage: str) -> None:
        print(f"[gen3d] {stage}", file=sys.stderr, flush=True)

    try:
        result = generate_asset(
            prompt=args.prompt, image_path=args.image, out=args.out, backend=args.backend,
            texture=not args.no_texture, seed=args.seed, face_count=args.face_count,
            on_progress=progress, timeout=args.timeout,
        )
    except Gen3DError as ex:
        print(f"[gen3d] {ex}", file=sys.stderr)
        return 1
    print(result.path)  # the agent reads this from stdout
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

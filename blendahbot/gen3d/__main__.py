"""CLI: generate a 3D asset and print the GLB path.

    python -m blendahbot.gen3d "a wooden barrel" --image ref.png --out assets/barrel.glb

Inspect a generated asset IN ISOLATION before placing it (renders a multi-angle
contact sheet in a throwaway Blender scene — never touches the live scene):

    python -m blendahbot.gen3d "a wooden barrel" --out assets/barrel.glb --preview runs/.../preview

Compare backends and let the builder pick the best:

    python -m blendahbot.gen3d "a wooden barrel" --compare hunyuan,trellis --out assets/barrel.glb

Opt-in: let an INDEPENDENT critic vet the asset and auto-regenerate until it passes:

    python -m blendahbot.gen3d "a wooden barrel" --out assets/barrel.glb --vet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .base import Gen3DError, generate_asset
from .compare import generate_candidates


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="blendahbot.gen3d", description="Generate a 3D asset (textured GLB).")
    p.add_argument("prompt", nargs="?", default="", help="Text prompt (text->3D backends).")
    p.add_argument("--image", default=None, help="Reference image for image->3D (best quality).")
    p.add_argument("--out", default="asset.glb", help="Output .glb path (default: asset.glb).")
    p.add_argument("--backend", default=None,
                   help="hunyuan (local) | trellis (local, image-only) | hunyuan3 (hosted 3.1, "
                        "REPLICATE_API_TOKEN) | tripo (default: auto-detect).")
    p.add_argument("--compare", default=None,
                   help="Comma list of backends to generate candidates from (e.g. hunyuan,trellis); "
                        "writes one GLB per backend and prints a JSON manifest. Runs sequentially.")
    p.add_argument("--no-texture", action="store_true", help="Geometry only, no texture.")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--face-count", type=int, default=None, help="Target face count (backend permitting).")
    p.add_argument("--timeout", type=float, default=600.0)
    # --- inspect a generated asset in isolation before placing it ---
    p.add_argument("--preview", default=None, metavar="DIR",
                   help="After generating, render a multi-angle preview of the asset ALONE into DIR "
                        "(throwaway Blender scene; live scene untouched). Prints the PNG paths to look at.")
    p.add_argument("--preview-shots", default=None,
                   help="Comma list of catalog labels or label:az:el for --preview "
                        "(default: front,side,back_3q,high_3q).")
    p.add_argument("--vet", action="store_true",
                   help="Opt-in: render the isolated preview, have an INDEPENDENT critic judge it, and "
                        "auto-regenerate (new seed / tightened prompt) until it passes or attempts run out.")
    p.add_argument("--vet-attempts", type=int, default=2, help="Max generate+vet attempts (default 2).")
    p.add_argument("--vet-accept", type=int, default=55, help="Critic score to accept an asset (default 55).")
    p.add_argument("--vet-shots", default=None, help="Override preview shots used while vetting.")
    args = p.parse_args(argv)

    if not args.prompt and not args.image:
        p.error("provide a text prompt or --image")

    def progress(stage: str) -> None:
        print(f"[gen3d] {stage}", file=sys.stderr, flush=True)

    if args.prompt and len(args.prompt) > 65 and args.backend in (None, "hunyuan"):
        print("[gen3d] note: keep prompts short + front-loaded — the local model truncates to "
              "~60 chars (put object, material, then key feature first).", file=sys.stderr, flush=True)

    if args.compare:
        return _run_compare(args, progress)

    if args.vet:
        return _run_vet(args, progress)

    try:
        result = generate_asset(
            prompt=args.prompt, image_path=args.image, out=args.out, backend=args.backend,
            texture=not args.no_texture, seed=args.seed, face_count=args.face_count,
            on_progress=progress, timeout=args.timeout,
        )
    except Gen3DError as ex:
        print(f"[gen3d] {ex}", file=sys.stderr)
        return 1

    if args.preview:
        _preview_one(_make_client(args), result.path, args.preview, args.preview_shots)
    print(result.path)  # the agent reads this from stdout
    return 0


def _run_compare(args: argparse.Namespace, progress) -> int:
    backends = [b.strip() for b in args.compare.split(",") if b.strip()]
    try:
        results = generate_candidates(
            prompt=args.prompt, image_path=args.image, out=args.out, backends=backends,
            texture=not args.no_texture, seed=args.seed, face_count=args.face_count,
            on_progress=progress, timeout=args.timeout,
        )
    except Gen3DError as ex:
        print(f"[gen3d] {ex}", file=sys.stderr)
        return 1

    entries: list[dict] = []
    client = _make_client(args) if args.preview else None
    for r in results:
        entry = {"backend": r.backend, "path": str(r.path)}
        if client is not None:
            pv = _preview_one(client, r.path, str(Path(args.preview) / r.backend), args.preview_shots)
            entry["preview_images"] = [str(sp) for sp in pv.sheet_paths]
        entries.append(entry)
    # The builder reads this: import each candidate, render, keep the best, delete the rest.
    print(json.dumps({"candidates": entries}))
    return 0


def _run_vet(args: argparse.Namespace, progress) -> int:
    import asyncio

    from ..config import BotConfig
    from ..errors import AuthError
    from .preview import _parse_shot_arg
    from .vet import vet_and_generate

    out = Path(args.out)
    work_dir = Path(args.preview) if args.preview else out.with_name(out.stem + "_vet")
    config = BotConfig.from_env("(gen3d vet)")
    try:
        vr = asyncio.run(vet_and_generate(
            args.prompt, args.out,
            client=_make_client(args), config=config, work_dir=work_dir,
            backend=args.backend, image_path=args.image, texture=not args.no_texture,
            seed=args.seed, face_count=args.face_count,
            max_attempts=args.vet_attempts, accept_threshold=args.vet_accept,
            shots=_parse_shot_arg(args.vet_shots or args.preview_shots),
            on_progress=progress, stderr_cb=lambda _line: None, timeout=args.timeout,
        ))
    except AuthError as ex:
        print(f"[gen3d] vet needs a logged-in Claude session: {ex}", file=sys.stderr)
        return 1
    except Gen3DError as ex:
        print(f"[gen3d] {ex}", file=sys.stderr)
        return 1

    if vr.vetted:
        score = vr.verdict.score if vr.verdict else "?"
        print(f"[gen3d] vetted OK (score {score}) after {len(vr.attempts)} attempt(s)", file=sys.stderr)
    else:
        summary = vr.verdict.summary if vr.verdict else "no verdict"
        print(f"[gen3d] UNVETTED after {len(vr.attempts)} attempt(s): {summary}", file=sys.stderr)
        for iss in (vr.result.issues or [])[:4]:
            print(f"[gen3d]   - {iss}", file=sys.stderr)
    print(vr.result.path)  # stdout contract preserved
    return 0 if vr.vetted else 0  # an unvetted-but-produced asset is not a hard failure


def _make_client(args: argparse.Namespace):
    from ..blender import BlenderClient

    host = os.environ.get("BLENDER_MCP_HOST", "localhost")
    port = int(os.environ.get("BLENDER_MCP_PORT", "9876"))
    return BlenderClient(host, port, timeout=max(args.timeout, 300.0))


def _preview_one(client, glb, out_dir: str, shots_arg: str | None):
    from .preview import _parse_shot_arg, render_isolated_preview

    pv = render_isolated_preview(client, glb, out_dir, shots=_parse_shot_arg(shots_arg))
    if pv.ok:
        print(f"PREVIEW: {out_dir} ({len(pv.sheet_paths)} images — Read them to vet the asset)",
              file=sys.stderr)
        for sp in pv.sheet_paths:
            print(f"  {sp}", file=sys.stderr)
        if pv.stats is not None:
            s = pv.stats
            print(f"[gen3d] preview stats: meshes={s.mesh_count} faces={s.face_count} "
                  f"textured={s.has_materials} aspect={s.bbox_aspect}", file=sys.stderr)
    else:
        print(f"[gen3d] preview failed: {pv.detail}", file=sys.stderr)
    return pv


if __name__ == "__main__":
    raise SystemExit(main())

"""Render a generated GLB *in isolation* so the bot can vet it before placing it.

A freshly generated mesh (especially text->3D) is unpredictable: it may be the
wrong object, a blobby/holed reconstruction, a whole *scene* baked into one
"object", garbled-texture, or wrongly proportioned. Today the builder imports it
into the live scene blind and only discovers problems at the full-scene critic,
tangled with everything else. This module gives the bot an isolated checkpoint:
render the GLB alone on a neutral backdrop from several angles, so it can decide
ACCEPT / REGENERATE / FALL BACK *before* the asset pollutes the scene.

The render happens inside the ALREADY-RUNNING Blender session (out-of-band, via
:class:`~blendahbot.blender.BlenderClient`), but in a THROWAWAY scene — so the live
working scene's objects, camera, world, selection and render settings are never
touched. The approach (temporary ``bpy.data.scenes.new`` scene, import under a
``temp_override``, render the non-active scene with ``render(scene=...)``, then
remove ONLY the datablocks we created — never ``orphans_purge``, which is
destructive) was verified live on Blender 5.1.2.

    python -m blendahbot.gen3d.preview assets/barrel.glb --out runs/.../preview
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..blender import BlenderClient, BlenderUnavailable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

# (label, azimuth_deg, elevation_deg). Front catches the face, side+back_3q catch
# missing/blobby backs and asymmetry, high catches top holes / a baked-in floor.
DEFAULT_SHOTS: list[list] = [
    ["front", 0, 12],
    ["side", 90, 8],
    ["back_3q", 150, 18],
    ["high_3q", 40, 38],
]


@dataclass
class AssetStats:
    """Factual geometry/material counts pulled from the import, for the heuristic."""

    object_count: int = 0
    mesh_count: int = 0
    face_count: int = 0
    has_materials: bool = False
    has_uvs: bool = False
    texture_count: int = 0
    max_texture_px: int = 0
    longest_dim: float = 0.0
    bbox_aspect: float = 0.0


@dataclass
class PreviewResult:
    glb_path: Path
    sheet_paths: list[Path] = field(default_factory=list)
    stats: AssetStats | None = None
    ok: bool = False
    detail: str = ""


def heuristic_floor(stats: AssetStats | None, *, want_texture: bool = True) -> tuple[bool, list[str]]:
    """Free pre-critic gate: reject obvious garbage without spending a model call.

    Returns ``(passes, reasons)``. ``passes`` is False (with reasons) for an empty
    import, a many-mesh result that is almost certainly a whole baked scene, an
    extreme/degenerate bounding box, or a missing material when one was expected.
    """
    if stats is None:
        return False, ["no preview stats (the isolated render did not complete)"]
    reasons: list[str] = []
    if stats.mesh_count == 0 or stats.face_count == 0:
        reasons.append("the import produced no usable geometry")
    if stats.mesh_count >= 6:
        reasons.append(
            f"the import produced {stats.mesh_count} separate meshes — almost certainly a "
            "whole baked scene, not one prop"
        )
    if stats.bbox_aspect >= 12:
        reasons.append(
            f"extreme bounding-box aspect ({stats.bbox_aspect:.0f}:1) — likely a flat/degenerate "
            "or scene-like result, not a solid object"
        )
    if want_texture and not stats.has_materials:
        reasons.append("the mesh has no material though a textured asset was requested")
    return (not reasons), reasons


def build_preview_program(
    glb_path: str | Path,
    out_dir: str | Path,
    shots: Sequence | None = None,
    res: tuple[int, int] = (480, 480),
    engine: str = "BLENDER_EEVEE",
    target_size: float = 2.0,
) -> str:
    """Return the bpy program that renders ``glb_path`` in an isolated temp scene.

    Parameters are injected as a small literal header (no ``str.format``/f-string on
    the body) so the bpy code — full of dict/set literals — stays readable and never
    needs brace-escaping.
    """
    shots = list(shots) if shots is not None else DEFAULT_SHOTS
    glb = str(Path(glb_path).resolve()).replace("\\", "/")
    out = str(Path(out_dir).resolve()).replace("\\", "/")
    header = (
        f"_GLB = {json.dumps(glb)}\n"
        f"_OUT = {json.dumps(out)}\n"
        f"_RES = ({int(res[0])}, {int(res[1])})\n"
        f"_ENGINE = {json.dumps(engine)}\n"
        f"_TARGET = {float(target_size)!r}\n"
        f"_SHOTS = {json.dumps(shots)}\n"
    )
    return header + _PREVIEW_BODY


def render_isolated_preview(
    client: BlenderClient,
    glb_path: str | Path,
    out_dir: str | Path,
    *,
    shots: Sequence | None = None,
    res: tuple[int, int] = (480, 480),
    engine: str = "BLENDER_EEVEE",
    target_size: float = 2.0,
) -> PreviewResult:
    """Render ``glb_path`` alone from several angles, leaving the live scene untouched.

    Sends the render program through ``client.execute`` (out-of-band socket). A heavy
    glTF import can occasionally drop the add-on socket; that surfaces as a transport
    error here and is returned as ``ok=False`` rather than raising, so callers can
    retry or fall back.
    """
    glb = Path(glb_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    code = build_preview_program(glb, out, shots, res, engine, target_size)
    try:
        resp = client.execute(code)
    except BlenderUnavailable as ex:
        return PreviewResult(glb_path=glb, ok=False, detail=str(ex))

    stdout = str(resp.get("stdout") or "")
    if resp.get("status") == "ok" and "BB_PREVIEW_OK" in stdout:
        manifest = _parse_manifest(stdout)
        stats = _stats_from_manifest(manifest) if manifest else None
        sheets = [Path(p) for p in (manifest or {}).get("shots", []) if Path(p).exists()]
        if not sheets:  # manifest unreadable but render ran — fall back to the dir
            sheets = sorted(out.glob("shot_*.png"))
        return PreviewResult(glb_path=glb, sheet_paths=sheets, stats=stats, ok=True)
    reason = str(resp.get("message") or resp.get("stderr") or "preview did not complete").strip()
    return PreviewResult(glb_path=glb, ok=False, detail=reason or "preview did not complete")


# -- manifest parsing ------------------------------------------------------

def _parse_manifest(stdout: str) -> dict | None:
    for line in stdout.splitlines():
        if line.startswith("BB_PREVIEW="):
            try:
                obj = json.loads(line[len("BB_PREVIEW="):])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
    return None


def _stats_from_manifest(m: dict) -> AssetStats:
    def _i(key: str) -> int:
        try:
            return int(m.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _f(key: str) -> float:
        try:
            return float(m.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    return AssetStats(
        object_count=_i("object_count"),
        mesh_count=_i("mesh_count"),
        face_count=_i("face_count"),
        has_materials=bool(m.get("has_materials")),
        has_uvs=bool(m.get("has_uvs")),
        texture_count=_i("texture_count"),
        max_texture_px=_i("max_texture_px"),
        longest_dim=_f("longest_dim"),
        bbox_aspect=_f("bbox_aspect"),
    )


def parse_preview_stats(stdout: str) -> AssetStats | None:
    """Public: extract :class:`AssetStats` from a preview program's stdout (or None)."""
    m = _parse_manifest(stdout)
    return _stats_from_manifest(m) if m else None


# -- the bpy program (runs inside the live Blender session) ----------------
# Builds everything via the bpy.data API (NOT operators, which mutate the active
# scene/selection), renders the NON-active temp scene with render(scene=...) so the
# user's window scene never changes, and in `finally` removes ONLY the datablocks
# this program created (set-difference vs a pre-snapshot). It NEVER calls
# orphans_purge — verified live that it sweeps pre-existing user datablocks and
# silently flips the working scene's active object.
_PREVIEW_BODY = r'''
import bpy, json, math, os
from mathutils import Vector

os.makedirs(_OUT, exist_ok=True)
win = bpy.context.window
_KINDS = ("objects", "meshes", "materials", "images", "cameras", "lights",
          "collections", "worlds")


def _snap():
    return {k: set(d.name_full for d in getattr(bpy.data, k)) for k in _KINDS}


before = _snap()

preview = bpy.data.scenes.new("BB_Gen3DPreview")
pw = bpy.data.worlds.new("BB_Gen3DPreviewWorld")
pw.use_nodes = True
_bg = pw.node_tree.nodes.get("Background")
if _bg is not None:
    _bg.inputs[0].default_value = (0.18, 0.18, 0.19, 1.0)
preview.world = pw

manifest = {"shots": [], "object_count": 0, "mesh_count": 0, "face_count": 0,
            "has_materials": False, "has_uvs": False, "texture_count": 0,
            "max_texture_px": 0, "longest_dim": 0.0, "bbox_aspect": 0.0}

try:
    # Import INTO the temp scene/collection only (5.1 defaults select created objects
    # and wrap them in a new collection; the override + flag keep that out of the
    # user's working view layer).
    with bpy.context.temp_override(window=win, scene=preview,
                                   view_layer=preview.view_layers[0],
                                   collection=preview.collection):
        bpy.ops.import_scene.gltf(filepath=_GLB, import_scene_as_collection=False)

    after_import = _snap()
    new_objs = [bpy.data.objects[n] for n in (after_import["objects"] - before["objects"])]
    for o in new_objs:
        if o.name not in preview.objects:
            try:
                preview.collection.objects.link(o)
            except RuntimeError:
                pass
    meshes = [o for o in new_objs if o.type == "MESH"]
    if not meshes:
        raise RuntimeError("glTF import produced no mesh")

    bpy.context.view_layer.update()
    raw = []
    for o in meshes:
        raw += [o.matrix_world @ Vector(c) for c in o.bound_box]
    mn = Vector((min(c.x for c in raw), min(c.y for c in raw), min(c.z for c in raw)))
    mx = Vector((max(c.x for c in raw), max(c.y for c in raw), max(c.z for c in raw)))
    dims = mx - mn
    longest = max(dims.x, dims.y, dims.z) or 1.0
    shortest = max(min(dims.x, dims.y, dims.z), 1e-6)
    manifest["longest_dim"] = round(longest, 5)
    manifest["bbox_aspect"] = round(longest / shortest, 3)

    # Normalize to a known size + center on the origin so framing is consistent.
    center = (mn + mx) / 2.0
    for o in meshes:
        o.location -= center
        o.scale *= (_TARGET / longest)
    bpy.context.view_layer.update()
    cs = []
    for o in meshes:
        cs += [o.matrix_world @ Vector(c) for c in o.bound_box]
    bcen = sum(cs, Vector()) / len(cs)
    radius = max((c - bcen).length for c in cs) or 1.0

    # Geometry + material facts for the cheap heuristic gate.
    faces = 0
    has_uv = False
    has_mat = False
    images = {}
    for o in meshes:
        faces += len(o.data.polygons)
        if o.data.uv_layers:
            has_uv = True
        for slot in o.data.materials:
            if slot is None:
                continue
            has_mat = True
            if slot.use_nodes and slot.node_tree is not None:
                for node in slot.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image is not None:
                        try:
                            images[node.image.name_full] = int(max(node.image.size))
                        except (ValueError, TypeError):
                            images[node.image.name_full] = 0
    manifest["object_count"] = len(new_objs)
    manifest["mesh_count"] = len(meshes)
    manifest["face_count"] = faces
    manifest["has_materials"] = bool(has_mat)
    manifest["has_uvs"] = bool(has_uv)
    manifest["texture_count"] = len(images)
    manifest["max_texture_px"] = int(max(images.values())) if images else 0

    # Camera + lights OWNED by the temp scene (data-API, no operators).
    cam_data = bpy.data.cameras.new("BB_Gen3DPreviewCam")
    cam = bpy.data.objects.new("BB_Gen3DPreviewCam", cam_data)
    preview.collection.objects.link(cam)
    preview.camera = cam
    kd = bpy.data.lights.new("BB_Gen3DPreviewKey", "SUN")
    kd.energy = 3.5
    key = bpy.data.objects.new("BB_Gen3DPreviewKey", kd)
    preview.collection.objects.link(key)
    key.rotation_euler = (math.radians(55), math.radians(15), math.radians(40))
    fd = bpy.data.lights.new("BB_Gen3DPreviewFill", "SUN")
    fd.energy = 1.2
    fill = bpy.data.objects.new("BB_Gen3DPreviewFill", fd)
    preview.collection.objects.link(fill)
    fill.rotation_euler = (math.radians(60), math.radians(-25), math.radians(-110))

    pr = preview.render
    try:
        pr.engine = _ENGINE
    except (TypeError, ValueError):
        pr.engine = "BLENDER_EEVEE"
    try:
        preview.eevee.taa_render_samples = 16
    except Exception:
        pass
    pr.resolution_x, pr.resolution_y = _RES
    pr.resolution_percentage = 100
    pr.film_transparent = False
    pr.image_settings.file_format = "PNG"
    pr.image_settings.color_mode = "RGB"

    aspect = _RES[0] / _RES[1]
    hfov = 2 * math.atan((cam_data.sensor_width / 2) / cam_data.lens)
    vfov = 2 * math.atan(math.tan(hfov / 2) / aspect)
    dist = (radius * 1.3) / math.sin(min(hfov, vfov) / 2)
    cam_data.clip_start = max(dist * 0.01, 0.001)
    cam_data.clip_end = dist * 100.0

    for label, az, el in _SHOTS:
        a = math.radians(az)
        e = math.radians(el)
        cam.location = bcen + Vector((dist * math.cos(e) * math.sin(a),
                                      -dist * math.cos(e) * math.cos(a),
                                      dist * math.sin(e)))
        cam.rotation_euler = (bcen - cam.location).to_track_quat("-Z", "Y").to_euler()
        p = os.path.join(_OUT, "shot_%s.png" % label).replace("\\", "/")
        pr.filepath = p
        # Render the NON-active preview scene; window.scene is never swapped.
        bpy.ops.render.render(write_still=True, scene=preview.name)
        manifest["shots"].append(p)

    print("BB_PREVIEW=" + json.dumps(manifest))
    print("BB_PREVIEW_OK")
finally:
    # Targeted teardown: remove ONLY what we created (set-difference vs `before`).
    # Scene first (drops it as a user of its collection/world), then objects, then
    # data, when users hit 0. NEVER a global orphan purge (it sweeps the user's own
    # pre-existing orphans and flips their active object).
    after = _snap()
    try:
        if win is None or win.scene is not preview:
            bpy.data.scenes.remove(preview, do_unlink=True)
    except Exception as _scene_err:
        print("BB_PREVIEW_CLEANUP_WARN scene", _scene_err)
    for kind in ("objects", "collections", "meshes", "cameras", "lights",
                 "materials", "images", "worlds"):
        coll = getattr(bpy.data, kind)
        for name in (after.get(kind, set()) - before.get(kind, set())):
            d = coll.get(name)
            if d is None:
                continue
            try:
                if kind == "objects" or d.users == 0:
                    coll.remove(d)
            except (ReferenceError, RuntimeError):
                pass
'''


# -- standalone CLI: preview an arbitrary existing GLB ---------------------

_SHOT_CATALOG = {
    "front": ["front", 0, 12],
    "side": ["side", 90, 8],
    "back": ["back", 180, 12],
    "back_3q": ["back_3q", 150, 18],
    "hero_3q": ["hero_3q", 35, 18],
    "high_3q": ["high_3q", 40, 38],
    "top": ["top", 0, 85],
}


def _parse_shot_arg(arg: str | None) -> list[list] | None:
    if not arg:
        return None
    shots: list[list] = []
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            label, az, el = tok.split(":")
            shots.append([label, float(az), float(el)])
        elif tok in _SHOT_CATALOG:
            shots.append(list(_SHOT_CATALOG[tok]))
    return shots or None


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="blendahbot.gen3d.preview",
        description="Render a multi-angle isolated preview of a GLB in the live Blender session.",
    )
    p.add_argument("glb", help="Path to the .glb to preview.")
    p.add_argument("--out", default="preview", help="Directory for the contact-sheet PNGs.")
    p.add_argument("--shots", default=None,
                   help="Comma list of catalog labels or label:az:el (default: front,side,back_3q,high_3q).")
    p.add_argument("--res", type=int, default=480, help="Square render resolution (default 480).")
    p.add_argument("--engine", default="BLENDER_EEVEE", help="Render engine (default fast EEVEE).")
    p.add_argument("--host", default=os.environ.get("BLENDER_MCP_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("BLENDER_MCP_PORT", "9876")))
    p.add_argument("--timeout", type=float, default=300.0)
    args = p.parse_args(argv)

    client = BlenderClient(args.host, args.port, timeout=args.timeout)
    result = render_isolated_preview(
        client, args.glb, args.out,
        shots=_parse_shot_arg(args.shots), res=(args.res, args.res), engine=args.engine,
    )
    if not result.ok:
        print(f"[preview] failed: {result.detail}", file=sys.stderr)
        return 1
    for sp in result.sheet_paths:
        print(sp)  # the agent reads these from stdout
    if result.stats is not None:
        s = result.stats
        print(
            f"[preview] {s.mesh_count} mesh(es), {s.face_count} faces, textured={s.has_materials}, "
            f"uvs={s.has_uvs}, aspect={s.bbox_aspect}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

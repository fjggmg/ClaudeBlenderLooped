"""Render a generated GLB *in isolation* so the bot can vet it before placing it.

A freshly generated mesh (especially text->3D) is unpredictable: it may be the
wrong object, a blobby/holed reconstruction, a whole *scene* baked into one
"object", garbled-texture, or wrongly proportioned. Today the builder imports it
into the live scene blind and only discovers problems at the full-scene critic,
tangled with everything else. This module gives the bot an isolated checkpoint:
render the GLB alone on a neutral backdrop from several angles, so it can decide
ACCEPT / REGENERATE / FALL BACK *before* the asset pollutes the scene.

The render runs in a SEPARATE, throwaway ``blender --background`` process — NOT the
live session. That is the safe choice: importing a GLB through the live blender-mcp
add-on can crash it (heavy importer operators drop the socket), whereas an ephemeral
headless process is fully isolated by construction — it cannot touch the live scene,
needs no add-on, and disappears (with everything it created) when it exits. It only
needs ``blender.exe`` on disk and the GLB file; the live Blender doesn't even have to
be running.

    python -m blendahbot.gen3d.preview assets/barrel.glb --out runs/.../preview
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..discovery import DiscoveryError, find_blender_executable

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
    engine: str = ""


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


def render_isolated_preview(
    glb_path: str | Path,
    out_dir: str | Path,
    *,
    blender: str | None = None,
    shots: Sequence | None = None,
    res: int = 480,
    engine: str = "BLENDER_EEVEE",
    target_size: float = 2.0,
    timeout: float = 240.0,
) -> PreviewResult:
    """Render ``glb_path`` alone from several angles in a throwaway headless Blender.

    Never touches the live session — it spawns its own ``blender --background`` process,
    so it cannot disturb (or be disturbed by) the build. Returns ``ok=False`` with a
    reason if Blender can't be found/launched or the render fails, rather than raising.
    """
    glb = Path(glb_path)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    shots = list(shots) if shots is not None else DEFAULT_SHOTS
    try:
        exe = find_blender_executable(blender)
    except DiscoveryError as ex:
        return PreviewResult(glb_path=glb, ok=False, detail=str(ex))

    script_path = _write_script()
    try:
        cmd = _preview_command(exe, script_path, glb, out, res, engine, target_size, shots)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return PreviewResult(glb_path=glb, ok=False,
                                 detail=f"preview render timed out after {timeout:.0f}s")
        except OSError as ex:
            return PreviewResult(glb_path=glb, ok=False, detail=f"could not launch Blender: {ex}")
    finally:
        try:
            os.unlink(script_path)
        except OSError:
            pass

    stdout = proc.stdout or ""
    if "BB_PREVIEW_OK" in stdout:
        manifest = _parse_manifest(stdout)
        stats = _stats_from_manifest(manifest) if manifest else None
        sheets = [Path(p) for p in (manifest or {}).get("shots", []) if Path(p).exists()]
        if not sheets:  # manifest unreadable but render ran — fall back to the dir
            sheets = sorted(out.glob("shot_*.png"))
        engine_used = str((manifest or {}).get("engine") or engine)
        return PreviewResult(glb_path=glb, sheet_paths=sheets, stats=stats, ok=True, engine=engine_used)
    return PreviewResult(glb_path=glb, ok=False,
                         detail=_failure_detail(stdout, proc.stderr or "", proc.returncode))


def _preview_command(exe, script, glb, out, res, engine, target_size, shots) -> list[str]:
    """The ``blender --background`` argv. ``--factory-startup`` skips user add-ons/prefs
    (faster, and the live session's add-on never loads in this process)."""
    return [
        str(exe), "--background", "--factory-startup", "--python", str(script), "--",
        str(Path(glb).resolve()), str(Path(out).resolve()), str(int(res)), str(engine),
        repr(float(target_size)), json.dumps(list(shots)),
    ]


def _write_script() -> str:
    f = tempfile.NamedTemporaryFile("w", suffix="_bbpreview.py", delete=False, encoding="utf-8")
    try:
        f.write(_PREVIEW_SCRIPT)
    finally:
        f.close()
    return f.name


def _failure_detail(stdout: str, stderr: str, returncode: int) -> str:
    for line in stdout.splitlines():
        if line.startswith("BB_PREVIEW_ERR"):
            return line[len("BB_PREVIEW_ERR"):].strip() or "preview render failed"
    err_lines = [ln for ln in stderr.splitlines() if ln.strip()]
    if err_lines:
        return err_lines[-1][:300]
    return f"preview render failed (Blender exited {returncode})"


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


# -- the headless bpy program (runs in its own throwaway Blender process) ---
# Self-contained: bpy + stdlib only (it executes inside Blender's Python, which does
# not have the blendahbot package on its path). Reads args after "--", clears the
# startup scene, imports the GLB, normalizes/centers it, renders N angles to PNGs, and
# prints a JSON manifest. No live session is involved, so no temp-scene/teardown dance
# is needed — the process is ephemeral and takes everything with it on exit. EEVEE is
# fast and works headless on 5.1; if a shot fails it falls back to Cycles once.
_PREVIEW_SCRIPT = r'''
import bpy, sys, json, math, os
from mathutils import Vector

_a = sys.argv[sys.argv.index("--") + 1:]
GLB, OUT = _a[0], _a[1]
RES = int(_a[2]) if len(_a) > 2 else 480
ENGINE = _a[3] if len(_a) > 3 else "BLENDER_EEVEE"
TARGET = float(_a[4]) if len(_a) > 4 else 2.0
SHOTS = json.loads(_a[5]) if len(_a) > 5 else [["front", 0, 12], ["side", 90, 8],
                                               ["back_3q", 150, 18], ["high_3q", 40, 38]]
os.makedirs(OUT, exist_ok=True)

try:
    bpy.ops.preferences.addon_enable(module="io_scene_gltf2")
except Exception:
    pass  # glTF importer is enabled by default; this is just belt-and-braces

# Fresh process: clear the startup scene so ONLY the asset is in frame.
for _o in list(bpy.data.objects):
    bpy.data.objects.remove(_o, do_unlink=True)

before = set(bpy.data.objects)
try:
    bpy.ops.import_scene.gltf(filepath=GLB)
except Exception as ex:
    print("BB_PREVIEW_ERR glTF import failed:", ex)
    sys.exit(2)
new = [o for o in bpy.data.objects if o not in before]
meshes = [o for o in new if o.type == "MESH"]
if not meshes:
    print("BB_PREVIEW_ERR glTF import produced no mesh")
    sys.exit(2)

bpy.context.view_layer.update()
raw = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
mn = Vector((min(c.x for c in raw), min(c.y for c in raw), min(c.z for c in raw)))
mx = Vector((max(c.x for c in raw), max(c.y for c in raw), max(c.z for c in raw)))
dims = mx - mn
longest = max(dims.x, dims.y, dims.z) or 1.0
shortest = max(min(dims.x, dims.y, dims.z), 1e-6)

center = (mn + mx) / 2.0
for o in meshes:
    o.location -= center
    o.scale *= (TARGET / longest)
bpy.context.view_layer.update()
cs = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
bcen = sum(cs, Vector()) / len(cs)
radius = max((c - bcen).length for c in cs) or 1.0

faces = 0
has_uv = has_mat = False
images = {}
for o in meshes:
    faces += len(o.data.polygons)
    if o.data.uv_layers:
        has_uv = True
    for slot in o.data.materials:
        if slot is None:
            continue
        has_mat = True
        if slot.use_nodes and slot.node_tree:
            for node in slot.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    try:
                        images[node.image.name_full] = int(max(node.image.size))
                    except Exception:
                        images[node.image.name_full] = 0

scene = bpy.context.scene
world = bpy.data.worlds.new("BB_PreviewWorld")
world.use_nodes = True
_bg = world.node_tree.nodes.get("Background")
if _bg is not None:
    _bg.inputs[0].default_value = (0.18, 0.18, 0.19, 1.0)
scene.world = world

cam_data = bpy.data.cameras.new("BB_PreviewCam")
cam = bpy.data.objects.new("BB_PreviewCam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam
kd = bpy.data.lights.new("BB_PreviewKey", "SUN"); kd.energy = 3.5
key = bpy.data.objects.new("BB_PreviewKey", kd); scene.collection.objects.link(key)
key.rotation_euler = (math.radians(55), math.radians(15), math.radians(40))
fd = bpy.data.lights.new("BB_PreviewFill", "SUN"); fd.energy = 1.2
fill = bpy.data.objects.new("BB_PreviewFill", fd); scene.collection.objects.link(fill)
fill.rotation_euler = (math.radians(60), math.radians(-25), math.radians(-110))

r = scene.render
r.resolution_x = r.resolution_y = RES
r.image_settings.file_format = "PNG"
r.image_settings.color_mode = "RGB"
try:
    r.engine = ENGINE
except (TypeError, ValueError):
    r.engine = "BLENDER_EEVEE"

hfov = 2 * math.atan((cam_data.sensor_width / 2) / cam_data.lens)
vfov = 2 * math.atan(math.tan(hfov / 2) / 1.0)
dist = (radius * 1.3) / math.sin(min(hfov, vfov) / 2)
cam_data.clip_start = max(dist * 0.01, 0.001)
cam_data.clip_end = dist * 100.0

written = []
used_engine = r.engine
for i, (label, az, el) in enumerate(SHOTS):
    aa, ee = math.radians(az), math.radians(el)
    cam.location = bcen + Vector((dist * math.cos(ee) * math.sin(aa),
                                  -dist * math.cos(ee) * math.cos(aa),
                                  dist * math.sin(ee)))
    cam.rotation_euler = (bcen - cam.location).to_track_quat("-Z", "Y").to_euler()
    p = os.path.join(OUT, "shot_%s.png" % label).replace("\\", "/")
    r.filepath = p
    try:
        bpy.ops.render.render(write_still=True)
    except Exception as ex:
        if i == 0 and r.engine != "CYCLES":
            print("BB_PREVIEW_NOTE EEVEE render failed, falling back to Cycles:", ex)
            r.engine = "CYCLES"; used_engine = "CYCLES"
            try:
                scene.cycles.samples = 16
            except Exception:
                pass
            bpy.ops.render.render(write_still=True)
        else:
            raise
    written.append(p)

manifest = {
    "shots": written, "object_count": len(new), "mesh_count": len(meshes),
    "face_count": faces, "has_materials": has_mat, "has_uvs": has_uv,
    "texture_count": len(images), "max_texture_px": int(max(images.values())) if images else 0,
    "longest_dim": round(longest, 5), "bbox_aspect": round(longest / shortest, 3),
    "engine": used_engine,
}
print("BB_PREVIEW=" + json.dumps(manifest))
print("BB_PREVIEW_OK")
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
        description="Render a multi-angle isolated preview of a GLB in a throwaway headless Blender.",
    )
    p.add_argument("glb", help="Path to the .glb to preview.")
    p.add_argument("--out", default="preview", help="Directory for the contact-sheet PNGs.")
    p.add_argument("--shots", default=None,
                   help="Comma list of catalog labels or label:az:el (default: front,side,back_3q,high_3q).")
    p.add_argument("--res", type=int, default=480, help="Square render resolution (default 480).")
    p.add_argument("--engine", default="BLENDER_EEVEE", help="Render engine (default fast EEVEE).")
    p.add_argument("--blender", default=None, help="Path to blender.exe (default: auto-detect).")
    p.add_argument("--timeout", type=float, default=240.0)
    args = p.parse_args(argv)

    result = render_isolated_preview(
        args.glb, args.out, blender=args.blender, shots=_parse_shot_arg(args.shots),
        res=args.res, engine=args.engine, timeout=args.timeout,
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
            f"uvs={s.has_uvs}, aspect={s.bbox_aspect}, engine={result.engine}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

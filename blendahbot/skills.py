"""A growable library of Blender modelling recipes the builder consults and extends.

Seeds a set of verified hard-surface / PBR / lighting / camera recipes into
``~/.blendahbot/skills/`` on first run. The builder reads ``INDEX.md`` and the
matching recipes before modelling (so it starts from a known-good technique
instead of from scratch), and is told to save new recipes it discovers — so the
bot accumulates modelling know-how over time.
"""

from __future__ import annotations

import os
from pathlib import Path

# Verified Blender 5.1.2 helper functions, embedded into the recipes that use them.
_FN_HDRI = '''```python
import bpy, os

def setup_hdri_world(hdri_path, strength=1.0, rotation_z=0.0, world_name="HDRI_World"):
    if not os.path.isfile(hdri_path):
        raise FileNotFoundError(hdri_path)
    world = bpy.data.worlds.get(world_name) or bpy.data.worlds.new(world_name)
    world.use_nodes = True
    nt = world.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputWorld"); out.location = (600, 0)
    bg = nt.nodes.new("ShaderNodeBackground"); bg.location = (400, 0)
    env = nt.nodes.new("ShaderNodeTexEnvironment"); env.location = (150, 0)
    mapping = nt.nodes.new("ShaderNodeMapping"); mapping.location = (-100, 0)
    texcoord = nt.nodes.new("ShaderNodeTexCoord"); texcoord.location = (-300, 0)
    img = bpy.data.images.load(hdri_path, check_existing=True)
    try: img.colorspace_settings.name = "Linear Rec.709"
    except Exception: img.colorspace_settings.name = "Non-Color"
    env.image = img
    mapping.inputs["Rotation"].default_value[2] = rotation_z
    L = nt.links
    L.new(texcoord.outputs["Generated"], mapping.inputs["Vector"])
    L.new(mapping.outputs["Vector"], env.inputs["Vector"])
    L.new(env.outputs["Color"], bg.inputs["Color"])
    bg.inputs["Strength"].default_value = strength
    L.new(bg.outputs["Background"], out.inputs["Surface"])
    bpy.context.scene.world = world
    return world
```'''

_FN_PBR = '''```python
import bpy

def build_pbr_material(obj, diff_path=None, nor_gl_path=None, rough_path=None,
                       metal_path=None, ao_path=None, scale=1.0, normal_strength=1.0,
                       use_uv=True, mat_name="PBR_Mat"):
    # Colorspace: Diffuse/AO = sRGB; Normal/Rough/Metal = Non-Color. Use nor_gl (OpenGL).
    mat = bpy.data.materials.new(mat_name); mat.use_nodes = True
    nt = mat.node_tree; nt.nodes.clear(); L = nt.links
    out = nt.nodes.new("ShaderNodeOutputMaterial"); out.location = (1000, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled"); bsdf.location = (700, 0)
    L.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    tc = nt.nodes.new("ShaderNodeTexCoord"); tc.location = (-900, 0)
    mp = nt.nodes.new("ShaderNodeMapping"); mp.location = (-700, 0)
    mp.inputs["Scale"].default_value = (scale, scale, scale)
    L.new(tc.outputs["UV"] if use_uv else tc.outputs["Generated"], mp.inputs["Vector"])
    def img(path, non_color, y):
        n = nt.nodes.new("ShaderNodeTexImage"); n.location = (-400, y)
        n.image = bpy.data.images.load(path, check_existing=True)
        n.image.colorspace_settings.name = "Non-Color" if non_color else "sRGB"
        L.new(mp.outputs["Vector"], n.inputs["Vector"]); return n
    if diff_path:
        base = img(diff_path, False, 400).outputs["Color"]
        if ao_path:
            mix = nt.nodes.new("ShaderNodeMixRGB"); mix.blend_type = "MULTIPLY"
            mix.inputs["Fac"].default_value = 1.0
            L.new(base, mix.inputs["Color1"])
            L.new(img(ao_path, True, 600).outputs["Color"], mix.inputs["Color2"])
            base = mix.outputs["Color"]
        L.new(base, bsdf.inputs["Base Color"])
    if rough_path: L.new(img(rough_path, True, 150).outputs["Color"], bsdf.inputs["Roughness"])
    if metal_path: L.new(img(metal_path, True, -100).outputs["Color"], bsdf.inputs["Metallic"])
    if nor_gl_path:
        nmap = nt.nodes.new("ShaderNodeNormalMap"); nmap.location = (200, -350)
        nmap.inputs["Strength"].default_value = normal_strength
        L.new(img(nor_gl_path, True, -350).outputs["Color"], nmap.inputs["Color"])
        L.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
    if obj.data.materials: obj.data.materials[0] = mat
    else: obj.data.materials.append(mat)
    return mat
```'''

_FN_CAMERA = '''```python
import bpy, math
from mathutils import Vector

def setup_hero_camera(target, name="HeroCam", azimuth_deg=35.0, elevation_deg=18.0,
                      lens_mm=50.0, margin=1.3, samples=256, resolution=(1920, 1080)):
    # Distance is derived from the camera FOV so the bounding sphere ALWAYS fits
    # (with margin) — fixes "the camera never gets everything".
    scene = bpy.context.scene
    scene.render.resolution_x, scene.render.resolution_y = resolution
    deps = bpy.context.evaluated_depsgraph_get(); target.update_tag(); deps.update()
    ev = target.evaluated_get(deps); mw = ev.matrix_world
    corners = [mw @ Vector(c) for c in ev.bound_box]
    center = sum(corners, Vector((0, 0, 0))) / 8.0
    radius = max((c - center).length for c in corners) or 1.0
    cam_data = bpy.data.cameras.new(name); cam_data.lens = lens_mm
    aspect = resolution[0] / resolution[1]
    hfov = 2 * math.atan((cam_data.sensor_width / 2) / lens_mm)
    vfov = 2 * math.atan(math.tan(hfov / 2) / aspect)
    dist = (radius * margin) / math.sin(min(hfov, vfov) / 2)   # tighter axis dictates fit
    cam_data.clip_start = max(dist * 0.01, 0.001); cam_data.clip_end = dist * 100.0
    cam = bpy.data.objects.new(name, cam_data); scene.collection.objects.link(cam)
    az = math.radians(azimuth_deg); el = math.radians(elevation_deg)
    cam.location = center + Vector((dist*math.cos(el)*math.sin(az),
                                    -dist*math.cos(el)*math.cos(az), dist*math.sin(el)))
    cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    scene.render.engine = "CYCLES"
    scene.render.resolution_x, scene.render.resolution_y = resolution
    scene.render.film_transparent = False
    cyc = scene.cycles; cyc.samples = samples; cyc.use_adaptive_sampling = True
    cyc.use_denoising = True
    try: cyc.denoiser = "OPTIX"
    except (TypeError, ValueError): cyc.denoiser = "OPENIMAGEDENOISE"
    try: cyc.device = "GPU"
    except (TypeError, ValueError): cyc.device = "CPU"
    return cam
```'''

# (name, when_to_use, confidence, body) for each seed recipe.
_SEED: list[tuple[str, str, str, str]] = [
    ("hard-surface-bevel-wn",
     "Every hard-surface object as the finishing pass — THE fix for the smooth-blob look.",
     "high",
     """# Hard-Surface Bevel + Weighted Normal (the anti-blob stack)

## When to use
Every hard-surface object after big shapes/panels/greebles are done. Replaces the
Subdivision-Surface habit that melts edges into blobs.

## Steps
1. Finish all large shape edits and panel cuts FIRST.
2. Modifiers top->bottom: Bevel, [Solidify only for plane parts], Weighted Normal LAST.
3. shade_smooth + shade_auto_smooth OPERATOR (not the removed mesh.use_auto_smooth).

## bpy snippet
```python
import bpy, math
def finish_hard_surface(obj, G, plane_based=False, segments=2):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True); bpy.context.view_layer.objects.active = obj
    bev = obj.modifiers.new('Bevel', 'BEVEL')
    bev.offset_type = 'WIDTH'      # 5.1: offset_type, NOT width_type
    bev.width = 0.2 * G            # small! big bevels = blobby
    bev.segments = segments
    bev.limit_method = 'ANGLE'; bev.angle_limit = math.radians(30)
    bev.harden_normals = True
    if plane_based:                # wings/fins/plating only
        sol = obj.modifiers.new('Solidify', 'SOLIDIFY'); sol.thickness = 1.5 * G; sol.offset = -1
    wn = obj.modifiers.new('WeightedNormal', 'WEIGHTED_NORMAL')   # MUST be last
    wn.keep_sharp = True; wn.mode = 'FACE_AREA'; wn.weight = 50
    bpy.ops.object.shade_smooth()
    bpy.ops.object.shade_auto_smooth(angle=math.radians(30))
    return obj
```

## Gotchas
- Bevel property is `offset_type` (NOT `width_type`) in 5.1.
- `mesh.use_auto_smooth` was removed in 4.1+; use the `shade_auto_smooth` operator.
- Weighted Normal MUST be last. harden_normals + Weighted Normal make small bevels read crisp.

## Validated result
Property names + operators verified on live Blender 5.1.2. PASS."""),

    ("pbr-material-from-polyhaven",
     "Any object needing a realistic surface — prefer over hand-rolled procedural shaders.",
     "high",
     f"""# PBR Material from PolyHaven

## When to use
Whenever a surface should look real. The fix for 'texture work is so bad'.

## Steps
1. Download maps: `python -m blendahbot.assets texture "metal plate" --out assets`
   (returns {{Diffuse,nor_gl,Rough,Metal,AO,Displacement}} paths that exist — not all have Metal).
2. UV unwrap first: edit mode, select all, `bpy.ops.uv.smart_project(angle_limit=1.15, island_margin=0.02)`.
3. Call build_pbr_material(obj, diff_path=..., nor_gl_path=..., rough_path=..., metal_path=...).
4. Assign DIFFERENT metals per section (hull/nacelle/trench) via separate slots.

## bpy snippet
{_FN_PBR}

## Gotchas
- Colorspace: Diffuse/AO = sRGB; Normal/Rough/Metal/Disp = Non-Color.
- Use nor_gl (OpenGL), NOT nor_dx.
- Mesh needs a UV map (smart_project) or pass use_uv=False for Generated projection.

## Validated result
Node IDs/sockets/colorspaces assign on Blender 5.1.2. PASS."""),

    ("hdri-world-lighting",
     "Every scene before render — metals/glass look dead-gray with nothing to reflect.",
     "high",
     f"""# HDRI World Lighting

## When to use
Every render. A black/flat-gray world is the strongest 'this is CG' tell after blobby geometry.

## Steps
1. `python -m blendahbot.assets hdri "studio" --out assets` (studio_small_09 = clean product-shot reflections).
2. Call setup_hdri_world(hdri_path, strength=1.0).
3. Add a key area light ~45deg + a RIM/back light grazing the surface to catch bevel highlights.

## bpy snippet
{_FN_HDRI}

## Gotchas
- HDRI image colorspace: 'Linear Rec.709' (fallback 'Non-Color'). Never sRGB.
- Set bpy.context.scene.world = world or it won't take effect.

## Validated result
World node IDs/sockets assign on Blender 5.1.2; studio_small_09 URL verified live. PASS."""),

    ("hero-camera-setup",
     "Final framing of any single hero object — replaces the flat default camera.",
     "high",
     f"""# Hero Camera — choose a GOOD angle, don't guess one

## When to use
Final framing of a single hero subject. `setup_hero_camera` guarantees the whole object FITS
(FOV-based), but a fitted-but-awkward angle still looks bad. You must CHOOSE the angle, not guess.

## How to get a good shot
1. DON'T commit to one angle. Render several candidate angles small/fast with
   `render_angle_candidates(target, out_dir)`, Read every `angle_*.png`, and pick the one that
   reads best — then re-render THAT angle at full quality for the final.
2. Composition: a 3/4 view (shows two sides + depth), never dead-on/axis-aligned. Elevation by
   subject — LOW (~10-15deg) for big/heroic subjects (ships, buildings, mechs) so they feel
   imposing; ~20-30deg for tabletop props; eye-level for characters. Lens 50-85mm.
3. Rule of thirds: nudge the subject slightly off-centre (small camera shift) rather than dead-centre.
4. Make sure the DEFINING features face the camera (a ship's profile + nacelles; a face's front).

## bpy snippet
{_FN_CAMERA}

```python
import bpy, os
def render_angle_candidates(target, out_dir, lens_mm=50.0, samples=48):
    # Render the subject from several angles so you can LOOK and pick the best-composed one.
    cands = [("front34", 35, 14), ("side", 90, 12), ("rear34", 145, 18), ("high34", 45, 40)]
    paths = []
    for label, az, el in cands:
        setup_hero_camera(target, name="Cam_" + label, azimuth_deg=az, elevation_deg=el,
                          lens_mm=lens_mm, samples=samples)
        p = os.path.join(out_dir, "angle_%s.png" % label)
        bpy.context.scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
    return paths
# Then: Read each angle_*.png, pick the best, call setup_hero_camera(target, azimuth_deg=...,
# elevation_deg=...) for that winner and render the final at full samples.
```

## Gotchas
- A fitted but badly-ANGLED shot still looks bad — always compare candidates and choose.
- 5.1 denoiser: 'OPTIX' / 'OPENIMAGEDENOISE' (NOT 'OPENIMAGEDENOISE_GPU').
- Update the depsgraph before reading bound_box.

## Validated result
Camera math + Cycles settings assign on Blender 5.1.2. PASS."""),

    ("panel-lines-bmesh",
     "Breaking up any large flat/round panel so it doesn't read as a featureless primitive.",
     "medium",
     """# Panel Lines (with real depth)

## When to use
Whenever a face is bigger than a palm and flat/round. A flat line with no depth disappears under lighting.

## Steps (inset, robust)
1. Edit mode, select the face region(s).
2. `bpy.ops.mesh.inset_faces(thickness=0.3*G, depth=-0.05*G)` — NEGATIVE depth recesses the panel so it self-shadows.
3. Repeat per region with VARIED inset sizes (uneven paneling reads as designed).
4. The later global Bevel pass catches the new panel edges -> crisp shadow line.

## bpy snippet
```python
import bpy
def recessed_panel(obj, G, thickness_mult=0.3, depth_mult=-0.05):
    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True); bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.inset_faces(thickness=thickness_mult*G, depth=depth_mult*G)
    bpy.ops.object.mode_set(mode='OBJECT')
```

## Gotchas
- Panel lines MUST have depth/shadow. Zero-depth lines are invisible at render.
- Do panels BEFORE the bevel pass. Vary spacing/size; regular grids look like a texture.

## Validated result
Standard 5.1 operator args. Pending in-scene render validation -> confidence medium."""),

    ("grounding-and-assembly",
     "EVERY multi-part build. Stops the #1 failure: primitives floating and not connected.",
     "high",
     """# Grounding + Assembly (stop floating, disconnected primitives)

## When to use
EVERY build with more than one part. A pile of primitives floating in the air and not
touching is the single most common failure. Objects must rest on the ground and parts
must physically connect.

## Steps
1. Build/position each part roughly.
2. `drop_to_floor(obj)` for everything that rests on the ground (walls, trunks, rocks, body).
3. `place_on_top(part, base, overlap)` to stack connected parts (roof ON walls, canopy ON
   trunk, cap ON chimney) — use a small overlap so they meet with no gap.
4. Make pieces that form ONE object actually one: `join_objects([...])` or a Boolean union.
   A chimney should PENETRATE the roof; a handle should meet the body.
5. Render a SIDE / orthographic view and confirm nothing floats and every part touches its
   neighbour and the ground. Fix gaps before finishing.

## bpy snippet
```python
import bpy
from mathutils import Vector

def _bbox_world(obj):
    return [obj.matrix_world @ Vector(c) for c in obj.bound_box]

def drop_to_floor(obj, floor_z=0.0):
    bpy.context.view_layer.update()
    obj.location.z += floor_z - min(v.z for v in _bbox_world(obj))

def place_on_top(obj, base, overlap=0.0):
    bpy.context.view_layer.update()
    base_top = max(v.z for v in _bbox_world(base))
    obj_bottom = min(v.z for v in _bbox_world(obj))
    obj.location.z += (base_top - obj_bottom) - overlap

def join_objects(objs, name=None):
    bpy.ops.object.select_all(action='DESELECT')
    for o in objs: o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    if name: objs[0].name = name
    return objs[0]
```

## Gotchas
- Call `bpy.context.view_layer.update()` after MOVING objects, before reading `matrix_world`.
- OVERLAP connected parts slightly — never leave an air gap; a visible seam reads as "broken".
- `join()` merges into the ACTIVE object; set the active object explicitly.
- A floating object is an automatic fail. Drop EVERYTHING to where it should sit.

## Validated result
bbox grounding math is standard; join is `bpy.ops.object.join`. confidence high."""),

    ("varied-instances",
     "Placing MANY of something that should differ (trees, rocks, crowd) — stops identical-clone copies.",
     "high",
     """# Varied Instances (no two trees the same)

## When to use
Whenever you place many of something that should differ — trees, rocks, bushes, crowds,
debris, buildings. Identical copies read as fake and lazy.

## Approaches (best first)
1. PROCEDURAL with a different SEED per instance — best variation. Trees: enable the bundled
   Sapling add-on and vary the seed.
   ```python
   import bpy
   bpy.ops.preferences.addon_enable(module="add_curve_sapling_3")
   for i, loc in enumerate(positions):
       bpy.ops.curve.tree_add(do_update=True, seed=i, bevel=True, prune=False, leaves=150)
       t = bpy.context.active_object; t.location = loc      # different seed -> different tree
   ```
2. GEOMETRY NODES scatter (Distribute Points on Faces -> Instance on Points, with randomized
   Rotation + Scale) for fields of grass / rocks / trees with built-in variation.
3. RANDOMIZED COPIES of a base mesh — quick fallback:
   ```python
   import bpy, random, math
   def varied_copy(src, location, scale_jitter=0.3, lean=0.15, seed=0):
       random.seed(seed)
       o = src.copy(); o.data = src.data.copy()     # UNIQUE mesh so it can differ
       bpy.context.collection.objects.link(o); o.location = location
       s = 1.0 + random.uniform(-scale_jitter, scale_jitter)
       o.scale = (s*random.uniform(0.85,1.15), s*random.uniform(0.85,1.15), s*random.uniform(0.9,1.2))
       o.rotation_euler.z = random.uniform(0, 2*math.pi)
       o.rotation_euler.x += random.uniform(-lean, lean)
       o.rotation_euler.y += random.uniform(-lean, lean)
       return o
   ```
   Then tweak a few verts / swap proportions on some so they are not just scaled clones.

## Gotchas
- `obj.data = src.data.copy()` is ESSENTIAL — without it copies share one mesh and edits hit all.
- Randomize rotation AND proportions, not just uniform scale (uniform scale still looks cloned).
- Drop each instance to the floor after placing (see grounding-and-assembly).

## Validated result
copy()/data.copy() + Sapling `tree_add(seed=)` are standard bpy. confidence medium-high."""),

    ("greeble-scatter",
     "Adding asymmetric silhouette-breaking mechanical detail (vents, intakes, struts, antennae).",
     "medium",
     """# Greeble Scatter + Silhouette Breakers

## When to use
After big shapes, panels, and the mirror is APPLIED. Detail that sticks OUT separates 'machine' from 'primitive'.

## Steps
1. APPLY the Mirror modifier first, then add ASYMMETRIC detail.
2. Build ~8-15 detail meshes: chamfered boxes, small cylinders, pipe runs, vents, antenna stubs, dishes.
3. Repeat ribs/vents along trenches with the ARRAY modifier.
4. Add silhouette breakers: intakes (inset+extrude inward), engine bells, nacelle struts, off-center booms.
5. Hierarchy: a FEW big + several medium + MANY tiny. Uniform-size greebles read as noise.

## bpy snippet
```python
import bpy
def array_along(obj, count, dx=0.0, dy=0.0, dz=0.0):
    m = obj.modifiers.new('Array', 'ARRAY')
    m.count = count; m.use_relative_offset = False
    m.use_constant_offset = True; m.constant_offset_displace = (dx, dy, dz)
    return m
```

## Gotchas
- Apply the Mirror BEFORE greebling or asymmetric detail gets mirrored too.
- A few BIG details anchor the eye; many equal tiny ones look like noise.

## Validated result
Array props are standard 5.1. Pending in-scene render validation -> confidence medium."""),
]


def skills_dir() -> Path:
    base = os.environ.get("BLENDAHBOT_HOME")
    root = Path(base) if base else (Path.home() / ".blendahbot")
    return root / "skills"


def index_path() -> Path:
    return skills_dir() / "INDEX.md"


def ensure_seed_skills() -> Path:
    """(Re)write the canonical seed recipes and rebuild INDEX.md.

    Seed files are overwritten so shipped fixes propagate; agent-added recipes
    (any other ``*.md``) are preserved and listed in the index.
    """
    d = skills_dir()
    d.mkdir(parents=True, exist_ok=True)
    seed_names = {name for name, *_ in _SEED}
    for name, _when, conf, body in _SEED:
        front = f"---\nid: {name}\nconfidence: {conf}\n---\n\n"
        (d / f"{name}.md").write_text(front + body + "\n", encoding="utf-8")

    rows = ["# blendahbot skills index", "",
            "Modelling recipes the builder consults before building and extends after.",
            "Read the full recipe file for any row whose 'when to use' matches the task.",
            "", "| skill | confidence | when to use |", "|---|---|---|"]
    for name, when, conf, _body in _SEED:
        rows.append(f"| [{name}]({name}.md) | {conf} | {when} |")
    for extra in sorted(d.glob("*.md")):
        if extra.stem not in seed_names and extra.name != "INDEX.md":
            rows.append(f"| [{extra.stem}]({extra.name}) | added | agent-added recipe |")
    index_path().write_text("\n".join(rows) + "\n", encoding="utf-8")
    return d

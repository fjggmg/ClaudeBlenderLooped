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
                      lens_mm=50.0, margin=1.3, samples=256, resolution=(1920, 1080),
                      fstop=None):
    # Distance is derived from the camera FOV so the bounding sphere ALWAYS fits
    # (with margin) — fixes "the camera never gets everything".
    # fstop (optional): enable depth-of-field focused on the subject centre for
    # bokeh/subject isolation (low f = shallow). None = everything sharp.
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
    if fstop:                                   # depth-of-field: focus the subject centre
        cam_data.dof.use_dof = True
        cam_data.dof.focus_distance = dist
        cam_data.dof.aperture_fstop = float(fstop)
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

    ("camera-framing-library",
     "Choosing the camera shot for ANY subject — a catalog of named, exact framings to draw from.",
     "high",
     """# Camera Framing Library — pick a real shot, don't guess

## When to use
Framing ANY render — a single hero prop, a vehicle, a character, a building, or a whole
environment. This is a permanent catalog of named, proven camera setups with EXACT
angle/lens/framing/composition, so you PICK a shot instead of guessing numbers. Pairs with
hero-camera-setup (the FOV-fit math) and the loop's render-candidates-and-choose.
The catalog is 120+ named shots in 9 families distilled from real media conventions — film
shot-grammar, portrait & fine-art photography, product/packshot, architecture & interior,
automotive press shots, game key-art / cinematics, and wildlife/macro — plus an optional
depth-of-field (`fstop`) for subject isolation. Browse by the Shot families guide or jump
straight to the subject table.

## How to use it
1. Find candidates two ways: the SUBJECT TABLE (fast, per-subject defaults) or the SHOT
   FAMILIES guide (when you want a specific look — heroic, intimate, editorial, technical).
   Pick 3-5, render them small with `render_contact_sheet(target, out_dir, shots=[...])`,
   Read every `shot_*.png`, and choose the best-composed one.
2. Re-render the winner at full samples/resolution with `place_shot(target, "<winner>")`.
3. NEVER ship the default viewport camera or a dead-on axis-aligned shot.
4. Unsure how a named shot reads? PULL REAL EXAMPLE MEDIA first and study the composition —
   any source helps: `python -m blendahbot.refs "low angle hero car shot" --out reference`
   (or WebSearch film stills / product photos of that shot type), then match what you see.

## What the numbers mean (framing knobs)
- azimuth: 0=front, 90=right, 180=back, negative=left. A 3/4 (~30-45) shows TWO sides + depth.
- elevation: +above / -below. LOW (0-10) = heroic/imposing for big subjects (ships, mechs,
  buildings); 15-30 = tabletop props; ~8-12 = characters at eye line; 40-70 = layouts / plans /
  making a subject look small; NEGATIVE = worm's-eye drama.
- lens_mm: 24-35 = wide, dramatic, exaggerated scale + perspective (environments, hero-low);
  50 = natural; 85-135 = compressed, flattering, ISOLATES the subject (portraits, product).
- margin: framing tightness — 1.1 tight, 1.3 normal, 1.6 breathing room, 2.0+ establishing.
  Below 1.0 crops INTO the subject for a detail shot.
- shift_x/shift_y: rule-of-thirds nudge (puts the subject off-centre without re-aiming).
- roll: dutch tilt (degrees) — tension/energy, use sparingly.
- fstop: OPTIONAL depth-of-field. Low (1.4-2.8) = shallow, creamy bokeh that ISOLATES the
  subject — for portrait / beauty / macro / single-creature; note it also softens the front &
  back of the fitted subject, so reserve it for those. 4-8 = moderate. 11-16 = deep, all sharp
  (architecture / landscape / establishing). Omit = everything sharp (the default).

## Shot families (browse by the LOOK you want)
- **Signature 3/4 & Hero** — default flattering three-quarter and hero angles for almost any subject.
- **Film Shot Sizes & Angles** — continuity-editing coverage: full/medium/close sizes + power/vulnerable angles.
- **Portrait & Tele Isolation** — face/figure geometry with telephoto compression and shallow depth.
- **Fine-Art & Editorial Composition** — negative space, chiaroscuro, frame-in-frame, diagonal moods.
- **Product & Commercial** — packshots, tabletop, beauty and macro-detail angles for goods and food.
- **Architecture & Interior** — one/two-point perspective, elevations, aerials, interior corners.
- **Automotive & Vehicle** — press-kit hero, profile, detail and dynamic angles tuned for vehicles.
- **Key Art & Cinematic** — box-art splash, monumental reveals, turntables, presentation/loadout cards.
- **Wildlife & Macro Nature** — eye-line tele portraits, habitat wides, worm/canopy angles, 1:1 macro.
(The `# --- Family ---` comment headers in SHOTS below group every shot under these.)

## Subject -> shots to try (then contact-sheet and choose)
| subject | candidates (contact-sheet, then pick) |
|---|---|
| product | packshot_hero_3q_tele, tabletop_45, pack_straight_tele, macro_texture_extreme, low_glamour_hero, flat_lay_topdown |
| prop | hero_3q, tabletop_45, insert_cut_in, chiaroscuro_low, negative_space_minimal |
| vehicle | car_front_3q_low_hero, car_side_profile_pure, car_high_3q_beauty, car_rear_3q_low, car_wheel_detail, top_plan |
| character | hero_3q, portrait_85, cowboy_american, rembrandt_short, close_up_emotional, keyart_hero_wide_left |
| creature | low_creature_hero_wide, animal_portrait_tele, tele_isolation_3q, eye_line_ground_low, hero_3q, backlit_rim_silhouette |
| building | two_point_corner_low, dusk_hero, hero_low_dramatic, aerial_3q, courtyard_context, facade_elevation |
| interior | one_point_corridor, interior_wide_corner, upshot_ceiling, threshold_inside_out, plan_roof |
| environment | extreme_wide_establishing, habitat_establishing, courtyard_context, aerial_3q, establishing_env_deep |
| food | food_drink_high_3q, flat_lay_topdown, macro_texture_extreme, tabletop_45, medium_close_up |
| weapon | weapon_loadout_profile, big_close_up, concept_orthoish_side, insert_cut_in |
| group | keyart_hero_wide_left, establishing_env_deep, diorama_birdseye, extreme_wide_establishing, birds_eye |
| plant | top_down_botanical, macro_extreme_detail, negative_space_minimal, dew_macro_dutch, habitat_establishing |
| statue | hero_3q, strong_profile_tele, chiaroscuro_low, detail_facade, rembrandt_short, epic_low_ultrawide |
| jewelry | jewelry_macro_isolate, beauty_tele_bokeh, macro_texture_extreme, pack_straight_tele |

## bpy: base FOV-fit camera (reused by the picker)
""" + _FN_CAMERA + """
## bpy: the shot catalog + picker
```python
import bpy, math, os
from mathutils import Matrix

# az=azimuth deg, el=elevation deg, lens=mm, m=margin, sx/sy=thirds shift, roll=dutch deg, fstop=DOF f-number
SHOTS = {
    # --- Signature 3/4 & Hero ---
    "hero_3q":                      dict(az=35, el=18, lens=50, m=1.25, sx=-0.1, sy=0.04),
    "hero_3q_left":                 dict(az=-35, el=18, lens=50, m=1.25, sx=0.1, sy=0.04),
    "hero_3q_low":                  dict(az=35, el=7, lens=35, m=1.3, sy=0.06),
    "hero_3q_high":                 dict(az=42, el=38, lens=50, m=1.3),
    "back_3q":                      dict(az=145, el=18, lens=50, m=1.3),
    "low_hero":                     dict(az=25, el=5, lens=28, m=1.35, sy=0.05),
    # --- Film Shot Sizes & Angles ---
    "front":                        dict(az=0, el=0, lens=85, m=1.4),
    "side":                         dict(az=90, el=0, lens=85, m=1.4),
    "back":                         dict(az=180, el=0, lens=85, m=1.4),
    "worm_eye":                     dict(az=20, el=-8, lens=24, m=1.3),
    "dutch_left":                   dict(az=35, el=16, lens=35, m=1.3, roll=-12),
    "dutch_right":                  dict(az=-35, el=16, lens=35, m=1.3, roll=12),
    "establishing_wide":            dict(az=40, el=12, lens=24, m=2.2),
    "char_full_3q":                 dict(az=32, el=8, lens=50, m=1.2),
    "extreme_wide_establishing":    dict(az=38, el=14, lens=18, m=2.7, sy=0.1, fstop=11.0),
    "full_shot":                    dict(az=30, el=6, lens=40, m=1.55, sy=0.04),
    "cowboy_american":              dict(az=33, el=4, lens=40, m=1.18, sx=-0.06, sy=0.08),
    "medium_shot":                  dict(az=28, el=3, lens=50, m=1.0, sx=-0.05, sy=0.05),
    "medium_close_up":              dict(az=24, el=4, lens=85, m=0.82, sx=-0.07, sy=0.04, fstop=4.0),
    "close_up_emotional":           dict(az=18, el=5, lens=100, m=0.68, sx=-0.06, fstop=2.8),
    "big_close_up":                 dict(az=10, el=2, lens=135, m=0.6, fstop=2.0),
    "insert_cut_in":                dict(az=35, el=22, lens=100, m=0.7, fstop=2.8),
    "low_angle_power":              dict(az=22, el=-6, lens=28, m=1.3, sy=0.07),
    "high_angle_vulnerable":        dict(az=26, el=45, lens=50, m=1.4, sy=-0.04),
    "eye_level_neutral":            dict(az=20, el=1, lens=50, m=1.22, sx=-0.05, sy=0.02),
    "ots_feel":                     dict(az=58, el=7, lens=85, m=1.08, sx=0.12, fstop=2.8),
    "pov_low_drama":                dict(az=15, el=-14, lens=24, m=1.45, sy=0.1, roll=4),
    "dutch_unease":                 dict(az=28, el=10, lens=40, m=1.28, sx=0.06, roll=16),
    # --- Portrait & Tele Isolation ---
    "portrait_85":                  dict(az=25, el=12, lens=85, m=1.15, sx=-0.08),
    "profile":                      dict(az=90, el=8, lens=85, m=1.3),
    "char_portrait":                dict(az=22, el=10, lens=100, m=0.9, sx=-0.08),
    "rembrandt_short":              dict(az=40, el=14, lens=85, m=1.0, sx=-0.07, sy=0.03, fstop=2.0),
    "broad_two_thirds":             dict(az=28, el=10, lens=105, m=1.05, sx=0.07, sy=0.02, fstop=2.8),
    "strong_profile_tele":          dict(az=90, el=4, lens=135, m=0.95, sx=0.1, fstop=2.5),
    "thirds_phi_left":              dict(az=33, el=12, lens=85, m=1.3, sx=0.13, sy=0.08),
    "thirds_phi_right":             dict(az=-33, el=12, lens=85, m=1.3, sx=-0.13, sy=0.08),
    "close_eyes_thirds":            dict(az=18, el=15, lens=100, m=0.72, sx=-0.06, sy=0.1, fstop=2.0),
    "phi_profile_lead_left":        dict(az=-90, el=6, lens=135, m=1.1, sx=-0.12, sy=0.02, fstop=2.5),
    "tele_compression_iso":         dict(az=24, el=9, lens=160, m=1.1, sx=-0.06, sy=0.02, fstop=2.8),
    "hero_phi_high_short":          dict(az=-42, el=28, lens=70, m=1.2, sx=-0.1, sy=-0.06),
    # --- Fine-Art & Editorial Composition ---
    "thirds_left":                  dict(az=35, el=18, lens=50, m=1.35, sx=0.12),
    "thirds_right":                 dict(az=-35, el=18, lens=50, m=1.35, sx=-0.12),
    "centered_symmetry":            dict(az=0, el=6, lens=50, m=1.35),
    "negative_space_minimal":       dict(az=20, el=6, lens=70, m=2.4, sx=0.14, sy=0.1),
    "overhead_flatlay_angled":      dict(az=0, el=78, lens=60, m=1.25),
    "diagonal_tension":             dict(az=52, el=20, lens=35, m=1.3, sx=0.1, sy=0.07, roll=9),
    "chiaroscuro_low":              dict(az=38, el=3, lens=50, m=1.15, sx=-0.09, sy=0.04, fstop=2.8),
    "frame_in_frame_lead":          dict(az=30, el=8, lens=28, m=2.0, sx=0.08, sy=0.06, fstop=11.0),
    # --- Product & Commercial ---
    "macro_detail":                 dict(az=30, el=16, lens=100, m=0.75),
    "packshot_hero_3q_tele":        dict(az=38, el=14, lens=100, m=1.2, sx=-0.08, sy=0.03),
    "pack_straight_tele":           dict(az=0, el=2, lens=135, m=1.25),
    "tabletop_45":                  dict(az=45, el=24, lens=85, m=1.3, sx=-0.06),
    "flat_lay_topdown":             dict(az=0, el=89, lens=50, m=1.45),
    "low_glamour_hero":             dict(az=30, el=4, lens=85, m=1.15, sy=0.06, fstop=2.0),
    "beauty_tele_bokeh":            dict(az=22, el=10, lens=135, m=1.1, sx=-0.07, fstop=1.8),
    "macro_texture_extreme":        dict(az=28, el=18, lens=100, m=0.62, fstop=4.0),
    "reflection_studio":            dict(az=12, el=6, lens=85, m=1.55, sy=-0.05),
    "food_drink_high_3q":           dict(az=35, el=32, lens=85, m=1.3, sx=-0.05, fstop=2.8),
    "pack_label_3q":                dict(az=28, el=8, lens=100, m=1.2, sx=-0.1, sy=0.02),
    "jewelry_macro_isolate":        dict(az=20, el=22, lens=150, m=0.8, sx=-0.05, fstop=2.2),
    "luxury_compressed_200":        dict(az=85, el=6, lens=200, m=1.25),
    "three_quarter_back_pack":      dict(az=150, el=12, lens=85, m=1.25, sx=0.06),
    "hero_dutch_dynamic":           dict(az=40, el=12, lens=50, m=1.2, sx=-0.06, sy=0.04, roll=9),
    # --- Architecture & Interior ---
    "top_plan":                     dict(az=0, el=87, lens=50, m=1.4),
    "birds_eye":                    dict(az=30, el=62, lens=35, m=1.4),
    "arch_2point":                  dict(az=30, el=4, lens=28, m=1.6, sy=0.1),
    "arch_hero_low":                dict(az=25, el=3, lens=24, m=1.7, sy=0.12),
    "one_point_corridor":           dict(az=0, el=0, lens=24, m=1.9, fstop=11.0),
    "two_point_corner_low":         dict(az=38, el=3, lens=20, m=1.7, sy=0.1, fstop=11.0),
    "facade_elevation":             dict(az=0, el=2, lens=50, m=1.6, fstop=11.0),
    "upshot_ceiling":               dict(az=10, el=72, lens=18, m=1.5, fstop=8.0),
    "hero_low_dramatic":            dict(az=28, el=-6, lens=16, m=1.55, sy=0.12, fstop=11.0),
    "dusk_hero":                    dict(az=42, el=5, lens=24, m=1.85, sy=0.08, fstop=11.0),
    "courtyard_context":            dict(az=35, el=10, lens=28, m=2.4, fstop=13.0),
    "aerial_3q":                    dict(az=40, el=48, lens=35, m=1.9, fstop=11.0),
    "interior_wide_corner":         dict(az=35, el=-2, lens=18, m=2.0, fstop=13.0),
    "detail_facade":                dict(az=30, el=8, lens=85, m=0.8, fstop=5.6),
    "plan_roof":                    dict(az=0, el=84, lens=28, m=1.55, fstop=11.0),
    "threshold_inside_out":         dict(az=8, el=0, lens=35, m=1.7, sx=0.06, fstop=13.0),
    "sweeping_diagonal":            dict(az=55, el=14, lens=24, m=1.7, sx=0.1, fstop=11.0),
    "dutch_tower":                  dict(az=30, el=12, lens=20, m=1.6, roll=10, fstop=11.0),
    # --- Automotive & Vehicle ---
    "vehicle_3q_low":               dict(az=48, el=6, lens=35, m=1.35, sy=0.05),
    "car_front_3q_low_hero":        dict(az=38, el=4, lens=35, m=1.25, sy=0.05),
    "car_rear_3q_low":              dict(az=142, el=5, lens=50, m=1.25, sy=0.04),
    "car_aggressive_front_3q_wide": dict(az=48, el=2, lens=24, m=1.35, sy=0.06),
    "car_side_profile_pure":        dict(az=90, el=1, lens=135, m=1.2),
    "car_high_3q_beauty":           dict(az=40, el=28, lens=50, m=1.3, sx=-0.06),
    "car_wheel_detail":             dict(az=62, el=6, lens=100, m=0.7, fstop=2.8),
    "car_badge_macro":              dict(az=20, el=10, lens=100, m=0.62, sx=-0.05, fstop=2.0),
    "car_low_wide_dramatic":        dict(az=30, el=-4, lens=20, m=1.4, sy=0.08),
    "car_three_quarter_roll":       dict(az=36, el=8, lens=35, m=1.3, sy=0.05, roll=-8),
    "car_nose_detail_low":          dict(az=15, el=-2, lens=85, m=0.8, sy=0.04, fstop=4.0),
    "car_rear_quarter_panel":       dict(az=118, el=10, lens=85, m=0.85, fstop=4.0),
    "car_front_centered_menace":    dict(az=0, el=3, lens=50, m=1.3, sy=0.04),
    "car_roofline_high_rear_3q":    dict(az=150, el=32, lens=50, m=1.3),
    # --- Key Art & Cinematic ---
    "keyart_hero_wide_left":        dict(az=-38, el=6, lens=18, m=2.4, sx=0.14, sy=0.05),
    "keyart_hero_wide_right":       dict(az=40, el=6, lens=18, m=2.4, sx=-0.14, sy=0.05),
    "epic_low_ultrawide":           dict(az=22, el=-6, lens=16, m=1.9, sy=0.1),
    "splash_centered_symmetry":     dict(az=0, el=3, lens=24, m=1.7, sy=0.08),
    "diorama_birdseye":             dict(az=35, el=58, lens=40, m=1.8, fstop=2.2),
    "turntable_beauty_3q":          dict(az=45, el=12, lens=85, m=1.2),
    "weapon_loadout_profile":       dict(az=90, el=4, lens=100, m=1.15, sx=-0.1, fstop=2.8),
    "concept_orthoish_front":       dict(az=2, el=2, lens=150, m=1.3),
    "concept_orthoish_side":        dict(az=90, el=1, lens=150, m=1.3),
    "rim_silhouette_back_3q":       dict(az=150, el=10, lens=50, m=1.5, sx=0.1),
    "establishing_env_deep":        dict(az=46, el=8, lens=20, m=2.6, sx=0.12, sy=0.1, fstop=13.0),
    # --- Wildlife & Macro Nature ---
    "eye_line_ground_low":          dict(az=12, el=-4, lens=200, m=1.2, sy=0.03, fstop=4.0),
    "animal_portrait_tele":         dict(az=22, el=2, lens=180, m=1.0, sx=-0.06, fstop=2.8),
    "backlit_rim_silhouette":       dict(az=160, el=3, lens=135, m=1.45, sy=0.04),
    "habitat_establishing":         dict(az=38, el=9, lens=24, m=2.6, sy=0.08, fstop=11.0),
    "macro_extreme_detail":         dict(az=18, el=14, lens=100, m=0.62, sx=-0.05, fstop=5.6),
    "top_down_botanical":           dict(az=0, el=80, lens=60, m=1.15, fstop=8.0),
    "low_creature_hero_wide":       dict(az=28, el=-2, lens=28, m=1.4, sy=0.06),
    "tele_isolation_3q":            dict(az=40, el=6, lens=150, m=1.15, sx=-0.07, fstop=3.2),
    "profile_running_track":        dict(az=88, el=-1, lens=200, m=1.5, sx=0.1, fstop=4.0),
    "dew_macro_dutch":              dict(az=25, el=8, lens=100, m=0.7, roll=10, fstop=4.0),
    "underbelly_worm":              dict(az=15, el=-18, lens=24, m=1.3, sy=0.05),
    "canopy_lookdown":              dict(az=35, el=48, lens=70, m=1.55, fstop=5.6),
    "frontal_alert_tele":           dict(az=4, el=0, lens=200, m=1.25, fstop=3.5),
    "golden_hour_environ_low":      dict(az=130, el=6, lens=50, m=1.9, sy=0.07, fstop=8.0),
}

def place_shot(target, shot, name=None, samples=256, resolution=(1920, 1080)):
    s = SHOTS[shot] if isinstance(shot, str) else shot
    nm = name or ("Cam_" + (shot if isinstance(shot, str) else "custom"))
    cam = setup_hero_camera(target, name=nm, azimuth_deg=s["az"], elevation_deg=s["el"],
                            lens_mm=s["lens"], margin=s.get("m", 1.3),
                            samples=samples, resolution=resolution, fstop=s.get("fstop"))
    cam.data.shift_x = s.get("sx", 0.0); cam.data.shift_y = s.get("sy", 0.0)
    if s.get("roll"):
        cam.matrix_world = cam.matrix_world @ Matrix.Rotation(math.radians(s["roll"]), 4, "Z")
    bpy.context.scene.camera = cam
    return cam

def render_contact_sheet(target, out_dir, shots=None, samples=40, res=(560, 360)):
    # Render several catalog shots small so you can LOOK and pick the best-composed one.
    shots = shots or ["hero_3q", "low_hero", "hero_3q_high", "side", "portrait_85", "birds_eye", "establishing_wide", "front"]
    os.makedirs(out_dir, exist_ok=True); paths = []
    for sh in shots:
        place_shot(target, sh, samples=samples, resolution=res)
        p = os.path.join(out_dir, "shot_%s.png" % sh)
        bpy.context.scene.render.filepath = p
        bpy.ops.render.render(write_still=True); paths.append(p)
    return paths
# Then: Read each shot_*.png, pick the best-composed, and
#   place_shot(target, "<winner>", samples=256, resolution=(1920, 1080)) for the final render.
```

## For a whole scene (multiple objects)
Frame the WHOLE group: make a temporary Empty at the combined bounds (or briefly join a copy) and
pass it as `target`, so the shot fits everything — then delete the helper.

## Gotchas
- The catalog FITS the subject (FOV-based) so nothing is cut — but a fitted BAD ANGLE still looks
  bad. Always contact-sheet 3-5 and choose; don't trust one guess.
- roll/dutch, macro crops (m<1.0) and shallow DOF are seasoning, not defaults.
- shift_x/shift_y are in sensor-height units; +-0.10 is a gentle thirds nudge.
- fstop focuses the subject CENTRE. Shallow values blur the subject's own near/far edges (intended
  for macro/portrait/product); don't put a shallow fstop on a whole building or vehicle.

## Validated result
Built on setup_hero_camera (validated on 5.1.2); shift_x/shift_y + matrix roll + camera DOF
(use_dof / focus_distance / aperture_fstop) are standard camera-data ops. confidence high."""),

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

    ("gen3d-import-and-place",
     "MOST props (organic or hard-surface) — AI-generate the mesh as the default instead of hand-modelling.",
     "high",
     """# Generate & Import a 3D Asset (AI-generated mesh)

## When to use
Your DEFAULT for MOST props — organic or hard-surface: barrels, statues, busts, creatures,
plants, furniture, characters, weapons, food, ornaments, tools, crates, signage. Reach for it
FIRST (LLM hand-modelling is weak at organic/detail-dense shapes, and gen3d gives a better base
on most hard-surface too); the ~30-60s (sometimes up to ~10 min) is fine and expected, never a
reason to fall back to a hand-rolled primitive. Hand-model/kitbash only when it genuinely wins —
clean simple hard-surface or precise modular kits; download a CC0 asset when a good one already
exists. Image->3D from ONE clean isolated reference photo on a plain background is best (a busy
scene photo yields a cluttered mesh).

## Steps
1. EASIEST — text->3D (NO reference image; the local server makes a clean image internally):
   `python -m blendahbot.gen3d "a weathered wooden barrel" --out assets/barrel.glb`
2. OR image->3D to match a specific look — use a CLEAN single object on a plain background; a
   busy scene photo reconstructs the WHOLE scene into a cluttered mesh, so crop to the object:
   `python -m blendahbot.gen3d "wooden barrel" --image reference/barrel.png --out assets/barrel.glb`
   (prints the GLB path to stdout; needs the local Hunyuan3D server on :8081, or TRIPO_API_KEY).
3. PREVIEW & VET the mesh IN ISOLATION before importing — generation is unpredictable, so never
   import blind. See "## Preview & vet before you place it" below; decide ACCEPT / REGENERATE /
   FALL BACK.
4. Import + place the ACCEPTED mesh with the snippet below.
5. gen3d returns a BAKED-TEXTURE GLB by default (2048² PBR painted onto the mesh) — the glTF
   import builds the material automatically, so just light and frame it. ONLY if you passed
   --no-texture (a fast grey blockout) do you UV-unwrap and apply a PolyHaven PBR set
   (see pbr-material-from-polyhaven).

## Compare two generators and pick the best (optional)
When quality matters or the first mesh is weak, generate the SAME asset from two backends and
choose the better one yourself — you can see them, so you are the judge:
```
python -m blendahbot.gen3d "wooden barrel" --image reference/barrel.png --compare hunyuan,trellis --out assets/barrel.glb
```
- It runs the backends SEQUENTIALLY (two big local models can't share the GPU) and prints a JSON
  manifest to stdout: `{"candidates":[{"backend":"hunyuan","path":"assets/barrel.hunyuan.glb"}, ...]}`.
- A backend that isn't running / has no key is simply omitted — use whatever candidates came back.
  `trellis` is the local TRELLIS.2 server and is **image->3D only**, so pass `--image` when comparing
  with it.
- Then CHOOSE: import each candidate path into a temporary collection, render/screenshot each, compare
  against the request and any reference photo (geometry correctness, texture quality, clean silhouette),
  keep the best, and DELETE the losers' objects + files. Import the winner with the snippet below.
- Don't compare for every prop — it doubles time and VRAM. Use it when it earns its cost.

## Prompting (text->3D) — this dominates quality
The text prompt drives an internal text->image step, so write it like a clean product photo of
ONE object. THIS SERVER TRUNCATES the prompt to ~60 characters and auto-appends "white
background, best quality" — keep it SHORT and front-loaded.
- ORDER, most important first: object -> material -> finish/colour -> 1-2 distinctive features.
- Be SPECIFIC about material: "rough oak", "brushed steel", "glazed ceramic" — not "wood"/"metal".
- ONE object only. NEVER describe a scene or multiple objects ("a barrel in a cellar" makes a
  whole cellar mesh). Generate each prop separately, then assemble them yourself.
- No vague words ("nice", "cool"), no negatives ("no handle"), no abstract style without material.
- Good: `weathered oak wine barrel, iron hoops` · `brushed steel desk lamp, matte black base` ·
  `glazed ceramic teapot, floral pattern`. Bad: `a barrel` · `beautiful furniture` ·
  `a kitchen with a table and chairs` · `metal thing`.
For image->3D inputs: single isolated object, plain/neutral background, EVEN lighting (harsh
shadows bake into the texture), >=1024px, a 3/4 angle for depth. Avoid glass/transparent and
thin wires/hair (hard to reconstruct).

## Preview & vet before you place it (DO THIS EVERY TIME)
A generated mesh is unpredictable: wrong object, blobby/holed/melted geometry, a whole baked-in
SCENE instead of one prop, garbled/seam-ripped texture, or wrong proportions. NEVER import one
blind — look at it ALONE first, then ACCEPT / REGENERATE / FALL BACK.

The `--preview` flag renders the mesh from several angles in a SEPARATE, throwaway headless Blender
(`blender --background`) — it never touches your live session (can't disturb your scene or be
disturbed by it), needs no add-on, and is fast (~5s):
```
python -m blendahbot.gen3d "weathered oak barrel, iron hoops" --out assets/barrel.glb --preview <round_dir>/preview
```
It writes the GLB, renders `shot_*.png` into `<round_dir>/preview/`, and prints their paths. To
preview a mesh you ALREADY have (a CC0 download or a `--compare` candidate):
`python -m blendahbot.gen3d.preview assets/barrel.glb --out <round_dir>/preview` (auto-detects
blender.exe; set BLENDAHBOT_BLENDER or pass `--blender <path>` if it can't find it). Do NOT preview
by importing the GLB through execute_blender_code into your LIVE scene — a heavy glTF import through
the live add-on can crash it; the headless `--preview` avoids that entirely.

Then READ every `shot_*.png` and judge with this checklist — REJECT if any is true:
- not clearly the requested object / unrecognizable
- a whole baked-in scene (ground plane, multiple props, a room) instead of ONE object
- holes, gaps, a missing back/side, melted/blobby forms, intersecting or exploded parts
- garbled texture: smeared/seam-ripped UVs, wrong colours, baked-in shadows, or bare grey
- clearly wrong proportions vs. the real object / references

DECIDE:
- ACCEPT  -> import it with the snippet below.
- REGENERATE -> for an unlucky blob/holes/wrong-object, re-run with a NEW `--seed`; for a baked
  scene / wrong proportions / missing feature, the PROMPT is the lever — rewrite it shorter,
  single-object, material-first (or switch to image->3D from a clean cropped reference). Cap at
  ~3 attempts.
- FALL BACK after ~3 bad attempts (or for glass/thin-wire subjects gen3d handles poorly):
  hand-model/kitbash, or download a CC0 GLB/PolyHaven .blend; or accept the best-of-N if serviceable.

Want an INDEPENDENT judge to do this automatically? Add `--vet` (it renders the preview, has a
separate critic score it, and auto-regenerates a bad asset, keeping the best):
`python -m blendahbot.gen3d "..." --out assets/x.glb --vet`. It still prints the final GLB path to
stdout; a `[gen3d] UNVETTED ...` line on stderr means even the best attempt was poor — then fall back.

## bpy snippet
```python
import bpy
from mathutils import Vector

def import_and_place(glb_path, target_size=1.0, floor_z=0.0, name=None):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=glb_path)
    meshes = [o for o in bpy.data.objects if o not in before and o.type == 'MESH']
    if not meshes:
        raise RuntimeError("import produced no mesh")
    bpy.ops.object.select_all(action='DESELECT')
    for o in meshes: o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    if len(meshes) > 1: bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    if name: obj.name = name
    bpy.context.view_layer.update()
    longest = max(obj.dimensions) or 1.0          # gen meshes import at arbitrary scale
    obj.scale *= (target_size / longest)
    bpy.ops.object.transform_apply(scale=True)
    bpy.context.view_layer.update()
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    obj.location.z += floor_z - min(v.z for v in corners)   # drop to floor
    bpy.ops.object.shade_smooth()
    has_mats = bool(obj.data.materials) and any(obj.data.materials)
    return obj, has_mats
```

## Gotchas
- INPUT QUALITY DOMINATES: image->3D of a busy/multi-object photo = a junk mesh. Use ONE clean
  isolated object on a plain background (crop first if needed), or use text->3D.
- Generated meshes import at ARBITRARY scale — always normalize to a real size (a barrel ~0.9 m,
  not 40 m) and DROP TO FLOOR, or it floats / dwarfs the scene.
- Default output is TEXTURED (has_mats True); glTF auto-builds the Principled BSDF — use as-is.
  Only --no-texture output is bare (has_mats False) -> apply a PolyHaven PBR set.
- Vary instances (different --seed / --image) so multiple props aren't identical (see varied-instances).
- Generation replaces MODELLING, not the rest — still ground, scale, light, and frame it.

## Validated result
import_scene.gltf + join/normalize/ground are standard 5.1. confidence medium."""),

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
Whenever you place MANY of something — trees, rocks, bushes, crowds, debris, houses. Generate a
SMALL pool of unique variants once, then scatter cheap instances of the pool with per-instance
variation. This is about REALISM and art-control, not speed: identical copies read as fake and a
uniform crowd can't be directed, so a curated unique pool beats 100 separate clones.

## Library + scatter (THE way to populate a scene)
Build a pool of ~3-10 UNIQUE base meshes (generate via gen3d / download CC0 / model a few), then
scatter many instances. 5-10 unique trees -> a forest of 100; one crate -> a stack of 30.
```python
import bpy, random, math
from mathutils import Vector

def scatter_pool(pool, count, area=(12.0, 12.0), floor_z=0.0, scale_jitter=0.3, seed=0):
    # pool = a few unique base mesh objects. Places `count` INSTANCES (linked mesh data =
    # cheap memory) across `area`, each with random pick + transform, grounded to the floor.
    random.seed(seed); placed = []
    for _ in range(count):
        src = pool[random.randrange(len(pool))]
        o = src.copy()                          # linked duplicate: shares mesh data, cheap
        bpy.context.collection.objects.link(o)
        o.location = (random.uniform(-area[0]/2, area[0]/2),
                      random.uniform(-area[1]/2, area[1]/2), 0.0)
        s = 1.0 + random.uniform(-scale_jitter, scale_jitter)
        o.scale = (s, s, s * random.uniform(0.9, 1.15))
        o.rotation_euler = (random.uniform(-0.12, 0.12), random.uniform(-0.12, 0.12),
                            random.uniform(0, 2 * math.pi))
        bpy.context.view_layer.update()
        zmin = min((o.matrix_world @ Vector(c)).z for c in o.bound_box)
        o.location.z += floor_z - zmin          # drop each to the floor
        placed.append(o)
    return placed
```
For a terrain, sample positions on its surface (or use Geometry Nodes "Distribute Points on
Faces"); space big objects out (poisson/grid jitter) so they don't overlap.

## Per-variant techniques (to build the pool / add more variety)
1. PROCEDURAL with a different SEED per instance — best variation. Trees: enable the Sapling
   add-on and vary the seed. In Blender 5.x Sapling ships as the EXTENSION
   `bl_ext.blender_org.sapling_tree_gen` (the legacy `add_curve_sapling_3` module name is gone),
   so try the new name first and fall back; if it isn't installed, fetch it on demand (see the
   `acquire-extensions-and-libraries` skill: `python -m blendahbot.addons install sapling_tree_gen`).
   ```python
   import bpy
   for _m in ("bl_ext.blender_org.sapling_tree_gen", "add_curve_sapling_3", "add_curve_sapling"):
       try:
           bpy.ops.preferences.addon_enable(module=_m); break   # the one that exists wins
       except Exception:
           continue
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

    ("acquire-extensions-and-libraries",
     "When the build needs a capability base Blender lacks — a tree/scatter/CAD/format add-on, an "
     "importer/exporter, a node or shader pack, an asset library to drag from, or a Python package "
     "your bpy code imports. Download + install it into THIS session, on demand.",
     "high",
     """# Acquire Extensions, Add-ons, Asset Libraries & Python Packages (on demand)

## When to use
Whenever the task needs something the base Blender install doesn't have — a procedural tree or
scatter generator, a STEP/CAD or other format importer, a node/shader pack, an asset library to
pull pre-made props from, or a Python package (`trimesh`, `shapely`, `scipy`, …) you want to
`import` from bpy. You are NOT limited to what ships with Blender: fetch the capability, then use
it. Everything installs into the LIVE session, so new operators/panels/modules work immediately.

## The tool (preferred — one tested command each)
```
python -m blendahbot.addons search "tree generator"      # find extensions on extensions.blender.org
python -m blendahbot.addons install sapling_tree_gen      # by extension id (exact)
python -m blendahbot.addons install "scatter objects"     # or let it best-match a query
python -m blendahbot.addons install-url https://host/some_addon.zip   # trusted sources only
python -m blendahbot.addons enable bl_ext.blender_org.node_wrangler    # turn on an installed/bundled module
python -m blendahbot.addons list                          # what's already enabled
python -m blendahbot.addons asset-library "C:/packs/kit" --name Kit    # register an asset-library folder
python -m blendahbot.addons pip trimesh shapely           # into Blender's OWN Python (bpy can import it)
```
- `install` prints the enabled MODULE NAME (e.g. `bl_ext.user_default.sapling_tree_gen`). The
  add-on's operators/panels are live right away — call them via `bpy.ops...` or its N-panel.
- `install` is key-free and only pulls from the official, vetted Extensions Platform. It
  short-circuits (no duplicate) if the extension is already enabled. `install-url` runs arbitrary
  third-party code, so point it only at sources you trust.
- `pip` targets Blender's bundled interpreter via `--user`, so a freshly installed package is
  importable from `execute_blender_code` immediately — not the project venv.

## Inline bpy fallback (when you'd rather install within your own bpy flow)
CRITICAL: over the MCP `execute_blender_code` path `sys.stdout` is None, so a third-party add-on
that `print()`s inside its `register()` will CRASH mid-registration. ALWAYS redirect stdout to a
real buffer around the install/enable, or the add-on installs half-registered and broken:
```python
import bpy, sys, io
def install_extension_zip(zip_path, repo="user_default"):
    so, se = sys.stdout, sys.stderr; sys.stdout = sys.stderr = io.StringIO()  # see CRITICAL above
    before = set(bpy.context.preferences.addons.keys())
    try:
        bpy.ops.extensions.package_install_files(
            filepath=zip_path, repo=repo, enable_on_install=True, overwrite=True)
    finally:
        sys.stdout, sys.stderr = so, se
    return sorted(set(bpy.context.preferences.addons.keys()) - before)   # ['bl_ext.user_default.<id>']
# Online by id (needs use_online_access + a synced repo):
#   bpy.ops.extensions.repo_refresh_all()
#   bpy.ops.extensions.package_install(repo_index=<i of 'blender_org'>, pkg_id="<id>", enable_on_install=True)
# Legacy .py/.zip add-on (not an Extensions-Platform package):
#   bpy.ops.preferences.addon_install(filepath=..., overwrite=True); bpy.ops.preferences.addon_enable(module=...)
```

## Gotchas
- The stdout-None print crash above is the #1 trap — prefer the `blendahbot.addons` CLI (it bakes
  the redirect in), or wrap your own call exactly as shown.
- Module naming: an Extensions-Platform add-on enables as `bl_ext.<repo>.<id>` (repo = `blender_org`
  for online installs, `user_default` for local-file installs) — NOT the bare id. Check with `list`.
- In Blender 5.x several once-bundled add-ons are now extensions (e.g. Sapling =
  `bl_ext.blender_org.sapling_tree_gen`); if `addon_enable` fails on a legacy name, `install` it.
- Online install needs `preferences.system.use_online_access = True` (on by default here).
- Don't reinstall what's already there — run `list` (or `install`, which auto-skips duplicates).

## Validated result
package_install_files / package_install / addon_install / asset_library_add signatures and the
`bl_ext.<repo>.<id>` module naming verified live on Blender 5.1.2, including the stdout-redirect
fix for register() prints. confidence high."""),
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

"""Direct socket client for the Blender MCP add-on.

The blendahbot agent normally talks to Blender through the ``blender-mcp`` MCP
server. This module is the *out-of-band* path the orchestrator uses for things
that must not depend on the model's cooperation:

* a fast pre-flight connectivity check,
* a guaranteed render-to-file fallback (so the critic always has an image), and
* saving the final ``.blend``.

The wire protocol is the add-on's own: a JSON request terminated by a NUL byte,
``{"type": "execute", "code": <python>, "strict_json": <bool>}``, and a JSON
response (also NUL-terminated) of the form
``{"status": "ok"|"error", "result": ..., "message": ..., "stdout": ..., "stderr": ...}``.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path


class BlenderUnavailable(RuntimeError):
    """Raised when Blender cannot be reached or returns an unusable response."""


@dataclass
class BlenderClient:
    host: str = "localhost"
    port: int = 9876
    timeout: float = 300.0

    # -- low level ---------------------------------------------------------

    def _send(self, payload: dict[str, object]) -> dict[str, object]:
        request = (json.dumps(payload) + "\0").encode("utf-8")
        buf = bytearray()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect((self.host, self.port))
                sock.sendall(request)
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                    if b"\0" in buf:
                        break
        except ConnectionRefusedError as ex:
            raise BlenderUnavailable(
                f"Cannot connect to Blender at {self.host}:{self.port}. Open Blender, "
                "enable the Blender MCP add-on and start its server."
            ) from ex
        except OSError as ex:
            raise BlenderUnavailable(
                f"Socket error talking to Blender at {self.host}:{self.port}: {ex}"
            ) from ex

        if not buf:
            raise BlenderUnavailable("Empty response from Blender.")
        line, _sep, _rest = buf.partition(b"\0")
        try:
            return json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as ex:
            raise BlenderUnavailable(f"Invalid response from Blender: {ex}") from ex

    def execute(self, code: str, *, strict_json: bool = False) -> dict[str, object]:
        """Run ``code`` in Blender and return the add-on's response dict.

        Raises :class:`BlenderUnavailable` on a transport problem; a Python error
        *inside* Blender comes back as ``{"status": "error", ...}`` instead.
        """
        return self._send({"type": "execute", "code": code, "strict_json": strict_json})

    # -- high level --------------------------------------------------------

    def ping(self) -> tuple[bool, str]:
        """Return ``(ok, detail)``. Never raises."""
        try:
            resp = self.execute(
                "import bpy; print('blender', bpy.app.version_string)"
            )
        except BlenderUnavailable as ex:
            return False, str(ex)
        if resp.get("status") == "ok":
            out = str(resp.get("stdout") or resp.get("result") or "").strip()
            return True, out or "connected"
        return False, str(resp.get("message") or "Blender returned an error")

    def render_still(self, filepath: str, *, width: int = 1280, height: int = 720) -> tuple[bool, str]:
        """Render the scene to ``filepath`` (PNG), creating a camera/light if needed.

        Returns ``(ok, detail)`` where ``detail`` is the absolute output path on
        success or the failure reason otherwise. This is the safety net used when
        the agent did not leave a usable render behind; it is deliberately
        forgiving rather than artful. ``filepath`` is resolved to an absolute path
        because Blender runs in a different process with its own cwd.
        """
        path = str(Path(filepath).resolve()).replace("\\", "/")
        code = _RENDER_TEMPLATE.format(
            path=json.dumps(path), width=int(width), height=int(height)
        )
        resp = self.execute(code)
        if resp.get("status") == "ok" and "BB_RENDER_OK" in str(resp.get("stdout", "")):
            return True, path
        reason = str(resp.get("message") or resp.get("stderr") or "render did not complete")
        return False, reason.strip() or "render did not complete"

    def save_blend(self, filepath: str) -> bool:
        """Save the current scene to ``filepath``. Returns ``True`` on success."""
        path = str(Path(filepath).resolve()).replace("\\", "/")
        code = (
            "import bpy\n"
            f"bpy.ops.wm.save_as_mainfile(filepath={json.dumps(path)})\n"
            "print('BB_SAVE_OK')\n"
        )
        resp = self.execute(code)
        return resp.get("status") == "ok" and "BB_SAVE_OK" in str(resp.get("stdout", ""))

    def scene_digest(self) -> str:
        """A short human-readable summary of the scene, or '' if unavailable."""
        code = (
            "import bpy\n"
            "lines=[]\n"
            "for o in bpy.context.scene.objects:\n"
            "    lines.append('%s (%s)' % (o.name, o.type))\n"
            "print('OBJECTS: ' + ', '.join(lines) if lines else 'OBJECTS: (none)')\n"
            "print('FRAME: %d' % bpy.context.scene.frame_current)\n"
        )
        try:
            resp = self.execute(code)
        except BlenderUnavailable:
            return ""
        if resp.get("status") == "ok":
            return str(resp.get("stdout", "")).strip()
        return ""


# bpy executed inside Blender. Frames a dedicated camera over the actual geometry
# and guarantees light/world so the fallback render is never a black frame. The
# user's active camera and film setting are restored afterwards.
_RENDER_TEMPLATE = """
import bpy, mathutils
from math import radians

scene = bpy.context.scene
geo = [o for o in scene.objects
       if o.type in {{'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}} and not o.hide_render]

_prev_cam = scene.camera
_prev_film = scene.render.film_transparent

# Always frame a dedicated camera, so an existing or mis-aimed camera cannot
# leave us rendering empty space.
try:
    cam = bpy.data.objects.get("BB_AutoCam")
    if cam is None or cam.type != 'CAMERA':
        cam = bpy.data.objects.new("BB_AutoCam", bpy.data.cameras.new("BB_AutoCam"))
        scene.collection.objects.link(cam)
    if geo:
        corners = []
        for o in geo:
            for c in o.bound_box:
                corners.append(o.matrix_world @ mathutils.Vector(c))
        mn = mathutils.Vector((min(c.x for c in corners),
                               min(c.y for c in corners),
                               min(c.z for c in corners)))
        mx = mathutils.Vector((max(c.x for c in corners),
                               max(c.y for c in corners),
                               max(c.z for c in corners)))
        center = (mn + mx) / 2.0
        radius = max((mx - mn).length, 1.0)
        dist = radius * 1.5
        loc = center + mathutils.Vector((dist, -dist, dist * 0.7))
        cam.location = loc
        cam.rotation_euler = (center - loc).to_track_quat('-Z', 'Y').to_euler()
    else:
        cam.location = (7.0, -7.0, 5.0)
        cam.rotation_euler = (radians(63), 0.0, radians(46))
    scene.camera = cam
except Exception as _frame_err:
    print("BB_FRAME_WARN", _frame_err)


def _contributes(o):
    return (o.type == 'LIGHT' and not o.hide_render
            and getattr(o.data, 'energy', 0.0) > 0.0)


if not any(_contributes(o) for o in scene.objects):
    light_data = bpy.data.lights.new("BB_AutoSun", 'SUN')
    light_data.energy = 3.0
    light = bpy.data.objects.new("BB_AutoSun", light_data)
    scene.collection.objects.link(light)
    light.rotation_euler = (radians(50), radians(15), radians(30))

if scene.world is None:
    world = bpy.data.worlds.new("BB_AutoWorld")
    scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is not None:
        bg.inputs[0].default_value = (0.05, 0.05, 0.06, 1.0)

r = scene.render
r.film_transparent = False
r.filepath = {path}
r.image_settings.file_format = 'PNG'
r.image_settings.color_mode = 'RGB'
r.resolution_x = {width}
r.resolution_y = {height}
r.resolution_percentage = 100
bpy.ops.render.render(write_still=True)

scene.camera = _prev_cam if _prev_cam is not None else scene.camera
scene.render.film_transparent = _prev_film
print("BB_RENDER_OK")
"""

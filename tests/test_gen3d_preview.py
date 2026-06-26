import json
import subprocess
import tempfile
import unittest
from unittest import mock

from blendahbot.discovery import DiscoveryError
from blendahbot.gen3d import preview
from blendahbot.gen3d.preview import (
    AssetStats,
    _parse_shot_arg,
    heuristic_floor,
    parse_preview_stats,
    render_isolated_preview,
)


def _manifest_stdout(**over) -> str:
    m = {
        "shots": ["/tmp/preview/shot_front.png", "/tmp/preview/shot_side.png"],
        "object_count": 2, "mesh_count": 1, "face_count": 1200,
        "has_materials": True, "has_uvs": True, "texture_count": 1,
        "max_texture_px": 2048, "longest_dim": 1.42, "bbox_aspect": 2.1, "engine": "BLENDER_EEVEE",
    }
    m.update(over)
    return "some blender startup noise\nBB_PREVIEW=" + json.dumps(m) + "\nBB_PREVIEW_OK\n"


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ParsePreviewStatsTests(unittest.TestCase):
    def test_clean_object(self):
        s = parse_preview_stats(_manifest_stdout())
        self.assertIsInstance(s, AssetStats)
        self.assertEqual(s.mesh_count, 1)
        self.assertEqual(s.face_count, 1200)
        self.assertTrue(s.has_materials)
        self.assertEqual(s.max_texture_px, 2048)
        self.assertAlmostEqual(s.bbox_aspect, 2.1)

    def test_many_objects_baked_scene(self):
        s = parse_preview_stats(_manifest_stdout(object_count=9, mesh_count=9))
        self.assertEqual(s.mesh_count, 9)

    def test_no_sentinel_returns_none(self):
        self.assertIsNone(parse_preview_stats("nothing here\njust logs\n"))

    def test_garbage_manifest_returns_none(self):
        self.assertIsNone(parse_preview_stats("BB_PREVIEW={not valid json\nBB_PREVIEW_OK\n"))


class HeuristicFloorTests(unittest.TestCase):
    def test_clean_textured_passes(self):
        ok, reasons = heuristic_floor(AssetStats(mesh_count=1, face_count=900, has_materials=True,
                                                 bbox_aspect=2.0))
        self.assertTrue(ok)
        self.assertEqual(reasons, [])

    def test_empty_geometry_rejected(self):
        ok, reasons = heuristic_floor(AssetStats(mesh_count=0, face_count=0))
        self.assertFalse(ok)
        self.assertTrue(any("no usable geometry" in r for r in reasons))

    def test_many_meshes_flagged_as_scene(self):
        ok, reasons = heuristic_floor(AssetStats(mesh_count=8, face_count=5000, has_materials=True,
                                                 bbox_aspect=3.0))
        self.assertFalse(ok)
        self.assertTrue(any("separate meshes" in r for r in reasons))

    def test_extreme_aspect_rejected(self):
        ok, reasons = heuristic_floor(AssetStats(mesh_count=1, face_count=10, has_materials=True,
                                                 bbox_aspect=40.0))
        self.assertFalse(ok)
        self.assertTrue(any("aspect" in r for r in reasons))

    def test_missing_material_when_texture_wanted(self):
        ok, _ = heuristic_floor(AssetStats(mesh_count=1, face_count=900, has_materials=False),
                                want_texture=True)
        self.assertFalse(ok)
        ok2, _ = heuristic_floor(AssetStats(mesh_count=1, face_count=900, has_materials=False),
                                 want_texture=False)
        self.assertTrue(ok2)

    def test_none_stats_rejected(self):
        ok, reasons = heuristic_floor(None)
        self.assertFalse(ok)
        self.assertTrue(reasons)


class PreviewCommandTests(unittest.TestCase):
    def test_command_shape(self):
        cmd = preview._preview_command("blender.exe", "scr.py", "C:/assets/barrel.glb", "C:/out",
                                       480, "BLENDER_EEVEE", 2.0, [["front", 0, 12]])
        self.assertIn("--background", cmd)
        self.assertIn("--factory-startup", cmd)  # don't load the live session's add-on/prefs
        self.assertIn("--python", cmd)
        self.assertIn("scr.py", cmd)
        joined = " ".join(cmd)
        self.assertIn("barrel.glb", joined)
        self.assertIn('"front"', joined)  # shots arrive as a JSON arg

    def test_script_is_isolated_and_sane(self):
        # Headless = fresh process, so no temp-scene/teardown dance and never a global purge.
        self.assertIn("import_scene.gltf", preview._PREVIEW_SCRIPT)
        self.assertIn("BB_PREVIEW_OK", preview._PREVIEW_SCRIPT)
        self.assertNotIn("orphans_purge", preview._PREVIEW_SCRIPT)


class RenderIsolatedPreviewTests(unittest.TestCase):
    def _run(self, proc=None, side_effect=None, find=("ok", "blender.exe")):
        find_kw = {"return_value": find[1]} if find[0] == "ok" else {"side_effect": find[1]}
        run_kw = {"side_effect": side_effect} if side_effect else {"return_value": proc}
        with mock.patch.object(preview, "find_blender_executable", **find_kw), \
             mock.patch.object(preview.subprocess, "run", **run_kw), \
             tempfile.TemporaryDirectory() as d:
            return render_isolated_preview("a.glb", d)

    def test_ok_path_parses_stats(self):
        result = self._run(proc=_Proc(stdout=_manifest_stdout()))
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.stats)
        self.assertEqual(result.stats.mesh_count, 1)
        self.assertEqual(result.engine, "BLENDER_EEVEE")

    def test_blender_not_found(self):
        result = self._run(find=("err", DiscoveryError("no blender installed")))
        self.assertFalse(result.ok)
        self.assertIn("no blender installed", result.detail)

    def test_import_error_reported(self):
        result = self._run(proc=_Proc(stdout="BB_PREVIEW_ERR glTF import produced no mesh", returncode=2))
        self.assertFalse(result.ok)
        self.assertIn("no mesh", result.detail)

    def test_timeout_tolerated(self):
        result = self._run(side_effect=subprocess.TimeoutExpired(cmd="blender", timeout=1))
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.detail)

    def test_launch_failure_tolerated(self):
        result = self._run(side_effect=OSError("exec format error"))
        self.assertFalse(result.ok)
        self.assertIn("could not launch", result.detail)


class ParseShotArgTests(unittest.TestCase):
    def test_none_and_empty(self):
        self.assertIsNone(_parse_shot_arg(None))
        self.assertIsNone(_parse_shot_arg(""))

    def test_catalog_labels(self):
        shots = _parse_shot_arg("front,side")
        self.assertEqual([s[0] for s in shots], ["front", "side"])

    def test_explicit_triples(self):
        shots = _parse_shot_arg("hero:35:18,low:20:5")
        self.assertEqual(shots[0], ["hero", 35.0, 18.0])
        self.assertEqual(shots[1], ["low", 20.0, 5.0])

    def test_unknown_label_skipped(self):
        self.assertIsNone(_parse_shot_arg("definitely_not_a_shot"))


if __name__ == "__main__":
    unittest.main()

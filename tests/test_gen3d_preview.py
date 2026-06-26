import json
import tempfile
import unittest
from pathlib import Path

from blendahbot.gen3d.preview import (
    AssetStats,
    _parse_shot_arg,
    build_preview_program,
    heuristic_floor,
    parse_preview_stats,
    render_isolated_preview,
)

from tests.test_blender_protocol import FakeBlender
from blendahbot.blender import BlenderClient


def _manifest_stdout(**over) -> str:
    m = {
        "shots": ["/tmp/preview/shot_front.png", "/tmp/preview/shot_side.png"],
        "object_count": 1, "mesh_count": 1, "face_count": 1200,
        "has_materials": True, "has_uvs": True, "texture_count": 1,
        "max_texture_px": 2048, "longest_dim": 1.42, "bbox_aspect": 2.1,
    }
    m.update(over)
    return "BB_PREVIEW=" + json.dumps(m) + "\nBB_PREVIEW_OK\n"


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
        ok, reasons = heuristic_floor(AssetStats(mesh_count=1, face_count=900, has_materials=False),
                                      want_texture=True)
        self.assertFalse(ok)
        ok2, _ = heuristic_floor(AssetStats(mesh_count=1, face_count=900, has_materials=False),
                                 want_texture=False)
        self.assertTrue(ok2)

    def test_none_stats_rejected(self):
        ok, reasons = heuristic_floor(None)
        self.assertFalse(ok)
        self.assertTrue(reasons)


class BuildPreviewProgramTests(unittest.TestCase):
    def test_embeds_glb_and_isolation_sentinels(self):
        code = build_preview_program("C:/assets/barrel.glb", "C:/out/preview", shots=[["front", 0, 12]])
        # the GLB path is embedded (forward-slashed)
        self.assertIn("barrel.glb", code)
        # isolation primitives
        self.assertIn("BB_Gen3DPreview", code)
        self.assertIn("temp_override", code)
        self.assertIn("scene=preview.name", code)  # renders the NON-active scene
        self.assertIn("BB_PREVIEW_OK", code)
        # the shot we passed reached the program header
        self.assertIn('"front"', code)

    def test_never_uses_orphans_purge(self):
        code = build_preview_program("a.glb", "out", shots=[["front", 0, 12]])
        self.assertNotIn("orphans_purge", code)


class RenderIsolatedPreviewTests(unittest.TestCase):
    def test_ok_path_parses_stats(self):
        fake = FakeBlender({"status": "ok", "stdout": _manifest_stdout()})
        try:
            with tempfile.TemporaryDirectory() as d:
                client = BlenderClient("localhost", fake.port, timeout=5)
                result = render_isolated_preview(client, "a.glb", d)
        finally:
            fake.close()
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.stats)
        self.assertEqual(result.stats.mesh_count, 1)
        # the executed code carried the render program
        self.assertIn("BB_Gen3DPreview", fake.requests[0]["code"])

    def test_error_status_returns_not_ok(self):
        fake = FakeBlender({"status": "error", "message": "import failed: bad glb"})
        try:
            with tempfile.TemporaryDirectory() as d:
                client = BlenderClient("localhost", fake.port, timeout=5)
                result = render_isolated_preview(client, "a.glb", d)
        finally:
            fake.close()
        self.assertFalse(result.ok)
        self.assertIn("bad glb", result.detail)

    def test_dropped_connection_tolerated(self):
        # Port 1 is never open -> transport error -> ok=False, no raise.
        with tempfile.TemporaryDirectory() as d:
            client = BlenderClient("localhost", 1, timeout=2)
            result = render_isolated_preview(client, "a.glb", d)
        self.assertFalse(result.ok)
        self.assertTrue(result.detail)


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

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blendahbot.blender import BlenderUnavailable
from blendahbot.loop import _gather_images, _save_checkpoint


class _Console:
    def info(self, *_a, **_k):
        pass

    warn = success = error = info


class _State:
    render_path = None


class GatherImagesFallbackTests(unittest.TestCase):
    def test_no_fallback_render_when_blender_dead(self):
        # allow_fallback=False must never touch Blender (which would block on a dead
        # socket); with no on-disk render it returns nothing.
        with tempfile.TemporaryDirectory() as d:
            round_dir = Path(d)
            blender = mock.Mock()
            images = _gather_images(
                blender, _State(), round_dir / "render.png", round_dir, _Console(),
                allow_fallback=False,
            )
        self.assertEqual(images, [])
        blender.render_still.assert_not_called()

    def test_uses_on_disk_render_without_calling_blender(self):
        with tempfile.TemporaryDirectory() as d:
            round_dir = Path(d)
            render = round_dir / "render.png"
            render.write_bytes(b"\x89PNG\r\n")
            blender = mock.Mock()
            images = _gather_images(
                blender, _State(), render, round_dir, _Console(), allow_fallback=False,
            )
        self.assertIn(render.resolve(), [p.resolve() for p in images])
        blender.render_still.assert_not_called()

    def test_fallback_render_used_when_allowed_and_no_image(self):
        with tempfile.TemporaryDirectory() as d:
            round_dir = Path(d)
            blender = mock.Mock()

            def fake_render(path, **_kw):
                Path(path).write_bytes(b"\x89PNG\r\n")
                return True, path

            blender.render_still.side_effect = fake_render
            images = _gather_images(
                blender, _State(), round_dir / "render.png", round_dir, _Console(),
                allow_fallback=True,
            )
        self.assertTrue(images)
        blender.render_still.assert_called_once()


class SaveCheckpointTests(unittest.TestCase):
    def test_swallows_unavailable(self):
        blender = mock.Mock()
        blender.save_blend.side_effect = BlenderUnavailable("down")
        self.assertFalse(_save_checkpoint(blender, Path("x.blend")))

    def test_returns_save_result(self):
        blender = mock.Mock()
        blender.save_blend.return_value = True
        self.assertTrue(_save_checkpoint(blender, Path("x.blend")))


if __name__ == "__main__":
    unittest.main()

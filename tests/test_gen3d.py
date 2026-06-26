import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blendahbot.gen3d import GenRequest, GenResult, generate_candidates, get_backend
from blendahbot.gen3d.base import Gen3DBackend, Gen3DError
from blendahbot.gen3d.compare import candidate_path
from blendahbot.gen3d.hunyuan3d import (
    Hunyuan3DReplicateBackend,
    _build_input,
    _extract_glb_url,
)
from blendahbot.gen3d.trellis_local import TrellisLocalBackend


class BackendSelectionTests(unittest.TestCase):
    def test_hunyuan3_is_registered(self):
        be = get_backend("hunyuan3")
        self.assertIsInstance(be, Hunyuan3DReplicateBackend)
        self.assertEqual(be.name, "hunyuan3")

    def test_trellis_is_registered(self):
        be = get_backend("trellis")
        self.assertIsInstance(be, TrellisLocalBackend)
        self.assertEqual(be.name, "trellis")

    def test_unknown_backend_lists_choices(self):
        with self.assertRaises(Gen3DError) as ctx:
            get_backend("nope")
        msg = str(ctx.exception)
        self.assertIn("hunyuan3", msg)
        self.assertIn("trellis", msg)


class TrellisAvailabilityTests(unittest.TestCase):
    def test_unreachable_server_reports_clearly(self):
        be = TrellisLocalBackend()
        # Point at a port nothing is listening on so the health probe fails fast.
        with mock.patch.dict(os.environ, {"BLENDAHBOT_TRELLIS_URL": "http://localhost:1"}, clear=False):
            ok, reason = be.available(GenRequest(image_path="x.png"))
        self.assertFalse(ok)
        self.assertIn("TRELLIS.2 server not reachable", reason)


# --- a fake backend so candidate generation can be tested without servers ---
class _FakeBackend(Gen3DBackend):
    def __init__(self, name, *, ok=True, fail=False):
        self.name = name
        self._ok = ok
        self._fail = fail

    def available(self, req):
        return (self._ok, "ok" if self._ok else "unavailable")

    def generate(self, req, out, on_progress=None, timeout=600.0):
        if self._fail:
            raise Gen3DError(f"{self.name} boom")
        Path(out).write_bytes(b"glb-" + self.name.encode())
        return GenResult(path=Path(out), backend=self.name)


class CandidatePathTests(unittest.TestCase):
    def test_suffixing(self):
        self.assertEqual(candidate_path("a/asset.glb", "trellis"), Path("a/asset.trellis.glb"))


class GenerateCandidatesTests(unittest.TestCase):
    def _patch(self, mapping):
        return mock.patch("blendahbot.gen3d.base._backends", return_value=mapping)

    def test_runs_each_available_backend(self):
        fakes = {"a": _FakeBackend("a"), "b": _FakeBackend("b")}
        with tempfile.TemporaryDirectory() as d, self._patch(fakes):
            out = Path(d) / "asset.glb"
            results = generate_candidates(prompt="x", out=out, backends=["a", "b"])
            self.assertEqual({r.backend for r in results}, {"a", "b"})
            self.assertTrue((Path(d) / "asset.a.glb").exists())
            self.assertTrue((Path(d) / "asset.b.glb").exists())

    def test_skips_unavailable_backend(self):
        fakes = {"a": _FakeBackend("a"), "b": _FakeBackend("b", ok=False)}
        with tempfile.TemporaryDirectory() as d, self._patch(fakes):
            results = generate_candidates(prompt="x", out=Path(d) / "asset.glb", backends=["a", "b"])
        self.assertEqual([r.backend for r in results], ["a"])

    def test_failing_backend_dropped_not_fatal(self):
        fakes = {"a": _FakeBackend("a", fail=True), "b": _FakeBackend("b")}
        with tempfile.TemporaryDirectory() as d, self._patch(fakes):
            results = generate_candidates(prompt="x", out=Path(d) / "asset.glb", backends=["a", "b"])
        self.assertEqual([r.backend for r in results], ["b"])

    def test_all_failing_raises(self):
        fakes = {"a": _FakeBackend("a", ok=False), "b": _FakeBackend("b", ok=False)}
        with tempfile.TemporaryDirectory() as d, self._patch(fakes):
            with self.assertRaises(Gen3DError):
                generate_candidates(prompt="x", out=Path(d) / "asset.glb", backends=["a", "b"])

    def test_dedups_backend_names(self):
        fakes = {"a": _FakeBackend("a")}
        with tempfile.TemporaryDirectory() as d, self._patch(fakes):
            results = generate_candidates(prompt="x", out=Path(d) / "asset.glb", backends=["a", "a"])
        self.assertEqual([r.backend for r in results], ["a"])


class AvailabilityTests(unittest.TestCase):
    def test_unavailable_without_token(self):
        be = Hunyuan3DReplicateBackend()
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, reason = be.available(GenRequest(prompt="a barrel"))
        self.assertFalse(ok)
        self.assertIn("REPLICATE_API_TOKEN", reason)

    def test_available_with_token_and_prompt(self):
        be = Hunyuan3DReplicateBackend()
        with mock.patch.dict(os.environ, {"REPLICATE_API_TOKEN": "r8_x"}, clear=True):
            ok, _ = be.available(GenRequest(prompt="a barrel"))
            self.assertTrue(ok)
            # token present but neither prompt nor image -> not usable
            ok2, _ = be.available(GenRequest())
            self.assertFalse(ok2)


class BuildInputTests(unittest.TestCase):
    def test_prompt_maps_to_canonical_key(self):
        inp = _build_input(GenRequest(prompt="oak barrel"), {"prompt", "seed", "texture"})
        self.assertEqual(inp["prompt"], "oak barrel")

    def test_prompt_falls_back_to_alternate_field_name(self):
        # Model that calls the prompt "caption" instead of "prompt".
        inp = _build_input(GenRequest(prompt="oak barrel"), {"caption", "image"})
        self.assertEqual(inp["caption"], "oak barrel")
        self.assertNotIn("prompt", inp)

    def test_texture_toggle_and_face_count(self):
        req = GenRequest(prompt="x", texture=False, face_count=20000, seed=7)
        inp = _build_input(req, {"prompt", "texture", "face_count", "seed"})
        self.assertEqual(inp["texture"], False)
        self.assertEqual(inp["face_count"], 20000)
        self.assertEqual(inp["seed"], 7)

    def test_face_count_uses_alternate_key(self):
        inp = _build_input(GenRequest(prompt="x", face_count=5000), {"prompt", "face_limit"})
        self.assertEqual(inp["face_limit"], 5000)

    def test_image_inlined_as_data_uri(self):
        # Point at a real file (this test file) so the .exists() branch runs.
        here = Path(__file__)
        inp = _build_input(GenRequest(image_path=str(here)), {"image"})
        self.assertTrue(str(inp["image"]).startswith("data:image/"))
        self.assertIn(";base64,", str(inp["image"]))

    def test_no_usable_field_raises(self):
        with self.assertRaises(Gen3DError):
            _build_input(GenRequest(), {"prompt"})


class ExtractGlbUrlTests(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(_extract_glb_url("https://x/out.glb"), "https://x/out.glb")

    def test_list_prefers_glb(self):
        out = ["https://x/preview.png", "https://x/model.glb"]
        self.assertEqual(_extract_glb_url(out), "https://x/model.glb")

    def test_dict_named_field(self):
        out = {"mesh": "https://x/model.glb", "texture": "https://x/t.png"}
        self.assertEqual(_extract_glb_url(out), "https://x/model.glb")

    def test_dict_nested(self):
        out = {"result": {"pbr_model": "https://x/m.glb"}}
        self.assertEqual(_extract_glb_url(out), "https://x/m.glb")

    def test_glb_with_query_string(self):
        out = ["https://x/model.glb?token=abc"]
        self.assertEqual(_extract_glb_url(out), "https://x/model.glb?token=abc")

    def test_none_when_empty(self):
        self.assertIsNone(_extract_glb_url({}))
        self.assertIsNone(_extract_glb_url([]))


if __name__ == "__main__":
    unittest.main()

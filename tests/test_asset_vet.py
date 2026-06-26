import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from blendahbot.builder import Verdict
from blendahbot.gen3d import vet as vetmod
from blendahbot.gen3d.base import GenResult
from blendahbot.gen3d.preview import AssetStats, PreviewResult


class TweakPromptTests(unittest.TestCase):
    def test_reseed_keeps_prompt(self):
        out = vetmod.tweak_prompt("weathered oak barrel, iron hoops", ["reseed"])
        self.assertEqual(out, "weathered oak barrel, iron hoops")

    def test_empty_suggestions_keeps_prompt(self):
        self.assertEqual(vetmod.tweak_prompt("brass lantern", []), "brass lantern")

    def test_adds_single_object_tightener(self):
        out = vetmod.tweak_prompt("a cozy kitchen with a table", ["one object only, plain background"])
        self.assertIn("single object", out)

    def test_truncates_to_limit(self):
        long = "a very long prompt that clearly exceeds the sixty character truncation limit by a lot"
        out = vetmod.tweak_prompt(long, ["reseed"], limit=60)
        self.assertLessEqual(len(out), 60)


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.calls = 0
        self.seeds: list = []

    def available(self, req):
        return True, "ok"

    def generate(self, req, out, on_progress=None, timeout=600.0):
        self.calls += 1
        self.seeds.append(req.seed)
        p = Path(out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"glb-bytes")
        return GenResult(path=p, backend=self.name)


_GOOD_STATS = AssetStats(mesh_count=1, face_count=1500, has_materials=True, bbox_aspect=2.0)


def _good_preview(*_a, **_k):
    return PreviewResult(glb_path=Path("x.glb"), sheet_paths=[], stats=_GOOD_STATS, ok=True)


def _run(coro):
    return asyncio.run(coro)


class VetAndGenerateTests(unittest.TestCase):
    def _patches(self, backend, verdicts):
        """Patch the seams vet_and_generate calls; verdicts is a list returned per attempt."""
        calls = {"n": 0}

        async def fake_critic(*_a, **_k):
            v = verdicts[min(calls["n"], len(verdicts) - 1)]
            calls["n"] += 1
            return v

        return (
            mock.patch.object(vetmod, "get_backend", return_value=backend),
            mock.patch.object(vetmod, "render_isolated_preview", _good_preview),
            mock.patch.object(vetmod, "heuristic_floor", lambda *a, **k: (True, [])),
            mock.patch.object(vetmod, "run_asset_critic", fake_critic),
        )

    def test_accepts_first_passing_asset(self):
        be = FakeBackend()
        good = Verdict(satisfied=True, score=80, summary="clean barrel")
        ps = self._patches(be, [good])
        with ps[0], ps[1], ps[2], ps[3], tempfile.TemporaryDirectory() as d:
            out = Path(d) / "barrel.glb"
            vr = _run(vetmod.vet_and_generate(
                "weathered oak barrel", out, config=None, work_dir=Path(d) / "wk",
                max_attempts=3, accept_threshold=55,
            ))
            out_exists = out.exists()
        self.assertTrue(vr.vetted)
        self.assertTrue(vr.result.vetted)
        self.assertEqual(be.calls, 1)  # stopped at first accept
        self.assertTrue(out_exists)

    def test_exhausts_and_returns_best_unvetted(self):
        be = FakeBackend()
        bad = Verdict(satisfied=False, score=30, summary="holed blob", issues=["holes in the back"],
                      suggestions=["reseed"])
        ps = self._patches(be, [bad, bad])
        with ps[0], ps[1], ps[2], ps[3], tempfile.TemporaryDirectory() as d:
            out = Path(d) / "barrel.glb"
            vr = _run(vetmod.vet_and_generate(
                "weathered oak barrel", out, config=None, work_dir=Path(d) / "wk",
                max_attempts=2, accept_threshold=55,
            ))
            out_exists = out.exists()
        self.assertFalse(vr.vetted)
        self.assertFalse(vr.result.vetted)
        self.assertEqual(be.calls, 2)
        self.assertEqual(be.seeds, [1, 2])  # deterministic per-attempt seed bump
        self.assertTrue(vr.result.issues)  # unresolved defects surfaced
        self.assertTrue(out_exists)

    def test_keeps_best_scoring_attempt(self):
        be = FakeBackend()
        v1 = Verdict(satisfied=False, score=20, summary="bad", suggestions=["reseed"])
        v2 = Verdict(satisfied=False, score=50, summary="closer", suggestions=["reseed"])
        ps = self._patches(be, [v1, v2])
        with ps[0], ps[1], ps[2], ps[3], tempfile.TemporaryDirectory() as d:
            out = Path(d) / "barrel.glb"
            vr = _run(vetmod.vet_and_generate(
                "barrel", out, config=None, work_dir=Path(d) / "wk",
                max_attempts=2, accept_threshold=55,
            ))
        self.assertFalse(vr.vetted)
        self.assertEqual(vr.verdict.score, 50)  # the better of the two attempts

    def test_preview_failure_is_handled(self):
        be = FakeBackend()
        good = Verdict(satisfied=True, score=80, summary="ok")

        def failed_preview(*_a, **_k):
            return PreviewResult(glb_path=Path("x.glb"), ok=False, detail="blender unreachable")

        with mock.patch.object(vetmod, "get_backend", return_value=be), \
             mock.patch.object(vetmod, "render_isolated_preview", failed_preview), \
             mock.patch.object(vetmod, "run_asset_critic", mock.AsyncMock(return_value=good)), \
             tempfile.TemporaryDirectory() as d:
            out = Path(d) / "barrel.glb"
            vr = _run(vetmod.vet_and_generate(
                "barrel", out, config=None, work_dir=Path(d) / "wk",
                max_attempts=1, accept_threshold=55,
            ))
            out_exists = out.exists()
        # preview failed -> not vetted, but still returns a produced asset (no crash)
        self.assertFalse(vr.vetted)
        self.assertTrue(out_exists)


if __name__ == "__main__":
    unittest.main()

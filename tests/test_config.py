import os
import unittest
from pathlib import Path

from blendahbot.config import BotConfig, slugify


class SlugifyTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("A Cozy Cabin!"), "a-cozy-cabin")

    def test_empty_falls_back(self):
        self.assertEqual(slugify("!!!"), "creation")

    def test_truncates(self):
        self.assertLessEqual(len(slugify("x" * 100)), 40)


class BotConfigTests(unittest.TestCase):
    def test_run_dir_contains_slug(self):
        cfg = BotConfig(request="make a red cube")
        self.assertIn("make-a-red-cube", cfg.run_dir.name)
        self.assertEqual(cfg.run_dir.parent, Path("runs").resolve())

    def test_run_dir_is_absolute(self):
        # Paths are handed to Blender (a separate process), so they must be absolute.
        cfg = BotConfig(request="x")
        self.assertTrue(cfg.run_dir.is_absolute())
        self.assertTrue(cfg.round_dir(1).is_absolute())

    def test_round_and_final_dirs(self):
        cfg = BotConfig(request="x")
        self.assertEqual(cfg.round_dir(3).name, "round_03")
        self.assertEqual(cfg.final_dir.name, "final")

    def test_from_env_ignores_none_overrides(self):
        cfg = BotConfig.from_env("thing", max_rounds=None, budget_usd=2.5)
        self.assertEqual(cfg.max_rounds, 6)
        self.assertEqual(cfg.budget_usd, 2.5)

    def test_from_env_reads_environment(self):
        os.environ["BLENDER_MCP_PORT"] = "9999"
        try:
            cfg = BotConfig.from_env("thing")
            self.assertEqual(cfg.blender_port, 9999)
        finally:
            del os.environ["BLENDER_MCP_PORT"]


if __name__ == "__main__":
    unittest.main()

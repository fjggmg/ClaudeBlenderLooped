import os
import shutil
import tempfile
import unittest

from blendahbot import settings


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._prev_home = os.environ.get("BLENDAHBOT_HOME")
        self._prev_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        self._tmp = tempfile.mkdtemp()
        os.environ["BLENDAHBOT_HOME"] = self._tmp

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("BLENDAHBOT_HOME", None)
        else:
            os.environ["BLENDAHBOT_HOME"] = self._prev_home
        if self._prev_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._prev_key
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_save_load_roundtrip(self):
        settings.save_settings({"budget_usd": 5.0, "score_threshold": 90})
        d = settings.load_settings()
        self.assertEqual(d["budget_usd"], 5.0)
        self.assertEqual(d["score_threshold"], 90)

    def test_load_missing_is_empty(self):
        self.assertEqual(settings.load_settings(), {})

    def test_config_overrides_skips_none_and_unset(self):
        d = {"budget_usd": 3.0, "max_rounds": None, "use_critic": False, "model": None}
        self.assertEqual(
            settings.config_overrides(d), {"budget_usd": 3.0, "use_critic": False}
        )

    def test_config_overrides_ignores_api_key(self):
        # The API key is auth, not a BotConfig field.
        self.assertNotIn("anthropic_api_key", settings.config_overrides({"anthropic_api_key": "x"}))

    def test_apply_to_env_sets_api_key(self):
        settings.apply_to_env({"anthropic_api_key": "sk-test-123"})
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "sk-test-123")

    def test_apply_to_env_no_key_leaves_unset(self):
        settings.apply_to_env({})
        self.assertIsNone(os.environ.get("ANTHROPIC_API_KEY"))


if __name__ == "__main__":
    unittest.main()

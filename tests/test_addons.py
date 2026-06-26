import os
import unittest
from pathlib import Path
from unittest import mock

from blendahbot import addons


def _entry(eid, name="", tagline="", tags=None, bmin="4.2.0", bmax=None, type="add-on", url=None):
    e = {
        "id": eid, "name": name or eid, "tagline": tagline, "tags": tags or [],
        "blender_version_min": bmin, "type": type, "version": "1.0.0",
        "archive_url": url or f"https://extensions.blender.org/download/{eid}.zip",
    }
    if bmax:
        e["blender_version_max"] = bmax
    return e


class VersionTests(unittest.TestCase):
    def test_ver_parses_and_pads(self):
        self.assertEqual(addons._ver("5.1.2"), (5, 1, 2))
        self.assertEqual(addons._ver("4.2"), (4, 2, 0))
        self.assertEqual(addons._ver(""), (0, 0, 0))
        self.assertEqual(addons._ver(None), (0, 0, 0))

    def test_compatible_min_bound(self):
        self.assertTrue(addons._compatible(_entry("a", bmin="4.2.0"), (5, 1, 0)))
        self.assertFalse(addons._compatible(_entry("a", bmin="6.0.0"), (5, 1, 0)))

    def test_compatible_max_bound_exclusive(self):
        self.assertFalse(addons._compatible(_entry("a", bmin="4.2.0", bmax="5.0.0"), (5, 1, 0)))
        self.assertTrue(addons._compatible(_entry("a", bmin="4.2.0", bmax="6.0.0"), (5, 1, 0)))


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.index = [
            _entry("modular_tree", "Modular Tree", "node based 3D tree generation", ["Mesh"]),
            _entry("modular_tree", "Modular Tree", "node based 3D tree generation", ["Mesh"]),  # dup
            _entry("easy_tree", "Easy Tree", "one-click procedural trees"),
            _entry("node_wrangler", "Node Wrangler", "shader node tools"),
            _entry("old_tree", "Old Tree", "tree maker", bmin="6.0.0"),  # incompatible
        ]

    def test_exact_id_ranks_first(self):
        hits = addons.search("easy_tree", self.index, n=5)
        self.assertEqual(hits[0]["id"], "easy_tree")

    def test_dedup_by_id(self):
        ids = [e["id"] for e in addons.search("tree", self.index, n=10)]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("modular_tree", ids)

    def test_kind_filter(self):
        idx = self.index + [_entry("dark_theme", "Dark", "a theme", type="theme")]
        self.assertEqual(addons.search("theme", idx, kind="add-on"), [])
        self.assertTrue(addons.search("theme", idx, kind="theme"))

    def test_incompatible_deranked_not_dropped(self):
        hits = addons.search("tree", self.index, n=10, blender_version=(5, 1, 0))
        ids = [e["id"] for e in hits]
        self.assertIn("old_tree", ids)  # kept
        self.assertLess(ids.index("easy_tree"), ids.index("old_tree"))  # but below compatible ones

    def test_no_match_returns_empty(self):
        self.assertEqual(addons.search("zzz_nonsense", self.index), [])


class ResolveTests(unittest.TestCase):
    def test_exact_id_case_insensitive(self):
        idx = [_entry("Foo_Bar"), _entry("baz")]
        self.assertEqual(addons.resolve("foo_bar", idx)["id"], "Foo_Bar")

    def test_falls_back_to_search(self):
        idx = [_entry("scatter_pro", "Scatter Pro", "scatter objects on surfaces")]
        self.assertEqual(addons.resolve("scatter objects", idx)["id"], "scatter_pro")

    def test_returns_none_when_nothing(self):
        self.assertIsNone(addons.resolve("nope", [_entry("foo")]))


class HelperTests(unittest.TestCase):
    def test_archive_name_from_url(self):
        self.assertEqual(
            addons._archive_name("https://x/download/add-on-foo-v1.2.zip"), "add-on-foo-v1.2.zip"
        )

    def test_archive_name_fallback(self):
        self.assertEqual(
            addons._archive_name("https://x/download/", _entry("foo")), "foo-1.0.0.zip"
        )

    def test_import_name(self):
        self.assertEqual(addons._import_name("opencv-python"), "cv2")  # known alias
        self.assertEqual(addons._import_name("trimesh>=4.0"), "trimesh")
        self.assertEqual(addons._import_name("shapely[all]"), "shapely")


class CodeBuilderTests(unittest.TestCase):
    def test_install_files_code_has_operator_and_redirect(self):
        code = addons._install_files_code(Path("C:/tmp/x.zip"), "user_default")
        self.assertIn("package_install_files", code)
        self.assertIn("user_default", code)
        self.assertIn("x.zip", code)
        self.assertIn("sys.stdout = sys.stderr = buf", code)  # stdout-None hardening

    def test_enable_code(self):
        self.assertIn("addon_enable", addons._enable_code("bl_ext.x.y", True))
        self.assertIn("addon_disable", addons._enable_code("bl_ext.x.y", False))

    def test_asset_lib_add_code_quotes_path(self):
        code = addons._asset_lib_add_code("C:/packs/kit", "Kit")
        self.assertIn("asset_library_add", code)
        self.assertIn('"Kit"', code)


class FakeClient:
    """Stand-in for BlenderClient: returns canned ``result`` dicts in order."""

    def __init__(self, results):
        self.results = list(results)
        self.codes: list[str] = []

    def execute(self, code, strict_json=False):
        self.codes.append(code)
        return {"status": "ok", "result": self.results.pop(0)}


class InstallFlowTests(unittest.TestCase):
    def test_install_downloads_then_enables(self):
        idx = [_entry("foo")]
        client = FakeClient([
            {"extensions": [], "legacy": []},  # pre-check: not installed
            {"error": None, "new_modules": ["bl_ext.user_default.foo"],
             "enabled_ext": ["bl_ext.user_default.foo"]},
        ])
        with mock.patch.object(addons, "_download", return_value=Path("C:/tmp/foo.zip")) as dl:
            info = addons.install_extension("foo", index=idx, client=client)
        dl.assert_called_once()
        self.assertEqual(info["modules"], ["bl_ext.user_default.foo"])

    def test_install_short_circuits_when_already_enabled(self):
        idx = [_entry("foo")]
        client = FakeClient([{"extensions": ["bl_ext.blender_org.foo"], "legacy": []}])
        with mock.patch.object(addons, "_download", side_effect=AssertionError("must not download")) as dl:
            info = addons.install_extension("foo", index=idx, client=client)
        dl.assert_not_called()
        self.assertEqual(info["modules"], [])
        self.assertEqual(info["already"], ["bl_ext.blender_org.foo"])

    def test_install_unknown_id_raises(self):
        with self.assertRaises(addons.AddonError):
            addons.install_extension("does_not_exist", index=[_entry("foo")], client=FakeClient([]))

    def test_install_falls_back_to_online_when_file_enables_nothing(self):
        idx = [_entry("foo")]
        client = FakeClient([
            {"extensions": [], "legacy": []},                       # pre-check
            {"error": "bad zip", "new_modules": [], "enabled_ext": []},  # file install: nothing
            {"error": None, "new_modules": ["bl_ext.blender_org.foo"]},  # online by id
        ])
        with mock.patch.object(addons, "_download", return_value=Path("C:/tmp/foo.zip")):
            info = addons.install_extension("foo", index=idx, client=client)
        self.assertEqual(info["modules"], ["bl_ext.blender_org.foo"])


class ExecTests(unittest.TestCase):
    def test_exec_raises_on_blender_error(self):
        class C:
            def execute(self, code, strict_json=False):
                return {"status": "error", "message": "boom"}
        with self.assertRaises(addons.AddonError):
            addons._exec(C(), "result = {}")

    def test_exec_parses_json_string_result(self):
        class C:
            def execute(self, code, strict_json=False):
                return {"status": "ok", "result": '{"a": 1}'}
        self.assertEqual(addons._exec(C(), "x"), {"a": 1})


class DownloadGuardTests(unittest.TestCase):
    def test_download_rejects_non_http_scheme(self):
        with self.assertRaises(addons.AddonError):
            addons._download("file:///etc/passwd", Path("x.zip"))

    def test_search_tolerates_non_string_tags(self):
        idx = [_entry("foo", "Foo", "bar", tags=[1, "Mesh", None])]
        self.assertEqual(addons.search("foo", idx)[0]["id"], "foo")  # no crash on int/None tags


class InstallGuardTests(unittest.TestCase):
    def test_install_rejects_off_platform_archive_host(self):
        idx = [_entry("foo", url="https://evil.example.com/foo.zip")]
        client = FakeClient([{"extensions": [], "legacy": []}])  # pre-check: not installed
        with mock.patch.object(addons, "_download", side_effect=AssertionError("must not download")):
            with self.assertRaises(addons.AddonError):
                addons.install_extension("foo", index=idx, client=client)

    def test_install_raises_when_no_archive_url(self):
        idx = [_entry("foo")]
        idx[0].pop("archive_url")
        client = FakeClient([{"extensions": [], "legacy": []}])
        with self.assertRaises(addons.AddonError):
            addons.install_extension("foo", index=idx, client=client)


class InstallUrlTests(unittest.TestCase):
    def test_extension_then_legacy_fallback(self):
        client = FakeClient([
            {"error": "not an extension", "new_modules": [], "enabled_ext": []},  # extension install
            {"error": None, "new_modules": ["my_addon"]},                          # legacy fallback
        ])
        with mock.patch.object(addons, "_download", return_value=Path("C:/tmp/a.zip")):
            info = addons.install_url("https://host/a.zip", client=client, legacy=None)
        self.assertEqual(info["modules"], ["my_addon"])
        self.assertEqual(len(client.codes), 2)


class OnlineCodeTests(unittest.TestCase):
    def test_online_code_shape(self):
        code = addons._install_online_code("foo", "blender_org")
        self.assertIn("package_install(", code)
        self.assertIn("repo_index=idx", code)
        self.assertIn("r.module ==", code)
        self.assertIn("idx < 0", code)            # guards a missing remote repo


class PipTests(unittest.TestCase):
    def test_pip_uses_end_of_options_and_blender_python(self):
        client = FakeClient([
            {"exe": "/blender/python", "version": "3.13"},  # _PY_EXE_CODE
            {"importable": {"trimesh": True}},               # import check
        ])
        fake_proc = type("P", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
        with mock.patch.object(addons.subprocess, "run", return_value=fake_proc) as run:
            res = addons.pip_install(["trimesh", "-evil"], client=client)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "/blender/python")
        self.assertIn("--", cmd)
        self.assertLess(cmd.index("--"), cmd.index("trimesh"))   # packages are after `--`
        self.assertEqual(res["returncode"], 0)

    def test_pip_timeout_becomes_addonerror(self):
        import subprocess as sp
        client = FakeClient([{"exe": "/blender/python"}])
        with mock.patch.object(addons.subprocess, "run", side_effect=sp.TimeoutExpired("pip", 1)):
            with self.assertRaises(addons.AddonError):
                addons.pip_install(["trimesh"], client=client)

    def test_import_name_aliases(self):
        self.assertEqual(addons._import_name("opencv-python"), "cv2")
        self.assertEqual(addons._import_name("Pillow"), "PIL")
        self.assertEqual(addons._import_name("some-pkg"), "some_pkg")


class ResultErrorTests(unittest.TestCase):
    def test_flat_error(self):
        self.assertEqual(addons._result_error({"error": "boom"}), "boom")

    def test_nested_errors_joined(self):
        msg = addons._result_error({"file": {"error": "bad zip"}, "online": {"error": "404"}})
        self.assertIn("bad zip", msg)
        self.assertIn("404", msg)


class WiringTests(unittest.TestCase):
    """Pin the cross-file flag wiring (config / settings / prompt)."""

    def setUp(self):
        self._prev = os.environ.pop("BLENDAHBOT_NO_ADDONS", None)

    def tearDown(self):
        os.environ.pop("BLENDAHBOT_NO_ADDONS", None)
        if self._prev is not None:
            os.environ["BLENDAHBOT_NO_ADDONS"] = self._prev

    def test_config_default_on(self):
        from blendahbot.config import BotConfig
        self.assertIs(BotConfig.from_env("x").addons, True)

    def test_config_env_disables(self):
        from blendahbot.config import BotConfig
        os.environ["BLENDAHBOT_NO_ADDONS"] = "1"
        self.assertIs(BotConfig.from_env("x").addons, False)

    def test_settings_override_includes_addons(self):
        from blendahbot import settings
        self.assertEqual(settings.config_overrides({"addons": False}), {"addons": False})
        self.assertNotIn("addons", settings.config_overrides({}))

    def test_prompt_grants_capability_when_allowed(self):
        from blendahbot.prompts import builder_system_prompt
        on = builder_system_prompt("r", "s", allow_addons=True)
        self.assertIn("blendahbot.addons", on)
        self.assertIn("SAFETY", on)
        self.assertIn("install-url", on)

    def test_prompt_forbids_when_disabled(self):
        from blendahbot.prompts import builder_system_prompt
        off = builder_system_prompt("r", "s", allow_addons=False)
        self.assertIn("DISABLED", off)
        self.assertIn("blendahbot.assets", off)  # asset fetches stay allowed
        self.assertNotIn("are live immediately", off)  # no install-encouragement block


if __name__ == "__main__":
    unittest.main()

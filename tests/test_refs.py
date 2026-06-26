import tempfile
import unittest
from pathlib import Path

from blendahbot.refs import (
    _is_usable,
    clean_query,
    ingest_user_references,
    parse_path_tokens,
    resolve_reference_specs,
)


class CleanQueryTests(unittest.TestCase):
    def test_strips_style_and_stopwords(self):
        self.assertEqual(
            clean_query("a cozy low-poly wooden cabin in a pine forest at dusk"),
            "wooden cabin pine forest dusk",
        )

    def test_drops_render_tokens(self):
        self.assertEqual(clean_query("realistic 3d render of a teapot"), "realistic teapot")

    def test_caps_word_count(self):
        q = clean_query("alpha beta gamma delta epsilon zeta eta", max_words=5)
        self.assertEqual(len(q.split()), 5)

    def test_fallback_when_everything_stripped(self):
        self.assertTrue(clean_query("the a an of"))  # non-empty fallback


class IsUsableTests(unittest.TestCase):
    def test_rejects_non_photo_extensions(self):
        self.assertFalse(_is_usable("https://x/commons/thumb/Foo.pdf/p1.jpg"))
        self.assertFalse(_is_usable("https://x/Logo.svg"))
        self.assertFalse(_is_usable("https://x/Book.djvu/page.jpg"))

    def test_rejects_junk_titles(self):
        self.assertFalse(_is_usable("https://x/A_Boys_Battle_cover.jpg"))
        self.assertFalse(_is_usable("https://x/Map_of_region.jpg"))
        self.assertFalse(_is_usable("https://x/Company_logo.png"))

    def test_accepts_real_photo(self):
        self.assertTrue(_is_usable("https://upload.wikimedia.org/.../Porsche_911_Carrera.jpg"))
        self.assertTrue(_is_usable("https://live.staticflickr.com/123/456_b.jpg"))


class ParsePathTokensTests(unittest.TestCase):
    def test_splits_unquoted_paths(self):
        self.assertEqual(
            parse_path_tokens("C:\\a.png D:\\b.jpg"),
            ["C:\\a.png", "D:\\b.jpg"],
        )

    def test_keeps_quoted_path_with_spaces_intact(self):
        self.assertEqual(
            parse_path_tokens('"C:\\my pics\\car.png"'),
            ["C:\\my pics\\car.png"],
        )

    def test_mixes_quoted_and_unquoted(self):
        self.assertEqual(
            parse_path_tokens('"a b.png" c.jpg'),
            ["a b.png", "c.jpg"],
        )

    def test_strips_file_uri_scheme(self):
        self.assertEqual(
            parse_path_tokens("file:///C:/pics/a%20b.png"),
            ["C:/pics/a b.png"],
        )

    def test_blank_line_yields_nothing(self):
        self.assertEqual(parse_path_tokens("   "), [])


class ResolveAndIngestTests(unittest.TestCase):
    def _img(self, root: Path, name: str) -> Path:
        p = root / name
        p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return p

    def test_resolves_files_and_dirs_skips_junk(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            img = self._img(root, "a.png")
            (root / "notes.txt").write_text("nope", encoding="utf-8")
            sub = root / "pics"
            sub.mkdir()
            sub_img = self._img(sub, "b.jpg")

            images, unusable = resolve_reference_specs(
                [str(img), str(sub), str(root / "notes.txt"), str(root / "missing.png")]
            )
            self.assertIn(img, images)
            self.assertIn(sub_img, images)
            self.assertEqual(len(images), 2)
            self.assertEqual(len(unusable), 2)  # the .txt and the missing file

    def test_ingest_copies_with_user_prefix_and_indexing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            a = self._img(root, "a.png")
            b = self._img(root, "b.jpg")
            out = root / "reference"

            saved = ingest_user_references([a, b], out, start_index=0)
            self.assertEqual([p.name for p in saved], ["user_ref_00.png", "user_ref_01.jpg"])
            self.assertTrue(all(p.exists() for p in saved))

            more = ingest_user_references([a], out, start_index=len(saved))
            self.assertEqual([p.name for p in more], ["user_ref_02.png"])


if __name__ == "__main__":
    unittest.main()

import unittest

from blendahbot.refs import _is_usable, clean_query


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


if __name__ == "__main__":
    unittest.main()

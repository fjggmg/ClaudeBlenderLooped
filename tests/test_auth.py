import os
import shutil
import tempfile
import unittest

from blendahbot import auth


class ExtractTokenTests(unittest.TestCase):
    def test_rejoins_soft_wrapped_token(self):
        # setup-token soft-wraps stdout; reconstruction must rejoin the pieces.
        full = "sk-ant-oat01-" + "A" * 120
        wrapped = "\n".join(full[i : i + 20] for i in range(0, len(full), 20))
        out = "Visit https://... to approve\n" + wrapped + "\n\ntrailer\n"
        self.assertEqual(auth.extract_token(out), full)

    def test_single_line_token(self):
        full = "sk-ant-oat01-" + "B" * 100
        self.assertEqual(auth.extract_token(full + "\n"), full)

    def test_crlf_wrapped(self):
        full = "sk-ant-oat01-" + "C" * 100
        out = full[:40] + "\r\n" + full[40:]
        self.assertEqual(auth.extract_token(out), full)

    def test_no_token(self):
        self.assertIsNone(auth.extract_token("nothing here"))

    def test_too_short(self):
        self.assertIsNone(auth.extract_token("sk-ant-oat01-short"))


class TokenFileTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("BLENDAHBOT_HOME")
        self._tmp = tempfile.mkdtemp()
        os.environ["BLENDAHBOT_HOME"] = self._tmp
        self._prev_tok = os.environ.pop(auth.TOKEN_ENV, None)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("BLENDAHBOT_HOME", None)
        else:
            os.environ["BLENDAHBOT_HOME"] = self._prev
        if self._prev_tok is not None:
            os.environ[auth.TOKEN_ENV] = self._prev_tok
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_save_load_roundtrip(self):
        tok = "sk-ant-oat01-" + "D" * 100
        path = auth.save_token(tok)
        self.assertTrue(path.exists())
        self.assertEqual(auth.load_saved_token(), tok)

    def test_load_missing(self):
        self.assertIsNone(auth.load_saved_token())

    def test_auth_env_empty_then_populated(self):
        self.assertEqual(auth.auth_env(), {})
        tok = "sk-ant-oat01-" + "E" * 100
        auth.save_token(tok)
        self.assertEqual(auth.auth_env(), {auth.TOKEN_ENV: tok})

    def test_load_into_env_sets_token(self):
        tok = "sk-ant-oat01-" + "F" * 100
        auth.save_token(tok)
        self.assertEqual(auth.load_into_env(), tok)
        self.assertEqual(os.environ.get(auth.TOKEN_ENV), tok)


if __name__ == "__main__":
    unittest.main()

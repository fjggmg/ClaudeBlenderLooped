import unittest

from blendahbot.builder import parse_verdict


class ParseVerdictTests(unittest.TestCase):
    def test_plain_json(self):
        v = parse_verdict('{"satisfied": true, "score": 88, "summary": "good", "issues": [], "suggestions": []}')
        self.assertTrue(v.satisfied)
        self.assertEqual(v.score, 88)
        self.assertFalse(v.parse_failed)

    def test_fenced_json(self):
        text = "Here is my verdict:\n```json\n{\"satisfied\": false, \"score\": 40, \"issues\": [\"too dark\"]}\n```\n"
        v = parse_verdict(text)
        self.assertFalse(v.satisfied)
        self.assertEqual(v.score, 40)
        self.assertIn("too dark", v.issues)

    def test_embedded_json(self):
        text = "blah blah {\"satisfied\": true, \"score\": 200} trailing"
        v = parse_verdict(text)
        self.assertTrue(v.satisfied)
        self.assertEqual(v.score, 100)  # clamped

    def test_garbage_marks_parse_failed(self):
        v = parse_verdict("no json here at all")
        self.assertTrue(v.parse_failed)
        self.assertFalse(v.satisfied)
        self.assertEqual(v.score, 0)

    def test_non_integer_score(self):
        v = parse_verdict('{"satisfied": false, "score": "high"}')
        self.assertEqual(v.score, 0)


if __name__ == "__main__":
    unittest.main()

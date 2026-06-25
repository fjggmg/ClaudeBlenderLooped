import io
import unittest
from contextlib import redirect_stdout

from blendahbot.ui import Console


class UiMarkupSafetyTests(unittest.TestCase):
    """Model-derived text often contains '[/...]' which crashes rich markup
    parsing unless escaped. None of these may raise."""

    def _exercise(self, console: Console) -> None:
        console.tool_call("blender:execute_blender_code", "code with [/dim] and [bold] markers")
        console.tool_result("result [/] text", is_error=True)
        console.info("summary [/foo/bar] ok")
        console.thinking("thinking [/x] more")
        console.warn("· issue [/dim] here")
        console.success("done [/]")
        console.rule("round [/dim] 1")

    def test_rich_mode_does_not_crash_on_markup(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self._exercise(Console(plain=False))

    def test_plain_mode_does_not_crash(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self._exercise(Console(plain=True))


if __name__ == "__main__":
    unittest.main()

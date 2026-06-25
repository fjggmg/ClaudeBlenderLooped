import os
import unittest

from blendahbot.discovery import find_blender_mcp_command, find_claude_cli, DiscoveryError


class FindBlenderMcpTests(unittest.TestCase):
    def test_override_wins(self):
        self.assertEqual(find_blender_mcp_command(["x", "y"]), ["x", "y"])

    def test_env_command_parsed(self):
        os.environ["BLENDER_MCP_SERVER_CMD"] = r'C:\tools\blender-mcp.exe --transport stdio'
        try:
            cmd = find_blender_mcp_command()
            self.assertEqual(cmd[0], r"C:\tools\blender-mcp.exe")
            self.assertIn("--transport", cmd)
        finally:
            del os.environ["BLENDER_MCP_SERVER_CMD"]


class FindClaudeCliTests(unittest.TestCase):
    def test_override_existing_file(self):
        # Use this test file itself as a stand-in "executable".
        here = os.path.abspath(__file__)
        self.assertEqual(find_claude_cli(here), here)

    def test_env_override_missing_then_raises_or_falls_through(self):
        os.environ["BLENDAHBOT_CLAUDE_CLI"] = r"Z:\does\not\exist\claude.exe"
        try:
            # Either it finds a real claude elsewhere, or it raises — both are fine,
            # but it must NOT return the bogus path.
            try:
                result = find_claude_cli()
                self.assertNotEqual(result, r"Z:\does\not\exist\claude.exe")
            except DiscoveryError:
                pass
        finally:
            del os.environ["BLENDAHBOT_CLAUDE_CLI"]


if __name__ == "__main__":
    unittest.main()

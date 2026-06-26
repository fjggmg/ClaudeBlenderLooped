"""Assemble ClaudeAgentOptions for the builder and critic agents."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, McpSdkServerConfig

from .auth import auth_env
from .config import BotConfig
from .discovery import find_blender_mcp_command, find_claude_cli
from .prompts import builder_system_prompt, critic_system_prompt
from .skills import skills_dir
from .tools import DONE_TOOL, NOTE_TOOL

# Built-in tools the builder is allowed to use without prompting.
_BUILDER_BUILTINS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TodoWrite",
]


def _blender_server(config: BotConfig) -> dict[str, object]:
    cmd = find_blender_mcp_command(config.blender_mcp_cmd)
    return {
        "type": "stdio",
        "command": cmd[0],
        "args": cmd[1:],
        "env": {
            "BLENDER_MCP_HOST": config.blender_host,
            "BLENDER_MCP_PORT": str(config.blender_port),
        },
    }


def build_builder_options(
    config: BotConfig,
    tools_server: McpSdkServerConfig,
    stderr_cb,
) -> ClaudeAgentOptions:
    """Options for the persistent builder session."""
    cli = find_claude_cli(config.cli_path)
    render_dir = str(config.run_dir.resolve())

    allowed = [
        *_BUILDER_BUILTINS,
        "mcp__blender__*",  # all blender MCP tools (glob form the CLI understands)
        DONE_TOOL,
        NOTE_TOOL,
    ]

    return ClaudeAgentOptions(
        system_prompt=builder_system_prompt(render_dir, str(skills_dir())),
        mcp_servers={
            "blender": _blender_server(config),
            "bb": tools_server,
        },
        allowed_tools=allowed,
        permission_mode="bypassPermissions",
        max_turns=config.max_turns_per_round,
        max_budget_usd=config.budget_usd,
        model=config.model,
        cwd=str(config.run_dir.resolve()),
        cli_path=cli,
        setting_sources=config.setting_sources,
        add_dirs=[str(config.run_dir.resolve())],
        include_partial_messages=False,
        stderr=stderr_cb,
        env={
            **auth_env(),
            "BLENDER_MCP_HOST": config.blender_host,
            "BLENDER_MCP_PORT": str(config.blender_port),
        },
    )


def build_critic_options(config: BotConfig, work_dir: Path, stderr_cb) -> ClaudeAgentOptions:
    """Options for a one-shot critic pass: read-only, sees only the run dir."""
    cli = find_claude_cli(config.cli_path)
    return ClaudeAgentOptions(
        system_prompt=critic_system_prompt(),
        allowed_tools=["Read", "Glob"],
        permission_mode="bypassPermissions",
        max_turns=8,
        model=config.model,
        cwd=str(work_dir.resolve()),
        cli_path=cli,
        setting_sources=[],
        add_dirs=[str(config.run_dir.resolve())],
        stderr=stderr_cb,
        env=auth_env(),
    )

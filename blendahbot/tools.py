"""In-process SDK tools the builder agent can call.

The key one is ``declare_done``: the agent calls it when it believes the
creation fulfils the request. The orchestrator watches the shared
:class:`CompletionState` to know a round has converged, then hands off to the
independent critic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

# Name of the in-process MCP server; tools surface as ``mcp__bb__<tool>``.
SERVER_NAME = "bb"
DONE_TOOL = f"mcp__{SERVER_NAME}__declare_done"
NOTE_TOOL = f"mcp__{SERVER_NAME}__note_progress"


@dataclass
class CompletionState:
    """Mutable handoff between the builder agent and the orchestrator."""

    declared: bool = False
    summary: str = ""
    self_score: int = 0
    render_path: str = ""
    notes: list[str] | None = None

    def reset(self) -> None:
        self.declared = False
        self.summary = ""
        self.self_score = 0
        self.render_path = ""
        self.notes = []


def make_tools_server(state: CompletionState) -> McpSdkServerConfig:
    """Create the in-process MCP server bound to ``state``."""

    @tool(
        "declare_done",
        "Call this ONLY when the current Blender scene genuinely and fully satisfies "
        "the user's request at high quality, AND you have rendered it to a file and "
        "looked at that render to confirm it. An independent reviewer will then judge "
        "the render, so do not call this prematurely.",
        {
            "summary": Annotated[
                str, "What you built and how it satisfies the request."
            ],
            "self_score": Annotated[
                int, "Your honest 0-100 quality score for the current result."
            ],
            "render_path": Annotated[
                str, "Absolute path to the final render PNG you produced this round."
            ],
        },
    )
    async def declare_done(args: dict[str, object]) -> dict[str, object]:
        state.declared = True
        state.summary = str(args.get("summary", ""))
        try:
            state.self_score = int(args.get("self_score", 0))
        except (TypeError, ValueError):
            state.self_score = 0
        state.render_path = str(args.get("render_path", ""))
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Completion recorded. An independent reviewer will now "
                    "evaluate your render against the request.",
                }
            ]
        }

    @tool(
        "note_progress",
        "Optionally record a short progress note for the run log. Use sparingly for "
        "milestones (e.g. 'blocked out the main shapes', 'added materials').",
        {"message": Annotated[str, "A one-line progress note."]},
    )
    async def note_progress(args: dict[str, object]) -> dict[str, object]:
        if state.notes is None:
            state.notes = []
        msg = str(args.get("message", "")).strip()
        if msg:
            state.notes.append(msg)
        return {"content": [{"type": "text", "text": "Noted."}]}

    return create_sdk_mcp_server(SERVER_NAME, tools=[declare_done, note_progress])

"""Drive the builder session and the independent critic, and render their output."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    ServerToolUseBlock,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from claude_agent_sdk import ClaudeAgentOptions

from .artifacts import Transcript
from .auth import auth_env
from .config import BotConfig
from .discovery import find_claude_cli
from .errors import AUTH_REMEDIATION, AuthError, looks_like_auth_failure
from .options import build_critic_options
from .steering import STOP_TOKENS, Steering
from .ui import Console


@dataclass
class RoundResult:
    final_text: str = ""
    cost_usd: float | None = None
    num_turns: int = 0
    is_error: bool = False
    subtype: str = ""
    error: str = ""
    auth_failed: bool = False
    steered: bool = False
    user_stopped: bool = False


@dataclass
class Verdict:
    satisfied: bool = False
    score: int = 0
    summary: str = ""
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw: str = ""
    parse_failed: bool = False
    cost_usd: float = 0.0


# --------------------------------------------------------------------------
# Streaming the builder
# --------------------------------------------------------------------------

async def run_round(
    client: ClaudeSDKClient,
    prompt: str,
    console: Console,
    transcript: Transcript,
    steering: Steering | None = None,
) -> RoundResult:
    """Send ``prompt`` to the persistent builder client and stream the response.

    If ``steering`` is active, instructions typed mid-build interrupt the agent
    and are re-sent as authoritative updates, so the round may span several
    query/response cycles before it settles.
    """
    result = RoundResult()
    await client.query(prompt)

    while True:
        watcher: asyncio.Task | None = None
        if steering is not None and steering.enabled and not steering.stop_requested:
            watcher = asyncio.create_task(_steer_watcher(client, steering))

        try:
            async for message in client.receive_response():
                transcript.write("builder", message)
                _render_message(console, message)
                if isinstance(message, AssistantMessage):
                    texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                    if texts:
                        result.final_text = "\n".join(texts)
                elif isinstance(message, ResultMessage):
                    result.cost_usd = (result.cost_usd or 0.0) + (message.total_cost_usd or 0.0)
                    result.num_turns += message.num_turns
                    result.is_error = message.is_error
                    result.subtype = message.subtype
        except Exception as ex:  # noqa: BLE001 - surfaced via RoundResult, not swallowed
            result.is_error = True
            result.error = str(ex)

        steers: list[str] = []
        if watcher is not None:
            if watcher.done():
                with contextlib.suppress(Exception):
                    steers = watcher.result() or []
            else:
                watcher.cancel()
                with contextlib.suppress(BaseException):
                    await watcher
        if steering is not None:
            steers += steering.drain()

        if steering is not None and steering.stop_requested:
            result.user_stopped = True
            break

        if steers:
            result.steered = True
            text = "\n".join(dict.fromkeys(steers))  # dedup, keep order
            console.warn(f"steering with your message: {text}")
            await client.query(_steer_wrap(text))
            continue

        break

    if looks_like_auth_failure(result.final_text + " " + result.error):
        result.auth_failed = True
    return result


async def _steer_watcher(client: ClaudeSDKClient, steering: Steering) -> list[str]:
    """Wait for one user line, interrupt the agent, and return queued instructions."""
    first = await steering.queue.get()
    extra = steering.drain()
    with contextlib.suppress(Exception):
        await client.interrupt()
    msgs = ([first] if first.lower() not in STOP_TOKENS else []) + extra
    return msgs


def _steer_wrap(text: str) -> str:
    return (
        "The user is watching the build and just sent additional instructions. "
        "Treat these as authoritative updates to the request — apply them to the "
        "current scene now, then continue working toward a finished result:\n\n"
        f"{text}"
    )


def _render_message(console: Console, message: object) -> None:
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, TextBlock):
                console.agent(block.text)
            elif isinstance(block, ThinkingBlock):
                console.thinking(block.thinking)
            elif isinstance(block, ToolUseBlock):
                console.tool_call(_short_tool(block.name), _brief_input(block.name, block.input))
            elif isinstance(block, ServerToolUseBlock):
                console.tool_call(_short_tool(str(block.name)), _brief_input(str(block.name), block.input))
    elif isinstance(message, UserMessage):
        if isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    console.tool_result(_brief_result(block.content), bool(block.is_error))
    elif isinstance(message, SystemMessage):
        if message.subtype not in ("init",):
            data = getattr(message, "data", {}) or {}
            text = data.get("message") or message.subtype
            if text and message.subtype != "stream_event":
                console.thinking(f"[system:{message.subtype}] {text}")
    elif isinstance(message, ResultMessage):
        cost = f"${message.total_cost_usd:.3f}" if message.total_cost_usd else "n/a"
        if message.is_error:
            console.warn(f"round ended with error ({message.subtype}); turns={message.num_turns}, cost={cost}")
        else:
            console.info(f"round complete — turns={message.num_turns}, cost={cost}")


def _short_tool(name: str) -> str:
    return name.replace("mcp__blender__", "blender:").replace("mcp__bb__", "bb:")


def _brief_input(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("code", "command", "query", "url", "prompt", "filepath", "path", "summary", "message"):
        if key in inp and isinstance(inp[key], str):
            return inp[key].replace("\n", " ")
    return ", ".join(f"{k}={v}" for k, v in list(inp.items())[:3])


def _brief_result(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.replace("\n", " ")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "image":
                    parts.append("[image]")
            else:
                parts.append(str(item))
        return " ".join(parts).replace("\n", " ")
    return str(content)


# --------------------------------------------------------------------------
# The independent critic
# --------------------------------------------------------------------------

def _critic_prompt(
    request: str,
    image_paths: list[Path],
    scene_digest: str,
    reference_paths: list[Path] | None = None,
    previous_path: Path | None = None,
) -> str:
    images = "\n".join(f"  - {p}" for p in image_paths) or "  (no render available)"
    digest = scene_digest.strip() or "(scene summary unavailable)"
    refs = ""
    if reference_paths:
        ref_list = "\n".join(f"  - {p}" for p in reference_paths)
        refs = (
            "\nReference photos of the intended subject (approximate real-world examples — "
            "Read these too and compare the render against them):\n" + ref_list + "\n"
        )
    prev = ""
    if previous_path:
        prev = (
            "\nPrevious best render of this same project (Read it and compare):\n"
            f"  - {previous_path}\n"
            "If the CURRENT render is WORSE than this previous one in any way, its score MUST "
            "be LOWER than you would have given the previous render, and say so explicitly. "
            "Regressions are punished, not excused.\n"
        )
    return f"""\
Original request:

    {request}

Scene summary (factual object list from Blender — judge the PIXELS, not these labels):
{digest}

Render(s) to review (use the Read tool to open and look at each image file):
{images}
{refs}{prev}
Now evaluate and reply with the strict JSON verdict described in your instructions.
"""


async def run_critic(
    config: BotConfig,
    request: str,
    image_paths: list[Path],
    scene_digest: str,
    work_dir: Path,
    console: Console,
    transcript: Transcript,
    stderr_cb,
    reference_paths: list[Path] | None = None,
    previous_path: Path | None = None,
) -> Verdict:
    options = build_critic_options(config, work_dir, stderr_cb)
    prompt = _critic_prompt(request, image_paths, scene_digest, reference_paths, previous_path)
    chunks: list[str] = []
    err = ""
    cost = 0.0
    try:
        async for message in query(prompt=prompt, options=options):
            transcript.write("critic", message)
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        console.tool_call(
                            "critic:" + _short_tool(block.name),
                            _brief_input(block.name, block.input),
                        )
            elif isinstance(message, ResultMessage):
                cost += message.total_cost_usd or 0.0
    except Exception as ex:  # noqa: BLE001 - classified below, not swallowed blindly
        err = str(ex)
    text = "\n".join(chunks)
    if looks_like_auth_failure(text + " " + err):
        raise AuthError(AUTH_REMEDIATION)
    if err and not text.strip():
        return Verdict(
            satisfied=False,
            score=0,
            summary=f"The reviewer could not be reached: {err}",
            parse_failed=True,
            raw=err,
            cost_usd=cost,
        )
    verdict = parse_verdict(text)
    verdict.cost_usd = cost
    return verdict


async def run_selftest(config: BotConfig, stderr_cb) -> tuple[bool, str]:
    """Minimal, Blender-free round-trip to verify the claude CLI authenticates.

    Returns ``(ok, detail)``. ``detail`` is ``"auth"`` on an authentication
    failure so the caller can print the right remediation.
    """
    cli = find_claude_cli(config.cli_path)
    options = ClaudeAgentOptions(
        system_prompt="You are a connectivity test. Reply with exactly: READY",
        permission_mode="bypassPermissions",
        max_turns=2,
        model=config.model,
        cli_path=cli,
        setting_sources=[],
        stderr=stderr_cb,
        env=auth_env(),
    )
    text = ""
    err = ""
    try:
        async for message in query(prompt="Say READY.", options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text += block.text
    except Exception as ex:  # noqa: BLE001
        err = str(ex)
    blob = text + " " + err
    if looks_like_auth_failure(blob):
        return False, "auth"
    if "ready" in text.lower():
        return True, text.strip()
    return False, (err or text or "no response from the model").strip()


def parse_verdict(text: str) -> Verdict:
    """Extract the JSON verdict from the critic's reply, tolerantly."""
    raw = text.strip()
    obj = _extract_json_object(raw)
    if obj is None:
        return Verdict(satisfied=False, score=0, summary="Could not parse critic output.",
                       raw=raw, parse_failed=True)
    try:
        score = int(obj.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return Verdict(
        satisfied=bool(obj.get("satisfied", False)),
        score=max(0, min(100, score)),
        summary=str(obj.get("summary", "")),
        issues=[str(x) for x in obj.get("issues", []) if str(x).strip()],
        suggestions=[str(x) for x in obj.get("suggestions", []) if str(x).strip()],
        raw=raw,
    )


def _extract_json_object(text: str) -> dict | None:
    # Strip ```json fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    # Try the whole string.
    try:
        val = json.loads(text)
        if isinstance(val, dict):
            return val
    except json.JSONDecodeError:
        pass
    # Brace-match the first balanced {...}.
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        val = json.loads(candidate)
                        if isinstance(val, dict):
                            return val
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None

"""Core runner shared by the schedule and webhook triggers."""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    TextBlock,
    query,
)

from .config import settings
from .prompts import system_prompt
from .tools import TOOL_NAMES, build_tool_server

VALID_TASKS = {"triage", "compliance", "all"}

# Read/author files + our two side-effect tools. Bash is intentionally absent.
_EDIT_TOOLS = ["Read", "Grep", "Glob", "Write", "Edit"]


async def _deny_bash(input_data, tool_use_id, context):
    """Defense-in-depth: never let the unattended agent run shell commands."""
    if input_data.get("tool_name") == "Bash":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Bash is disabled. Use Write/Edit for changes and the "
                    "open_pull_request tool to propose them."
                ),
            }
        }
    return {}


def _build_options(task: str) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=settings.agent_model,
        system_prompt=system_prompt(task),
        cwd=str(settings.resolved_repo_dir()),
        mcp_servers={"ops": build_tool_server()},
        allowed_tools=[*_EDIT_TOOLS, *TOOL_NAMES],
        permission_mode="acceptEdits",
        max_turns=80,
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_deny_bash])]},
    )


async def run_agent(task: str = "all", note: str | None = None) -> str:
    """Run one agent pass for the given task. Returns the agent's final text."""
    if task not in VALID_TASKS:
        raise ValueError(f"task must be one of {sorted(VALID_TASKS)}, got {task!r}")

    kickoff = f"Begin the {task} run now."
    if note:
        kickoff += f"\n\nTrigger context: {note}"

    transcript: list[str] = []
    async for message in query(prompt=kickoff, options=_build_options(task)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    transcript.append(block.text)

    return "\n".join(transcript).strip() or "(agent produced no text output)"

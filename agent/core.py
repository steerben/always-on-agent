"""Core runner shared by the schedule and webhook triggers."""

from __future__ import annotations

import json
import logging

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    TextBlock,
    query,
)

from .action_tools import ACTION_TOOL_NAMES
from .config import settings
from .memory import Outcome, prior_context_prompt, record_outcome, register_incident
from .observability import RunRecorder, escalation_decision
from .prompts import system_prompt
from .tools import TOOL_NAMES, build_tool_server

logger = logging.getLogger("ops-agent.core")

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
    # Prepend any calibration context from past runs (human overrides, recurring
    # incidents) so the agent learns across runs. Empty string when no state.
    base = system_prompt(task)
    prior = prior_context_prompt()
    full_prompt = f"{base}\n\n{prior}" if prior else base

    return ClaudeAgentOptions(
        model=settings.agent_model,
        system_prompt=full_prompt,
        cwd=str(settings.resolved_repo_dir()),
        mcp_servers={"ops": build_tool_server()},
        allowed_tools=[*_EDIT_TOOLS, *TOOL_NAMES, *ACTION_TOOL_NAMES],
        permission_mode="acceptEdits",
        max_turns=80,
        hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[_deny_bash])]},
    )


def _latest_agent_comment(issue: dict) -> dict | None:
    for comment in reversed(issue.get("comments") or []):
        if isinstance(comment, dict) and comment.get("author") == "always-on-ops-agent":
            return comment
    return None


def _confidence_from_text(text: str) -> str | None:
    lowered = (text or "").lower()
    for level in ("confirmed", "suspected", "none"):
        if level in lowered:
            return level
    return None


def _capture_run_state(recorder: RunRecorder) -> None:
    """Read the issues the agent wrote and record decisions + durable memory.

    Closes the loop: decisions feed the observability report's escalation gate,
    and outcomes/fingerprints feed the next run's prior_context_prompt. This is
    best-effort bookkeeping and must never mask the run itself.
    """
    issues_dir = settings.resolved_repo_dir() / "issues"
    if not issues_dir.is_dir():
        return
    for path in sorted(issues_dir.glob("*.json")):
        try:
            issue = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        issue_id = issue.get("id", path.stem)
        severity = issue.get("severity")
        is_compliance = "compliance" in (issue.get("labels") or []) or issue_id.startswith("COMPLIANCE-")

        if is_compliance:
            recorder.record_decision(issue_id, severity=severity, kind="compliance")
            record_outcome(Outcome(issue_id=issue_id, agent_severity=severity, agent_classification="compliance"))
            continue

        comment = _latest_agent_comment(issue)
        if comment is None:
            continue  # not triaged by this agent on this run
        classification = str(comment.get("classification", ""))
        is_incident = not any(
            marker in classification.lower() for marker in ("not an incident", "feature request")
        )
        recorder.record_decision(
            issue_id,
            severity=severity,
            confidence=_confidence_from_text(str(comment.get("correlation", ""))),
            kind="incident" if is_incident else "not_incident",
        )
        record_outcome(Outcome(issue_id=issue_id, agent_severity=severity, agent_classification=classification))
        if is_incident:
            register_incident(issue)


async def run_agent(task: str = "all", note: str | None = None) -> str:
    """Run one agent pass for the given task. Returns the agent's final text."""
    if task not in VALID_TASKS:
        raise ValueError(f"task must be one of {sorted(VALID_TASKS)}, got {task!r}")

    kickoff = f"Begin the {task} run now."
    if note:
        kickoff += f"\n\nTrigger context: {note}"

    recorder = RunRecorder(task=task, model=settings.agent_model)
    transcript: list[str] = []
    summary = "(agent produced no text output)"
    error: str | None = None
    try:
        async for message in query(prompt=kickoff, options=_build_options(task)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        transcript.append(block.text)
                    else:
                        name = getattr(block, "name", None)
                        if name:
                            recorder.record_tool_call(name, getattr(block, "input", None))
            usage = getattr(message, "usage", None)
            if usage is not None:
                try:
                    recorder.record_usage(dict(usage))
                except (TypeError, ValueError):
                    pass
            num_turns = getattr(message, "num_turns", None)
            if isinstance(num_turns, int):
                recorder.set_turns(num_turns)
        summary = "\n".join(transcript).strip() or summary
    except Exception as exc:  # recorded for observability, then re-raised
        error = repr(exc)
        summary = f"(agent run failed: {error})"
        raise
    finally:
        try:
            _capture_run_state(recorder)
        except Exception:
            logger.exception("post-run state capture failed")
        report = recorder.finish(summary, error=error)
        try:
            saved = recorder.save(report)
            logger.info(
                "run report saved: %s (escalation=%s)",
                saved,
                escalation_decision(report.decisions).get("action"),
            )
        except Exception:
            logger.exception("failed to save run report")

    return summary

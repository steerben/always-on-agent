#!/usr/bin/env python3
"""Trigger entrypoint for the Always-On Ops Agent on Claude Managed Agents.

This is what a *schedule* (cron / GitHub Action / Lambda on a timer) or a
*webhook* (an HTTP handler) invokes. It is NOT the agent — the agent lives on
Anthropic's infrastructure (see platform/agent.yaml). This script just starts a
session and sends the kickoff message, then streams the agent's findings.

For a no-integration demo it embeds a dataset (default: the demo/ sandbox)
inline in the kickoff message, so the agent needs neither a connected repo nor
Slack — it streams its triage + compliance findings straight back.

Prereqs:
    pip install anthropic            # a version with Managed Agents beta support
    export ANTHROPIC_API_KEY=...
    export AGENT_ID=...              # from creating platform/agent.yaml
    export ENVIRONMENT_ID=...        # from creating an environment (see README)

Usage:
    python platform/trigger.py --task all
    python platform/trigger.py --task compliance --data-dir ./contracts ...
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "demo"


def build_data_block(data_dir: Path) -> str:
    """Embed every file under data_dir as `=== FILE: <relpath> ===` sections."""
    files = sorted(p for p in data_dir.rglob("*") if p.is_file())
    if not files:
        sys.exit(f"No files found under {data_dir}")
    sections = [
        f"=== FILE: {p.relative_to(data_dir)} ===\n{p.read_text()}" for p in files
    ]
    return "\n\n".join(sections)


def kickoff_text(task: str, data_dir: Path, note: str | None) -> str:
    header = (
        f"Run the {task} task now. Below is the operational data to analyze "
        "(issues, deploys, runbooks, contracts, and the compliance policy)."
    )
    if note:
        header += f"\n\nTrigger context: {note}"
    return f"{header}\n\n{build_data_block(data_dir)}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Trigger an Always-On Ops Agent session")
    ap.add_argument("--task", default="all", choices=["triage", "compliance", "all"])
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    ap.add_argument("--note", default=None)
    args = ap.parse_args()

    agent_id = os.environ.get("AGENT_ID")
    environment_id = os.environ.get("ENVIRONMENT_ID")
    if not agent_id or not environment_id:
        sys.exit("Set AGENT_ID and ENVIRONMENT_ID (see platform/README.md).")

    client = Anthropic()  # reads ANTHROPIC_API_KEY; sets the beta header itself

    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=environment_id,
        title=f"ops-{args.task}",
    )
    print(f"[session {session.id}] streaming...\n", file=sys.stderr)

    with client.beta.sessions.events.stream(session.id) as stream:
        client.beta.sessions.events.send(
            session.id,
            events=[
                {
                    "type": "user.message",
                    "content": [
                        {"type": "text", "text": kickoff_text(args.task, Path(args.data_dir), args.note)}
                    ],
                }
            ],
        )
        for event in stream:
            if event.type == "agent.message":
                for block in event.content:
                    print(getattr(block, "text", ""), end="")
            elif event.type == "agent.tool_use":
                print(f"\n[tool: {event.name}]", file=sys.stderr)
            elif event.type == "session.status_idle":
                print("\n\n[agent finished]", file=sys.stderr)
                break


if __name__ == "__main__":
    main()

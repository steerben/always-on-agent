#!/usr/bin/env python3
"""Trigger entrypoint for the Always-On Ops Agent on Claude Managed Agents.

This is what a *schedule* (cron / GitHub Action / Lambda on a timer) or a
*webhook* (an HTTP handler) invokes. It is NOT the agent — the agent lives on
Anthropic's infrastructure (see platform/agent.yaml).

Data model (deliberate split):
  - STATIC REFERENCE (deploys, runbooks, contracts, compliance-policy, metrics)
    is uploaded via the Files API and mounted READ-ONLY under /mnt/data/. It
    rarely changes, so it doesn't belong in every event.
  - The FIRING ISSUE(S) ride in the triggering `user.message` event — exactly as
    a real alert/webhook would deliver the work item. Pass --issue for a single
    inline payload (webhook), or let it read the issues/ dir (demo).
  - Cross-run MEMORY is a native memory store (MEMORY_STORE_ID), attached
    read_write and mounted under /mnt/memory/.

Tasks:
  triage      -> issue(s) in the event, reference mounted
  compliance  -> scan mounted contracts vs policy (no issue needed)
  all         -> both
  maintain    -> memory-curation run (see --task maintain); no data, no issues

Prereqs:
    pip install anthropic
    export ANTHROPIC_API_KEY=...     # a workspace API key (Console > Settings > Keys)
    export AGENT_ID=... ENVIRONMENT_ID=...
    export MEMORY_STORE_ID=...       # optional; enables cross-run memory

Usage:
    python platform/trigger.py --task triage                 # demo issues in event
    python platform/trigger.py --task triage --issue alert.json
    python platform/trigger.py --task all
    python platform/trigger.py --task maintain               # daily memory curation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "demo"
DATA_MOUNT = "/mnt/data"

# Reference subtrees that get mounted (everything the agent consults but does
# not "act on"). Issues are deliberately excluded — they arrive in the event.
_REFERENCE_NAMES = {"deploys", "runbooks", "contracts", "metrics", "compliance-policy.md"}


def reference_files(data_dir: Path) -> list[Path]:
    out: list[Path] = []
    for name in sorted(_REFERENCE_NAMES):
        p = data_dir / name
        if p.is_dir():
            out += [f for f in sorted(p.rglob("*")) if f.is_file()]
        elif p.is_file():
            out.append(p)
    return out


def upload_reference(client: Anthropic, data_dir: Path) -> list[dict]:
    """Upload static reference files and return file-resource specs (read-only)."""
    resources: list[dict] = []
    for path in reference_files(data_dir):
        rel = path.relative_to(data_dir).as_posix()
        uploaded = client.beta.files.upload(file=path)
        resources.append({"type": "file", "file_id": uploaded.id, "mount_path": f"{DATA_MOUNT}/{rel}"})
        print(f"  mounted {rel} -> {uploaded.id}", file=sys.stderr)
    return resources


def memory_resource() -> list[dict]:
    store_id = os.environ.get("MEMORY_STORE_ID")
    if not store_id:
        return []
    return [
        {
            "type": "memory_store",
            "memory_store_id": store_id,
            "access": "read_write",
            "instructions": (
                "Your long-term ops memory. Read incidents/ and overrides/ before "
                "triaging; record incident fingerprints and human overrides after."
            ),
        }
    ]


def load_issue_payload(args) -> str:
    """The firing issue(s), embedded in the event. --issue wins; else demo issues/."""
    if args.issue:
        return Path(args.issue).read_text()
    issues_dir = Path(args.data_dir) / "issues"
    files = sorted(issues_dir.glob("*.json")) if issues_dir.is_dir() else []
    if not files:
        sys.exit(f"No issues found (looked for --issue or {issues_dir}/*.json)")
    return "\n\n".join(f"=== ISSUE: {p.name} ===\n{p.read_text()}" for p in files)


def kickoff_text(args) -> str:
    task = args.task
    if task == "maintain":
        return (
            "MEMORY MAINTENANCE RUN. Do NOT triage or scan anything. Read your "
            "entire memory store under /mnt/memory/ and curate it: merge duplicate "
            "incident fingerprints (summing their counts), prune entries with no "
            "sighting in over 90 days, keep overrides/ concise and current, and "
            "keep every file small and high-signal. Report what you changed."
        )

    parts: list[str] = []
    if task in ("triage", "all"):
        parts.append(
            "A new incident/issue has fired (payload below). Triage it using the "
            f"reference data mounted read-only under {DATA_MOUNT}/ "
            "(deploys/, runbooks/, metrics/). The issue(s):\n\n" + load_issue_payload(args)
        )
    if task in ("compliance", "all"):
        parts.append(
            f"Also scan the contracts under {DATA_MOUNT}/contracts/ against "
            f"{DATA_MOUNT}/compliance-policy.md."
        )
    if args.note:
        parts.append(f"Trigger context: {args.note}")
    return "\n\n".join(parts)


def build_resources(client: Anthropic, args) -> list[dict]:
    if args.task == "maintain":
        return memory_resource()  # only the store; no data, no issues
    reference = upload_reference(client, Path(args.data_dir))
    return reference + memory_resource()


def main() -> None:
    ap = argparse.ArgumentParser(description="Trigger an Always-On Ops Agent session")
    ap.add_argument("--task", default="all", choices=["triage", "compliance", "all", "maintain"])
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Reference data root.")
    ap.add_argument("--issue", default=None, help="Path to a single issue JSON (webhook payload).")
    ap.add_argument("--note", default=None)
    args = ap.parse_args()

    agent_id = os.environ.get("AGENT_ID")
    environment_id = os.environ.get("ENVIRONMENT_ID")
    if not agent_id or not environment_id:
        sys.exit("Set AGENT_ID and ENVIRONMENT_ID (see platform/README.md).")

    client = Anthropic()
    resources = build_resources(client, args)

    session = client.beta.sessions.create(
        agent=agent_id,
        environment_id=environment_id,
        title=f"ops-{args.task}",
        resources=resources,
    )
    print(f"[session {session.id}] {len(resources)} resources; streaming...\n", file=sys.stderr)

    with client.beta.sessions.events.stream(session.id) as stream:
        client.beta.sessions.events.send(
            session.id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": kickoff_text(args)}]}],
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

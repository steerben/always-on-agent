#!/usr/bin/env python3
"""Trigger entrypoint for the Always-On Ops Agent on Claude Managed Agents.

This is what a *schedule* (cron / GitHub Action / Lambda on a timer) or a
*webhook* (an HTTP handler) invokes. It is NOT the agent — the agent lives on
Anthropic's infrastructure (see platform/agent.yaml).

Data model:
  - STATIC REFERENCE (deploys, runbooks, compliance-policy, metrics) is uploaded
    via the Files API and mounted READ-ONLY under /mnt/data/.
  - The ONE work item per trigger — an issue (JSON) or a contract (Markdown) —
    rides in the triggering `user.message` event. The agent classifies it
    (incident / feature_request / contract) and routes accordingly.
  - Cross-run MEMORY is the attached store (MEMORY_STORE_ID), mounted read_write
    under /mnt/memory/.

Prereqs:
    pip install anthropic
    export ANTHROPIC_API_KEY=...     # a workspace API key (Console > Settings > Keys)
    export AGENT_ID=... ENVIRONMENT_ID=...
    export MEMORY_STORE_ID=...       # optional; enables cross-run memory

Usage (one item per trigger):
    python platform/trigger.py                              # default: one demo issue
    python platform/trigger.py --item demo/issues/DEMO-102.json   # a feature request
    python platform/trigger.py --item demo/contracts/contoso-cloud.md  # a contract
    python platform/trigger.py --maintain                  # daily memory curation
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from anthropic import Anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "demo"
DEFAULT_ITEM = DEFAULT_DATA_DIR / "issues" / "DEMO-101.json"
DATA_MOUNT = "/mnt/data"

# Static reference that gets mounted. Note: NOT contracts and NOT issues — those
# are work items delivered in the event. The compliance *policy* stays mounted.
_REFERENCE_NAMES = {"deploys", "runbooks", "metrics", "compliance-policy.md"}


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
                "handling the item; record fingerprints and human overrides after."
            ),
        }
    ]


def kickoff_text(args) -> str:
    if args.maintain:
        return (
            "MEMORY MAINTENANCE RUN. Do NOT classify or handle any work item. Read "
            "your entire memory store under /mnt/memory/ and curate it: merge "
            "duplicate incident fingerprints (summing counts), prune entries with "
            "no sighting in over 90 days, keep overrides/ concise and current, and "
            "keep every file small and high-signal. Report what you changed."
        )
    item_path = Path(args.item)
    item = item_path.read_text()
    text = (
        "A work item has fired. Classify it (incident, feature_request, or "
        f"contract) and run the matching procedure, using the reference data "
        f"mounted read-only under {DATA_MOUNT}/.\n\n"
        f"=== ITEM: {item_path.name} ===\n{item}"
    )
    if args.note:
        text += f"\n\nTrigger context: {args.note}"
    return text


def build_resources(client: Anthropic, args) -> list[dict]:
    if args.maintain:
        return memory_resource()  # only the store
    return upload_reference(client, Path(args.data_dir)) + memory_resource()


def main() -> None:
    ap = argparse.ArgumentParser(description="Trigger an Always-On Ops Agent session")
    ap.add_argument("--item", default=str(DEFAULT_ITEM),
                    help="Single work item to send: an issue JSON or a contract MD.")
    ap.add_argument("--maintain", action="store_true", help="Run memory curation instead.")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Reference data root.")
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
        title="ops-maintain" if args.maintain else f"ops-{Path(args.item).stem}",
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

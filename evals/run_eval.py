"""CLI entry point for the eval harness.

    python -m evals.run_eval [--mode score|live] [--repo-dir DIR]
                             [--golden PATH] [--threshold 0.8]

score mode (default): score the repo's current issues/ against golden.json,
print the report card, and exit 0 if overall >= threshold else 1 (CI gate).

live mode: run one agent pass first (agent.core.run_agent), then score. Degrades
gracefully -- if ANTHROPIC_API_KEY is unset or the SDK import/run fails, it
prints a clear message and exits without a traceback.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .score import format_report_card, score_run

# repo root = this package's parent's parent (evals/ lives at <repo>/evals).
DEFAULT_REPO_DIR = Path(__file__).resolve().parent.parent
DEFAULT_GOLDEN = Path(__file__).resolve().parent / "golden.json"


def run_live_pass(task: str = "all", note: str | None = None) -> tuple[bool, str]:
    """Try to run one live agent pass. Never raises.

    Returns (ok, message). ok=False means we degraded gracefully (no key, import
    error, or runtime error) and the caller should still proceed to score
    whatever is already in the repo.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, (
            "ANTHROPIC_API_KEY is not set; skipping the live agent run. "
            "Scoring the repo's current issues/ as-is."
        )
    try:
        import asyncio

        from agent.core import run_agent  # lazy import: only needed in live mode
    except Exception as exc:  # noqa: BLE001 - degrade on any import failure
        return False, (
            f"Could not import agent.core ({exc.__class__.__name__}: {exc}); "
            "skipping the live agent run. Scoring the repo's current issues/ as-is."
        )
    try:
        summary = asyncio.run(run_agent(task=task, note=note))
        return True, summary
    except Exception as exc:  # noqa: BLE001 - degrade on any runtime failure
        return False, (
            f"Live agent run failed ({exc.__class__.__name__}: {exc}); "
            "scoring the repo's current issues/ as-is."
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.run_eval",
        description="Score an Always-On Ops Agent run against golden ground truth.",
    )
    p.add_argument("--mode", choices=["score", "live"], default="score")
    p.add_argument("--repo-dir", type=Path, default=DEFAULT_REPO_DIR)
    p.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    p.add_argument("--threshold", type=float, default=0.8,
                   help="Minimum overall score to exit 0 (CI gate). Default 0.8.")
    p.add_argument("--task", choices=["triage", "compliance", "all"], default="all",
                   help="Which agent task to run in live mode. Default all.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.mode == "live":
        ok, message = run_live_pass(task=args.task)
        print(message)
        print()

    card = score_run(args.repo_dir, args.golden)
    print(format_report_card(card))

    passed = card["overall_score"] >= args.threshold
    print()
    print(
        f"Gate: overall {card['overall_score'] * 100:.1f}% "
        f"{'>=' if passed else '<'} threshold {args.threshold * 100:.1f}% "
        f"-> {'PASS' if passed else 'FAIL'}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

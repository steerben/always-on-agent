"""Schedule (cron) entrypoint.

The platform's scheduler runs, e.g.:  python -m agent --task all
"""

from __future__ import annotations

import argparse
import asyncio

from .core import VALID_TASKS, run_agent


def main() -> None:
    parser = argparse.ArgumentParser(description="Always-On Ops Agent (scheduled run)")
    parser.add_argument(
        "--task",
        default="all",
        choices=sorted(VALID_TASKS),
        help="Which capability to run (default: all).",
    )
    parser.add_argument("--note", default=None, help="Optional trigger context.")
    args = parser.parse_args()

    summary = asyncio.run(run_agent(task=args.task, note=args.note))
    print(summary)


if __name__ == "__main__":
    main()

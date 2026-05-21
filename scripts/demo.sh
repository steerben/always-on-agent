#!/usr/bin/env bash
# Local, self-contained demo: runs the agent over the demo/ sandbox in DRY_RUN
# (no Slack, no GitHub) and shows the changes it proposes.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "== Resetting demo sandbox =="
git restore demo 2>/dev/null || true
git clean -fdq demo 2>/dev/null || true

export REPO_DIR="$PWD/demo"
export DRY_RUN=true

echo
echo "== Issues before the run =="
ls demo/issues

echo
echo "== Running the agent (task=all, DRY_RUN) =="
uv run python -m agent --task all

echo
echo "== What the agent changed in demo/ =="
git status --short demo
echo
git --no-pager diff -- demo/issues
echo
echo "(New compliance findings, if any:)"
ls demo/issues | grep -i compliance || echo "  none"

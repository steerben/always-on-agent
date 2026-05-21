#!/usr/bin/env bash
# One-time setup: create the Managed Agent (from agent.yaml) and a cloud
# environment, then print the IDs to export.
#
#   export ANTHROPIC_API_KEY=...
#   eval "$(./platform/setup.sh)"     # captures AGENT_ID + ENVIRONMENT_ID
# or just run it and copy the printed export lines.
#
# Progress goes to stderr; only the `export ...` lines go to stdout.
set -euo pipefail

API="https://api.anthropic.com"
BETA="managed-agents-2026-04-01"
HERE="$(cd "$(dirname "$0")" && pwd)"

log() { echo "$@" >&2; }

: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY first}"
command -v jq >/dev/null || { log "error: jq is required"; exit 1; }

# agent.yaml is the single source of truth; convert it to JSON for the API.
PYCONV='import sys,json,yaml; json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)'
to_json() { python3 -c "$PYCONV" "$1" 2>/dev/null || uv run python -c "$PYCONV" "$1"; }

log "Creating agent from agent.yaml ..."
AGENT_JSON="$(to_json "$HERE/agent.yaml")" || {
  log "error: could not convert agent.yaml (needs PyYAML: 'pip install pyyaml' or run via uv)"; exit 1; }

agent="$(curl -sS --fail-with-body "$API/v1/agents" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: $BETA" \
  -H "content-type: application/json" \
  -d "$AGENT_JSON")"
AGENT_ID="$(jq -er '.id' <<<"$agent")"
log "  agent id: $AGENT_ID (version $(jq -r '.version' <<<"$agent"))"

log "Creating cloud environment ..."
environment="$(curl -sS --fail-with-body "$API/v1/environments" \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: $BETA" \
  -H "content-type: application/json" \
  -d '{"name":"ops-env","config":{"type":"cloud","networking":{"type":"unrestricted"}}}')"
ENVIRONMENT_ID="$(jq -er '.id' <<<"$environment")"
log "  environment id: $ENVIRONMENT_ID"

log ""
log "Done. Run:  python platform/trigger.py --task all"

# stdout: the export lines (so `eval \"\$(./platform/setup.sh)\"` works).
echo "export AGENT_ID=$AGENT_ID"
echo "export ENVIRONMENT_ID=$ENVIRONMENT_ID"

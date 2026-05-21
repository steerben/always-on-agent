# Always-On Ops Agent — Claude Managed Agents

This is the variant for **Claude Managed Agents** (Console > Managed Agents,
`POST /v1/agents`). The agent runs on Anthropic's infrastructure and is defined
declaratively — there's no container to build.

> The sibling `agent/` directory is a *self-hosted* variant built on the Claude
> Agent SDK (Docker + FastAPI). It does **not** apply to this platform; use the
> files here instead.

## How the platform works

| Concept | Here |
|---|---|
| **Agent** | `agent.yaml` — model, system prompt, tools (`agent_toolset_20260401` = bash/read/write/edit/glob/grep/web_fetch/web_search), optional MCP servers. |
| **Environment** | A cloud container Anthropic provisions for sessions. |
| **Session** | One run of the agent over one task. |
| **Trigger** | There is no built-in cron/webhook. *You* start a session and send the kickoff message — from a scheduler or an HTTP handler. `trigger.py` does this. |

## Quick path (two commands)

```bash
export ANTHROPIC_API_KEY=...
eval "$(./platform/setup.sh)"      # creates agent + environment + memory store, sets IDs
python platform/trigger.py         # sends one demo issue; the agent classifies + handles it
```

`setup.sh` creates the agent from `agent.yaml`, a cloud environment, and a
memory store, then prints `export AGENT_ID=... / ENVIRONMENT_ID=... /
MEMORY_STORE_ID=...` (progress goes to stderr, so `eval` captures just the IDs).
`trigger.py` attaches the memory store to each session so the agent learns
across runs. The manual steps below do the same by hand.

## 1. Create the agent (once)

Pick one:

- **Console:** Build > Managed Agents > paste `agent.yaml` > Create agent.
- **CLI:** `ant beta:agents create < platform/agent.yaml`
- **API:** `POST https://api.anthropic.com/v1/agents` with the JSON form of `agent.yaml`
  (headers `anthropic-beta: managed-agents-2026-04-01`, `anthropic-version: 2023-06-01`).

Save the returned id → `export AGENT_ID=...`

## 2. Create an environment (once)

```bash
curl -sS https://api.anthropic.com/v1/environments \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" \
  -H "content-type: application/json" \
  -d '{"name":"ops-env","config":{"type":"cloud","networking":{"type":"unrestricted"}}}'
```

Save the returned id → `export ENVIRONMENT_ID=...`

## 3. Create a memory store (once)

Gives the agent cross-run memory. `trigger.py` attaches it to each session.

```bash
curl -sS https://api.anthropic.com/v1/memory_stores \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" \
  -H "content-type: application/json" \
  -d '{"name":"ops-agent-memory","description":"Recurring incident fingerprints and human triage overrides."}'
```

Save the returned id → `export MEMORY_STORE_ID=...`

## Data model: classify-and-route

Each trigger delivers **exactly one work item**, and the agent (Sonnet) starts
by **classifying** it, then runs the matching procedure:

| Item type | Procedure |
|---|---|
| `incident` | correlate with deploys + runbooks (+ metrics), assign severity, escalation gate, propose rollback |
| `feature_request` | label, route to backlog, do **not** page |
| `contract` | check against the compliance policy, emit findings |

What's mounted vs. delivered:

- **Static reference** — `deploys`, `runbooks`, `compliance-policy.md`, `metrics`
  — is uploaded via the Files API and **mounted read-only** under `/mnt/data/`.
- **The work item** — an issue **or a contract** — rides in the **triggering
  event** (`user.message`). (Contracts and issues are *not* mounted; only the
  stable compliance *policy* is.)
- **Memory** (`/mnt/memory/`) is the attached store, `read_write`.

## 4. Demo it (no Slack, no repo)

One item per trigger. The agent classifies and handles it, streaming findings
back — nothing external needs connecting.

```bash
export ANTHROPIC_API_KEY=... AGENT_ID=... ENVIRONMENT_ID=... MEMORY_STORE_ID=...
python platform/trigger.py                                       # default: one issue (incident)
python platform/trigger.py --item demo/issues/DEMO-102.json      # a feature request
python platform/trigger.py --item demo/contracts/contoso-cloud.md # a contract
```

### Webhook shape

A webhook handler does three calls: create a session (mounting reference +
memory via `resources[]`), `POST /sessions/{id}/events` with the work item as
the `user.message`, then stream `/sessions/{id}/stream`. `trigger.py --item
<payload>` is the convenient version; point your handler at the same logic.

## 5. Schedule or webhook

`trigger.py` is the entrypoint either way:

- **Schedule:** cron / GitHub Actions `schedule:` / EventBridge — e.g. a nightly
  contract sweep that fires one `--item <contract>` per contract.
- **Webhook:** call it from a tiny HTTP handler (FastAPI/Lambda) on `POST`,
  passing the firing item (issue or contract) via `--item`.

The schedule/webhook lives in *your* infra; it only needs `ANTHROPIC_API_KEY`,
`AGENT_ID`, `ENVIRONMENT_ID` (and `MEMORY_STORE_ID`).

## Keeping memory fresh

Cross-run memory is **live, not periodic**: the store is mounted `read_write`,
so the agent reads `incidents/`+`overrides/` at the start of every run and writes
new learnings at the end — the next issue automatically sees the latest store.
No daily job is required for the agent to have up-to-date learnings.

A scheduled job adds **curation**, not freshness:

```bash
# daily: merge duplicate fingerprints, prune stale entries, tidy overrides/
0 3 * * *  python /app/platform/trigger.py --task maintain
```

Two more levers:
- **Human overrides, immediately:** push a correction into the store anytime via
  the memories API — `POST /v1/memory_stores/$MEMORY_STORE_ID/memories` with
  `path=/overrides/<issue>.md`. The next run reads it as trusted memory.
- **Native consolidation:** the platform's **Dreams** feature performs offline
  memory consolidation; enable it on the store instead of (or alongside) the
  `maintain` cron.

## 6. Going live (PR + Slack)

Uncomment the `mcp_servers` + `mcp_toolset` block in `agent.yaml`, point it at
your GitHub and Slack MCP servers, and update the agent. The system prompt
already instructs the agent to open a PR (never merge) and post to Slack *when
those tools are present* — no prompt changes needed.

## Feature parity with the self-hosted `agent/` variant

The self-hosted variant added five capabilities. Because Anthropic owns the
agent loop here, each one is either folded into the declarative agent, moved to
the trigger, or already provided natively:

| Capability (`agent/…`) | Here |
|---|---|
| **Escalation gate** (`observability.escalation_decision`) | Encoded in the agent system prompt: P0/P1 + confirmed → page; P0/P1 + suspected → PR-and-warn; else PR-only. |
| **Action tools** (`action_tools`: metrics check, rollback proposal) | Folded into the system prompt — the agent uses its built-in `read`/`write` to check `metrics/recent.json` and to write `rollbacks/*.json` (never executed). No custom tool needed; the `_impl` fns remain reusable if you later want `type: custom` tools. |
| **Cross-run memory** (`memory.prior_context_prompt`) | **Native Memory store.** `setup.sh` creates a store; `trigger.py` attaches it to each session (`read_write`), mounted at `/mnt/memory/`. The agent reads `incidents/` + `overrides/` before triaging and updates them after — owning its own memory, with versioned audit history. |
| **Observability** (`RunRecorder`, `/runs` dashboard) | **Native.** Every session's events, tool calls, tokens, and cost are recorded server-side — see Console → Analytics (Logs / Usage / Cost). No reimplementation. |
| **Interactive Slack** (`slack_interactive`) | The signing/parse/decision helpers are reusable, but the inbound callback endpoint stays in *your* infra (the platform has no inbound webhook). See the steering pattern below. |

### Human-in-the-loop via session steering

Managed Agents supports steering a session mid-run by sending more `user.message`
events. That maps cleanly to the Slack approve/dismiss flow:

1. The agent posts the Block Kit message (built with
   `agent.slack_interactive.build_findings_message`) via the Slack MCP, then goes
   idle awaiting a decision.
2. Your `/slack/actions` handler (same infra as the trigger) verifies the
   signature (`verify_slack_signature`), parses it (`parse_action_payload`), and
   maps it to a decision (`decide_from_action`).
3. Instead of mutating local state, it sends that decision back into the **same
   session** as a `user.message` event — so the agent itself carries out the
   approved action (page, keep PR, or dismiss).

This keeps the human gate without any loop you have to host — only the thin
signature-verifying handler.

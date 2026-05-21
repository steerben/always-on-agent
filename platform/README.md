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
eval "$(./platform/setup.sh)"          # creates the agent + environment, sets the IDs
python platform/trigger.py --task all  # runs the agent over the demo/ sandbox
```

`setup.sh` creates the agent from `agent.yaml` and a cloud environment, then
prints `export AGENT_ID=... / ENVIRONMENT_ID=...` (progress goes to stderr, so
`eval` captures just the IDs). The manual steps below do the same by hand.

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

## 3. Demo it (no Slack, no repo)

The agent degrades gracefully: with no GitHub/Slack MCP configured it just
analyzes the data and streams its findings back. `trigger.py` embeds a dataset
inline in the kickoff message, so nothing external needs connecting.

```bash
export ANTHROPIC_API_KEY=... AGENT_ID=... ENVIRONMENT_ID=...
python platform/trigger.py --task all          # uses the demo/ sandbox
python platform/trigger.py --task all --data-dir .   # full synthetic repo
```

You'll see the agent's triage table, compliance findings, and the exact file
changes it *would* commit — printed straight from the session stream.

### Raw `curl` trigger (what a webhook would call)

Start a session, send a message, stream the result — no SDK needed:

```bash
SESSION=$(curl -sS https://api.anthropic.com/v1/sessions \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json" \
  -d "{\"agent\":\"$AGENT_ID\",\"environment_id\":\"$ENVIRONMENT_ID\",\"title\":\"ops-demo\"}" | jq -r .id)

curl -sS "https://api.anthropic.com/v1/sessions/$SESSION/events" \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" -H "content-type: application/json" \
  -d '{"events":[{"type":"user.message","content":[{"type":"text","text":"Run the triage task. === FILE: issues/DEMO-1.json ===\n{...paste an issue...}"}]}]}'

curl -sS -N "https://api.anthropic.com/v1/sessions/$SESSION/stream" \
  -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: managed-agents-2026-04-01" -H "Accept: text/event-stream"
```

(`trigger.py` is the convenient version — it builds the payload from files for you.)

## 4. Schedule or webhook

`trigger.py` is the entrypoint either way:

- **Schedule:** run it from cron / a GitHub Actions `schedule:` / an EventBridge
  timer, e.g. `*/15 * * * *  python /app/platform/trigger.py --task all`.
- **Webhook:** call it from a tiny HTTP handler (FastAPI/Lambda) on `POST`.

The schedule/webhook lives in *your* infra; it only needs `ANTHROPIC_API_KEY`,
`AGENT_ID`, and `ENVIRONMENT_ID`.

## 5. Going live (PR + Slack)

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
| **Cross-run memory** (`memory.prior_context_prompt`) | `trigger.py` reuses that exact module and injects the calibration block into the *kickoff message*. State lives in `state/` at the repo root. The platform's native **Memory stores** are the eventual upgrade. |
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

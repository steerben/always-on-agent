# Always-On Ops Agent

An unattended agent (Claude Agent SDK, Python) that runs over the synthetic
enterprise data in this repo and:

- **Incident triage** — reads `issues/`, correlates each with `deploys/recent.json`
  and the `runbooks/`, classifies feature-requests vs. real incidents, assigns a
  severity, and writes a triage note + severity back into the issue JSON.
- **Compliance drift** — checks every contract in `contracts/` against
  `compliance-policy.md` and files a `COMPLIANCE-<vendor>.json` finding per
  contract with violations.

It then **opens a pull request** with its proposed changes (never merged) and
**posts a Slack summary** with the PR link.

## How it stays safe

- The model gets only `Read/Grep/Glob/Write/Edit` plus two custom tools
  (`open_pull_request`, `post_to_slack`). **No `Bash`** — a `PreToolUse` hook
  denies it. So the agent cannot merge, force-push, or run arbitrary commands.
- `DRY_RUN=true` (default): the PR and Slack tools only log what they *would* do.
  File edits still happen in the working tree so you can inspect the diff.

## Setup

```bash
uv sync
cp .env.example .env   # then fill in values
```

Requires the Claude Code CLI on PATH (the SDK uses it) and either
`ANTHROPIC_API_KEY` or a logged-in Claude Code session.

## Run — scheduled trigger (cron)

```bash
uv run python -m agent --task all        # or: triage | compliance
```

Point your platform's scheduler at this command.

## Run — webhook trigger

```bash
uv run uvicorn agent.webhook:app --host 0.0.0.0 --port 8080
curl -X POST localhost:8080/trigger \
  -H "X-Agent-Secret: $WEBHOOK_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"task": "all", "note": "manual run"}'
```

`/trigger` returns `202` immediately and runs the agent in the background;
`/healthz` is a liveness probe.

## Going live

Set `DRY_RUN=false`, `GITHUB_REPO=<owner/name>`, and `SLACK_WEBHOOK_URL=...` in
`.env`. The first real run will open a PR against `PR_BASE_BRANCH` (default `main`).

## Deploy

`docker build -t ops-agent .` builds an image with `git` + `gh` included. The
default `CMD` runs the webhook server; for scheduled runs invoke
`python -m agent --task all` from the platform's cron.

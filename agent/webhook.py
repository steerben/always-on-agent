"""Webhook entrypoint.

Run with:  uvicorn agent.webhook:app --host 0.0.0.0 --port 8080
Trigger with:
    curl -X POST localhost:8080/trigger \
         -H "X-Agent-Secret: $WEBHOOK_SECRET" \
         -H "Content-Type: application/json" \
         -d '{"task": "all", "note": "PROD-4521 paged"}'
"""

from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import settings
from .core import VALID_TASKS, run_agent
from .observability import load_runs, render_dashboard_html
from .slack_interactive import (
    ack_response,
    decide_from_action,
    parse_action_payload,
    verify_slack_signature,
)

logger = logging.getLogger("ops-agent.webhook")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Always-On Ops Agent")


class TriggerRequest(BaseModel):
    task: str = "all"
    note: str | None = None
    # wait=true runs the agent inline and returns its findings in the response
    # (great for demos). wait=false returns 202 and runs in the background.
    wait: bool = False


async def _run_logged(task: str, note: str | None) -> None:
    try:
        summary = await run_agent(task=task, note=note)
        logger.info("Agent run complete (task=%s):\n%s", task, summary)
    except Exception:
        logger.exception("Agent run failed (task=%s)", task)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "dry_run": settings.dry_run}


@app.post("/trigger")
async def trigger(
    req: TriggerRequest,
    background: BackgroundTasks,
    response: Response,
    x_agent_secret: str = Header(default=""),
) -> dict:
    if x_agent_secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="invalid or missing X-Agent-Secret")
    if req.task not in VALID_TASKS:
        raise HTTPException(status_code=400, detail=f"task must be one of {sorted(VALID_TASKS)}")

    if req.wait:
        summary = await run_agent(task=req.task, note=req.note)
        return {"task": req.task, "dry_run": settings.dry_run, "summary": summary}

    background.add_task(_run_logged, req.task, req.note)
    response.status_code = 202
    return {"accepted": True, "task": req.task}


@app.get("/runs.json")
def runs_json() -> list[dict]:
    """Structured history of past agent runs (newest first)."""
    return load_runs()


@app.get("/runs", response_class=HTMLResponse)
def runs_dashboard() -> str:
    """Human-readable dashboard of past agent runs."""
    return render_dashboard_html(load_runs())


@app.post("/slack/actions")
async def slack_actions(
    request: Request,
    x_slack_signature: str = Header(default=""),
    x_slack_request_timestamp: str = Header(default=""),
) -> dict:
    """Handle interactive Slack button clicks (approve / PR-only / dismiss).

    Slack signs the raw request body, so we must verify before parsing.
    """
    body = await request.body()
    if not verify_slack_signature(
        signing_secret=settings.slack_signing_secret,
        timestamp=x_slack_request_timestamp,
        signature=x_slack_signature,
        body=body,
    ):
        raise HTTPException(status_code=401, detail="invalid Slack signature")

    parsed = parse_action_payload(body.decode())
    decision = decide_from_action(parsed)
    logger.info(
        "Slack action %s by %s (token=%s) -> %s",
        parsed.get("action_id"),
        parsed.get("user"),
        parsed.get("action_token"),
        decision.get("decision"),
    )
    return ack_response(decision)

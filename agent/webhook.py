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

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from .config import settings
from .core import VALID_TASKS, run_agent

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

"""Observability for the Always-On Ops Agent.

Self-contained, stdlib-only module that:

- records a structured ``RunReport`` for each agent pass (``RunRecorder``),
  persisting it to ``runs/<run_id>.json``;
- turns the existing triage confidence (confirmed/suspected/none) and severity
  (P0..P3) into an operational escalation policy (``escalation_decision``);
- renders a single self-contained HTML dashboard over saved runs
  (``render_dashboard_html``).

No third-party dependencies and no FastAPI import: the orchestrator wires the
recorder into ``core.run_agent`` and mounts ``load_runs`` / ``render_dashboard_html``
behind ``/runs.json`` and ``/runs`` in ``webhook.py``.
"""

from __future__ import annotations

import html
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Repo root = parent of this package directory (same pattern as agent/config.py).
RUNS_DIR = Path(__file__).resolve().parent.parent / "runs"

# Severities that are considered "high" for escalation purposes.
_HIGH_SEVERITIES = {"P0", "P1"}

# How long an individual tool-arg value may be before it is truncated.
_ARG_VALUE_MAX = 80


def _utc_now_iso() -> str:
    """Current time as an ISO-8601 UTC string (seconds precision, 'Z' suffix)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _summarize_args(args: dict | None) -> str:
    """Render a short, single-line summary of tool args, truncating long values."""
    if not args:
        return ""
    parts: list[str] = []
    for key, value in args.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        text = text.replace("\n", " ")
        if len(text) > _ARG_VALUE_MAX:
            text = text[: _ARG_VALUE_MAX - 1] + "…"  # ellipsis
        parts.append(f"{key}={text}")
    return ", ".join(parts)


@dataclass
class ToolCall:
    name: str
    args_summary: str
    at: str  # ISO-8601 UTC

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Decision:
    issue_id: str
    severity: str | None = None
    confidence: str | None = None  # one of confirmed/suspected/none/None
    kind: str = "incident"  # e.g. "incident"/"compliance"/"not_incident"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunReport:
    run_id: str
    task: str
    model: str
    started_at: str
    ended_at: str | None = None
    duration_s: float | None = None
    num_turns: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    token_usage: dict = field(default_factory=dict)
    final_summary: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "model": self.model,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "num_turns": self.num_turns,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "decisions": [d.to_dict() for d in self.decisions],
            "token_usage": self.token_usage,
            "final_summary": self.final_summary,
            "error": self.error,
        }


class RunRecorder:
    """Collects telemetry over a single agent run and persists a RunReport."""

    def __init__(self, task: str, model: str) -> None:
        self.task = task
        self.model = model
        self.run_id = self._new_run_id()
        self.started_at = _utc_now_iso()
        self._start_monotonic = time.monotonic()
        self.num_turns = 0
        self.tool_calls: list[ToolCall] = []
        self.decisions: list[Decision] = []
        self.token_usage: dict = {}

    @staticmethod
    def _new_run_id() -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{stamp}-{secrets.token_hex(3)}"

    def record_tool_call(self, name: str, args: dict | None = None) -> None:
        self.tool_calls.append(
            ToolCall(name=name, args_summary=_summarize_args(args), at=_utc_now_iso())
        )

    def record_decision(
        self,
        issue_id: str,
        severity: str | None = None,
        confidence: str | None = None,
        kind: str = "incident",
    ) -> None:
        self.decisions.append(
            Decision(issue_id=issue_id, severity=severity, confidence=confidence, kind=kind)
        )

    def record_usage(self, usage: dict) -> None:
        if usage:
            self.token_usage = dict(usage)

    def set_turns(self, n: int) -> None:
        self.num_turns = n

    def finish(self, final_summary: str, error: str | None = None) -> RunReport:
        ended_at = _utc_now_iso()
        duration_s = round(time.monotonic() - self._start_monotonic, 3)
        return RunReport(
            run_id=self.run_id,
            task=self.task,
            model=self.model,
            started_at=self.started_at,
            ended_at=ended_at,
            duration_s=duration_s,
            num_turns=self.num_turns,
            tool_calls=list(self.tool_calls),
            decisions=list(self.decisions),
            token_usage=dict(self.token_usage),
            final_summary=final_summary,
            error=error,
        )

    def save(self, report: RunReport, *, runs_dir: Path | None = None) -> Path:
        target_dir = runs_dir or RUNS_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{report.run_id}.json"
        path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        return path


def escalation_decision(decisions: list[Decision]) -> dict:
    """Confidence-gated escalation policy.

    - any high-severity (P0/P1) decision with confidence == "confirmed" -> page
    - high-severity present but only suspected/none confidence    -> pr_and_warn
    - otherwise                                                    -> pr_only
    """
    confirmed_high: list[Decision] = []
    unconfirmed_high: list[Decision] = []
    for d in decisions:
        if d.severity in _HIGH_SEVERITIES:
            if d.confidence == "confirmed":
                confirmed_high.append(d)
            else:
                unconfirmed_high.append(d)

    if confirmed_high:
        reasons = [
            f"{d.issue_id}: {d.severity} with confirmed correlation — page on-call"
            for d in confirmed_high
        ]
        return {"action": "page", "reasons": reasons}

    if unconfirmed_high:
        reasons = [
            f"{d.issue_id}: {d.severity} but correlation {d.confidence or 'none'} "
            "— open PR and warn, do not page"
            for d in unconfirmed_high
        ]
        return {"action": "pr_and_warn", "reasons": reasons}

    reasons = ["No P0/P1 decisions — open PR only"]
    return {"action": "pr_only", "reasons": reasons}


def load_runs(*, runs_dir: Path | None = None) -> list[dict]:
    """Load all saved run reports, newest first. Returns [] if none."""
    source_dir = runs_dir or RUNS_DIR
    if not source_dir.exists():
        return []
    reports: list[dict] = []
    for path in source_dir.glob("*.json"):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    reports.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return reports


def _esc(text: object) -> str:
    """HTML-escape any value (incl. quotes) for safe inline rendering."""
    return html.escape("" if text is None else str(text), quote=True)


def render_dashboard_html(runs: list[dict]) -> str:
    """Render a single self-contained HTML dashboard over the given runs."""
    cards: list[str] = []
    for run in runs:
        decisions = run.get("decisions") or []
        tool_calls = run.get("tool_calls") or []
        # Reconstruct Decision objects to reuse the escalation policy.
        decision_objs = [
            Decision(
                issue_id=str(d.get("issue_id", "")),
                severity=d.get("severity"),
                confidence=d.get("confidence"),
                kind=d.get("kind", "incident"),
            )
            for d in decisions
        ]
        escalation = escalation_decision(decision_objs)
        usage = run.get("token_usage") or {}

        decision_rows = "".join(
            f"<li><code>{_esc(d.get('issue_id'))}</code> "
            f"<span class=\"sev sev-{_esc(d.get('severity') or 'none')}\">"
            f"{_esc(d.get('severity') or '—')}</span> "
            f"confidence={_esc(d.get('confidence') or 'none')} "
            f"kind={_esc(d.get('kind') or 'incident')}</li>"
            for d in decisions
        ) or "<li><em>no decisions</em></li>"

        tool_rows = "".join(
            f"<li><code>{_esc(tc.get('name'))}</code> "
            f"<span class=\"args\">{_esc(tc.get('args_summary'))}</span> "
            f"<span class=\"at\">{_esc(tc.get('at'))}</span></li>"
            for tc in tool_calls
        ) or "<li><em>no tool calls</em></li>"

        usage_text = ", ".join(f"{_esc(k)}={_esc(v)}" for k, v in usage.items()) or "—"

        error_html = (
            f"<p class=\"error\">error: {_esc(run.get('error'))}</p>"
            if run.get("error")
            else ""
        )

        cards.append(
            f"""
        <article class="run">
          <header>
            <h2>{_esc(run.get('task'))} <span class="rid">{_esc(run.get('run_id'))}</span></h2>
            <span class="action action-{_esc(escalation['action'])}">{_esc(escalation['action'])}</span>
          </header>
          <dl class="meta">
            <div><dt>started</dt><dd>{_esc(run.get('started_at'))}</dd></div>
            <div><dt>duration</dt><dd>{_esc(run.get('duration_s'))}s</dd></div>
            <div><dt>turns</dt><dd>{_esc(run.get('num_turns'))}</dd></div>
            <div><dt>decisions</dt><dd>{len(decisions)}</dd></div>
            <div><dt>tokens</dt><dd>{usage_text}</dd></div>
          </dl>
          {error_html}
          <p class="summary">{_esc(run.get('final_summary'))}</p>
          <details>
            <summary>Decisions ({len(decisions)})</summary>
            <ul class="decisions">{decision_rows}</ul>
          </details>
          <details>
            <summary>Tool calls ({len(tool_calls)})</summary>
            <ul class="tools">{tool_rows}</ul>
          </details>
          <ul class="reasons">{"".join(f"<li>{_esc(r)}</li>" for r in escalation['reasons'])}</ul>
        </article>"""
        )

    body = "\n".join(cards) if cards else "<p class=\"empty\">No runs recorded yet.</p>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ops Agent — Run Dashboard</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; margin: 0; padding: 1.5rem;
            background: #0f1419; color: #e6e6e6; }}
    h1 {{ font-size: 1.4rem; margin: 0 0 1rem; }}
    .run {{ background: #1a212b; border: 1px solid #2a3340; border-radius: 10px;
            padding: 1rem 1.2rem; margin-bottom: 1rem; }}
    .run header {{ display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; }}
    .run h2 {{ font-size: 1.05rem; margin: 0; }}
    .rid {{ font-weight: 400; color: #8a96a3; font-size: .8rem; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 1rem; margin: .6rem 0; }}
    .meta div {{ margin: 0; }}
    .meta dt {{ font-size: .7rem; text-transform: uppercase; color: #8a96a3; }}
    .meta dd {{ margin: 0; font-variant-numeric: tabular-nums; }}
    .summary {{ white-space: pre-wrap; color: #c4ccd4; }}
    .error {{ color: #ff6b6b; font-weight: 600; }}
    .action {{ font-size: .75rem; padding: .15rem .5rem; border-radius: 999px; font-weight: 600; }}
    .action-page {{ background: #5c1a1a; color: #ffb4b4; }}
    .action-pr_and_warn {{ background: #5c4a1a; color: #ffe08a; }}
    .action-pr_only {{ background: #1a3a2a; color: #8affc4; }}
    .sev {{ font-size: .7rem; padding: 0 .35rem; border-radius: 4px; background: #2a3340; }}
    code {{ background: #11161d; padding: 0 .25rem; border-radius: 3px; }}
    .args {{ color: #8a96a3; }}
    .at {{ color: #5c6773; font-size: .75rem; }}
    .reasons {{ color: #8a96a3; font-size: .85rem; }}
    details {{ margin: .4rem 0; }}
    summary {{ cursor: pointer; color: #9fb3c8; }}
    ul {{ margin: .3rem 0; }}
    .empty {{ color: #8a96a3; }}
  </style>
</head>
<body>
  <h1>Always-On Ops Agent — Run Dashboard ({len(runs)})</h1>
  {body}
</body>
</html>
"""

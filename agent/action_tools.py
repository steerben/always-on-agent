"""Action-loop tools: read live-ish metrics and propose deploy rollbacks.

These extend the agent's loop without giving it any new way to execute
something destructive. `query_metrics` is strictly read-only. `propose_rollback`
never runs git or a deploy: it writes a structured request file under
`rollbacks/` so the request flows through the existing open_pull_request flow
for human review. Both honour DRY_RUN.

The real logic lives in plain `_*_impl` helpers so it is unit-testable without
the SDK runtime; the `@tool` coroutines are thin wrappers around them.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import tool

from .config import settings


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


_VALID_METRICS = ("error_rate", "p99_latency_ms", "db_pool_idle")

# Fallback thresholds if the metrics file omits a "thresholds" block. Each is
# (comparison, value): "above" means a breach when value exceeds the threshold,
# "below" means a breach when value drops under it.
_DEFAULT_THRESHOLDS = {
    "error_rate": ("above", 0.05),
    "p99_latency_ms": ("above", 2000),
    "db_pool_idle": ("below", 2),
}


def _parse_at(at: str) -> datetime:
    """Parse an ISO-8601 timestamp (accepting a trailing 'Z') as UTC-aware."""
    return datetime.fromisoformat(at.replace("Z", "+00:00"))


# --------------------------------------------------------------------------- #
# query_metrics
# --------------------------------------------------------------------------- #
def _query_metrics_impl(args: dict, repo_dir: Path) -> str:
    service = args["service"]
    metric = args["metric"]
    window_minutes = int(args.get("window_minutes", 60))

    if metric not in _VALID_METRICS:
        return (
            f"Unknown metric {metric!r}. "
            f"Valid metrics: {', '.join(_VALID_METRICS)}."
        )

    path = repo_dir / "metrics" / "recent.json"
    if not path.exists():
        return f"No metrics file found at {path}; cannot query metrics."

    data = json.loads(path.read_text())
    series = data.get("series", {})

    if service not in series:
        known = ", ".join(sorted(series)) or "(none)"
        return f"Unknown service {service!r}. Known services: {known}."

    if metric not in series[service]:
        known = ", ".join(sorted(series[service])) or "(none)"
        return (
            f"No {metric!r} data for service {service!r}. "
            f"Available metrics for it: {known}."
        )

    points = series[service][metric]
    if not points:
        return f"No data points for {service}/{metric}."

    # Window the series relative to the most recent sample.
    points = sorted(points, key=lambda p: _parse_at(p["at"]))
    latest_at = _parse_at(points[-1]["at"])
    cutoff = latest_at.timestamp() - window_minutes * 60
    windowed = [p for p in points if _parse_at(p["at"]).timestamp() >= cutoff]
    if not windowed:
        windowed = [points[-1]]

    values = [p["value"] for p in windowed]
    latest = values[-1]
    vmin, vmax = min(values), max(values)
    avg = sum(values) / len(values)

    # Threshold check.
    thresholds = data.get("thresholds", {})
    if metric in thresholds:
        comparison = thresholds[metric].get("comparison")
        threshold = thresholds[metric].get("value")
    else:
        comparison, threshold = _DEFAULT_THRESHOLDS[metric]

    if comparison == "above":
        breaching = latest > threshold
        rel = ">"
    else:  # "below"
        breaching = latest < threshold
        rel = "<"
    verdict = (
        f"BREACHING threshold (latest {latest} {rel} {threshold})"
        if breaching
        else f"within threshold (latest {latest}, limit {threshold})"
    )

    return (
        f"{service} / {metric} over last {window_minutes}m "
        f"({len(windowed)} samples, latest at {points[-1]['at']}):\n"
        f"  latest = {latest}\n"
        f"  min = {vmin}, max = {vmax}, avg = {round(avg, 4)}\n"
        f"  status: {verdict}"
    )


@tool(
    "query_metrics",
    "Read recent live-ish metrics for a service to confirm an incident is still "
    "firing before paging. Read-only; never changes anything.",
    {"service": str, "metric": str, "window_minutes": int},
)
async def query_metrics(args: dict) -> dict:
    return _ok(_query_metrics_impl(args, settings.resolved_repo_dir()))


# --------------------------------------------------------------------------- #
# propose_rollback
# --------------------------------------------------------------------------- #
def _propose_rollback_impl(args: dict, repo_dir: Path, dry_run: bool) -> str:
    service = args["service"]
    from_version = args["from_version"]
    to_version = args["to_version"]
    reason = args["reason"]

    warnings: list[str] = []

    # Validate against the deploy log (advisory only; never hard-fails).
    deploys_path = repo_dir / "deploys" / "recent.json"
    if not deploys_path.exists():
        warnings.append(f"deploy log not found at {deploys_path}")
    else:
        deploys = json.loads(deploys_path.read_text()).get("deploys", [])
        latest = next((d for d in deploys if d.get("service") == service), None)
        if latest is None:
            warnings.append(f"no deploy record found for service {service!r}")
        else:
            if not latest.get("rollback_available", False):
                warnings.append(
                    f"latest deploy for {service} has rollback_available=false; "
                    "a human must confirm this rollback is possible"
                )
            lkg = latest.get("last_known_good")
            if lkg and lkg != to_version:
                warnings.append(
                    f"to_version {to_version!r} does not match last_known_good "
                    f"{lkg!r} from the deploy log"
                )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    request = {
        "kind": "rollback_request",
        "service": service,
        "from_version": from_version,
        "to_version": to_version,
        "reason": reason,
        "requested_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "warnings": warnings,
        "status": "pending_human_review",
    }
    out_path = repo_dir / "rollbacks" / f"{service}-{timestamp}.json"

    warn_text = ("\n  warnings:\n" + "\n".join(f"    - {w}" for w in warnings)) if warnings else ""

    if dry_run:
        return (
            f"[DRY_RUN] would write rollback request to {out_path}\n"
            f"  service: {service}\n"
            f"  from:    {from_version}\n"
            f"  to:      {to_version}\n"
            f"  reason:  {reason}{warn_text}\n"
            "Set DRY_RUN=false to write the request file for human review."
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(request, indent=2) + "\n")
    return (
        f"Wrote rollback request (pending human review; NOT executed) to {out_path}\n"
        f"  service: {service}\n"
        f"  from:    {from_version}\n"
        f"  to:      {to_version}\n"
        f"  reason:  {reason}{warn_text}\n"
        "Open a PR with open_pull_request so a human can approve it."
    )


@tool(
    "propose_rollback",
    "Propose a deploy rollback by writing a structured request file for human "
    "review; never executes the rollback, runs git, or runs a deploy.",
    {"service": str, "from_version": str, "to_version": str, "reason": str},
)
async def propose_rollback(args: dict) -> dict:
    return _ok(
        _propose_rollback_impl(args, settings.resolved_repo_dir(), settings.dry_run)
    )


ACTION_TOOLS = [query_metrics, propose_rollback]
ACTION_TOOL_NAMES = ["mcp__ops__query_metrics", "mcp__ops__propose_rollback"]

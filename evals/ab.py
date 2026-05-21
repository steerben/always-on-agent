"""A/B comparison scaffold for the eval harness.

Run several agent configurations (e.g. different models or task scopes), score
each against the same golden ground truth, and produce a comparison table.

The live execution reuses run_eval.run_live_pass and degrades gracefully when
no API key is available; the scoring, aggregation, and table-formatting code is
fully real and exercised by the tests.

    from evals.ab import compare, format_comparison
    result = compare(
        [
            {"label": "opus", "model": "claude-opus-4", "task": "all"},
            {"label": "sonnet", "model": "claude-sonnet-4", "task": "all"},
        ],
        repo_dir=repo_dir,
        golden_path=golden_path,
    )
    print(format_comparison(result))
"""

from __future__ import annotations

import os
from pathlib import Path

from .score import score_run


def _run_config_live(config: dict) -> tuple[bool, str]:
    """Best-effort live run for one config. Never raises.

    Sets AGENT_MODEL in the environment if the config names a model, then calls
    the shared live helper. Restores the previous env afterwards.
    """
    from .run_eval import run_live_pass

    prev = os.environ.get("AGENT_MODEL")
    model = config.get("model")
    try:
        if model:
            os.environ["AGENT_MODEL"] = str(model)
        return run_live_pass(task=config.get("task", "all"), note=config.get("label"))
    finally:
        if model:
            if prev is None:
                os.environ.pop("AGENT_MODEL", None)
            else:
                os.environ["AGENT_MODEL"] = prev


def compare(configs: list[dict], *, repo_dir: Path, golden_path: Path,
            live: bool = True) -> dict:
    """Run + score each config; return a structured comparison result.

    Each config: {"label": str, "model": str, "task": "triage|compliance|all"}.
    With live=True, attempts a live agent pass per config (degrading gracefully).
    With live=False, just scores the repo's current state once per config (useful
    for tests and for diffing two already-produced repos).
    """
    repo_dir = Path(repo_dir)
    golden_path = Path(golden_path)
    rows: list[dict] = []

    for config in configs:
        label = config.get("label") or config.get("model") or "config"
        live_ok, live_message = (False, "live run skipped (live=False)")
        if live:
            live_ok, live_message = _run_config_live(config)

        card = score_run(repo_dir, golden_path)
        rows.append(
            {
                "label": label,
                "model": config.get("model"),
                "task": config.get("task", "all"),
                "live_ran": live_ok,
                "live_message": live_message,
                "overall_score": card["overall_score"],
                "triage_score": card["triage"]["metrics"]["score"],
                "compliance_score": card["compliance"]["metrics"]["score"],
                "card": card,
            }
        )

    best = max(rows, key=lambda r: r["overall_score"]) if rows else None
    return {
        "rows": rows,
        "best_label": best["label"] if best else None,
        "best_score": best["overall_score"] if best else None,
    }


def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def format_comparison(result: dict) -> str:
    rows = result.get("rows", [])
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("A/B Comparison — Always-On Ops Agent")
    lines.append("=" * 72)
    lines.append(
        f"{'label':<16}{'task':<12}{'live':<6}{'triage':>9}{'compliance':>12}{'overall':>10}"
    )
    lines.append("-" * 72)
    for r in rows:
        lines.append(
            f"{str(r['label']):<16}{str(r['task']):<12}"
            f"{('yes' if r['live_ran'] else 'no'):<6}"
            f"{_pct(r['triage_score']):>9}{_pct(r['compliance_score']):>12}"
            f"{_pct(r['overall_score']):>10}"
        )
    lines.append("-" * 72)
    if result.get("best_label") is not None:
        lines.append(
            f"Best: {result['best_label']} ({_pct(result['best_score'])} overall)"
        )
    lines.append("=" * 72)
    return "\n".join(lines)

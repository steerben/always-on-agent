"""Unit tests for the action-loop tool helpers.

These exercise the `_*_impl` functions directly with tmp_path, so no SDK
runtime is required.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.action_tools import _propose_rollback_impl, _query_metrics_impl


def _write_metrics(repo_dir: Path) -> None:
    (repo_dir / "metrics").mkdir(parents=True, exist_ok=True)
    data = {
        "thresholds": {
            "error_rate": {"comparison": "above", "value": 0.05},
            "p99_latency_ms": {"comparison": "above", "value": 2000},
            "db_pool_idle": {"comparison": "below", "value": 2},
        },
        "series": {
            "payment-service": {
                "error_rate": [
                    {"at": "2026-05-19T14:00:00Z", "value": 0.006},
                    {"at": "2026-05-19T14:05:00Z", "value": 0.121},
                    {"at": "2026-05-19T14:10:00Z", "value": 0.196},
                ],
            },
            "auth-service": {
                "db_pool_idle": [
                    {"at": "2026-05-19T14:00:00Z", "value": 6},
                    {"at": "2026-05-19T14:05:00Z", "value": 0},
                    {"at": "2026-05-19T14:10:00Z", "value": 13},
                ],
            },
        },
    }
    (repo_dir / "metrics" / "recent.json").write_text(json.dumps(data))


def _write_deploys(repo_dir: Path, *, rollback_available: bool = True) -> None:
    (repo_dir / "deploys").mkdir(parents=True, exist_ok=True)
    data = {
        "deploys": [
            {
                "service": "payment-service",
                "version": "v4.8.2",
                "rollback_available": rollback_available,
                "last_known_good": "v4.8.1",
            }
        ]
    }
    (repo_dir / "deploys" / "recent.json").write_text(json.dumps(data))


# --------------------------------------------------------------------------- #
# query_metrics
# --------------------------------------------------------------------------- #
def test_query_metrics_known_service_includes_latest(tmp_path):
    _write_metrics(tmp_path)
    out = _query_metrics_impl(
        {"service": "payment-service", "metric": "error_rate", "window_minutes": 60},
        tmp_path,
    )
    assert "0.196" in out  # latest value present
    assert "payment-service" in out
    assert "BREACHING" in out  # 0.196 > 0.05


def test_query_metrics_below_threshold_breach(tmp_path):
    _write_metrics(tmp_path)
    out = _query_metrics_impl(
        {"service": "auth-service", "metric": "db_pool_idle", "window_minutes": 15},
        tmp_path,
    )
    # latest value (13) is within threshold; min over window hit 0.
    assert "min = 0" in out
    assert "within threshold" in out


def test_query_metrics_unknown_service_is_graceful(tmp_path):
    _write_metrics(tmp_path)
    out = _query_metrics_impl(
        {"service": "does-not-exist", "metric": "error_rate"}, tmp_path
    )
    assert "Unknown service" in out


def test_query_metrics_unknown_metric_is_graceful(tmp_path):
    _write_metrics(tmp_path)
    out = _query_metrics_impl(
        {"service": "payment-service", "metric": "cpu_usage"}, tmp_path
    )
    assert "Unknown metric" in out


# --------------------------------------------------------------------------- #
# propose_rollback
# --------------------------------------------------------------------------- #
def test_propose_rollback_dry_run_writes_no_file(tmp_path):
    _write_deploys(tmp_path)
    out = _propose_rollback_impl(
        {
            "service": "payment-service",
            "from_version": "v4.8.2",
            "to_version": "v4.8.1",
            "reason": "NPE spike at checkout",
        },
        tmp_path,
        dry_run=True,
    )
    assert "[DRY_RUN]" in out
    assert not (tmp_path / "rollbacks").exists()


def test_propose_rollback_writes_file_with_details(tmp_path):
    _write_deploys(tmp_path)
    out = _propose_rollback_impl(
        {
            "service": "payment-service",
            "from_version": "v4.8.2",
            "to_version": "v4.8.1",
            "reason": "NPE spike at checkout",
        },
        tmp_path,
        dry_run=False,
    )
    files = list((tmp_path / "rollbacks").glob("*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text())
    assert written["service"] == "payment-service"
    assert written["from_version"] == "v4.8.2"
    assert written["to_version"] == "v4.8.1"
    assert written["reason"] == "NPE spike at checkout"
    assert written["warnings"] == []
    assert "Wrote rollback request" in out


def test_propose_rollback_warns_when_rollback_unavailable(tmp_path):
    _write_deploys(tmp_path, rollback_available=False)
    out = _propose_rollback_impl(
        {
            "service": "payment-service",
            "from_version": "v4.8.2",
            "to_version": "v4.8.1",
            "reason": "test",
        },
        tmp_path,
        dry_run=False,
    )
    assert "rollback_available=false" in out
    files = list((tmp_path / "rollbacks").glob("*.json"))
    written = json.loads(files[0].read_text())
    assert any("rollback_available=false" in w for w in written["warnings"])


def test_propose_rollback_warns_on_version_mismatch(tmp_path):
    _write_deploys(tmp_path)
    out = _propose_rollback_impl(
        {
            "service": "payment-service",
            "from_version": "v4.8.2",
            "to_version": "v4.7.0",  # != last_known_good v4.8.1
            "reason": "test",
        },
        tmp_path,
        dry_run=False,
    )
    assert "last_known_good" in out

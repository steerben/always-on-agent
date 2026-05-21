"""Tests for the observability module."""

from __future__ import annotations

import time

from agent.observability import (
    Decision,
    RunRecorder,
    escalation_decision,
    load_runs,
    render_dashboard_html,
)


def test_run_recorder_lifecycle_produces_full_report():
    rec = RunRecorder(task="triage", model="claude-opus-4-7")
    rec.set_turns(5)
    rec.record_tool_call("Read", {"file_path": "issues/PROD-4521.json"})
    rec.record_tool_call("open_pull_request", {"title": "x" * 200})
    rec.record_decision("PROD-4521", severity="P0", confidence="confirmed", kind="incident")
    rec.record_usage({"input_tokens": 1200, "output_tokens": 800})
    time.sleep(0.01)  # ensure a positive measurable duration

    report = rec.finish("done triaging")
    d = report.to_dict()

    expected_keys = {
        "run_id", "task", "model", "started_at", "ended_at", "duration_s",
        "num_turns", "tool_calls", "decisions", "token_usage", "final_summary", "error",
    }
    assert set(d.keys()) == expected_keys
    assert d["task"] == "triage"
    assert d["num_turns"] == 5
    assert d["duration_s"] > 0
    assert d["error"] is None
    assert len(d["tool_calls"]) == 2
    assert len(d["decisions"]) == 1
    assert d["token_usage"]["input_tokens"] == 1200
    # long arg values are truncated
    assert len(d["tool_calls"][1]["args_summary"]) < 200


def test_save_and_load_round_trip_newest_first(tmp_path):
    first = RunRecorder(task="triage", model="m")
    r1 = first.finish("first")
    first.save(r1, runs_dir=tmp_path)

    time.sleep(1.05)  # run_id stamp has second precision; ensure ordering differs

    second = RunRecorder(task="compliance", model="m")
    r2 = second.finish("second")
    second.save(r2, runs_dir=tmp_path)

    loaded = load_runs(runs_dir=tmp_path)
    assert len(loaded) == 2
    assert loaded[0]["run_id"] == r2.run_id  # newest first
    assert loaded[1]["run_id"] == r1.run_id
    assert loaded[0]["final_summary"] == "second"


def test_load_runs_empty(tmp_path):
    assert load_runs(runs_dir=tmp_path / "missing") == []


def test_escalation_policy_truth_table():
    page = escalation_decision([Decision("A", severity="P0", confidence="confirmed")])
    assert page["action"] == "page"
    assert any("A" in r for r in page["reasons"])

    warn = escalation_decision([Decision("B", severity="P1", confidence="suspected")])
    assert warn["action"] == "pr_and_warn"

    pr_only = escalation_decision([Decision("C", severity="P3", confidence="none")])
    assert pr_only["action"] == "pr_only"

    assert escalation_decision([])["action"] == "pr_only"


def test_render_dashboard_html_escapes_and_includes_task():
    rec = RunRecorder(task="triage", model="m")
    rec.record_decision(
        "PROD-XSS<script>alert(1)</script>", severity="P0", confidence="confirmed"
    )
    report = rec.finish("summary with <script>alert('x')</script>")
    html_out = render_dashboard_html([report.to_dict()])

    assert "<html" in html_out
    assert "triage" in html_out
    # the injected script tag must not appear unescaped anywhere
    assert "<script>alert" not in html_out
    assert "&lt;script&gt;" in html_out


def test_render_dashboard_html_empty():
    out = render_dashboard_html([])
    assert "<html" in out
    assert "No runs recorded" in out

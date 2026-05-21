"""Tests for the deterministic eval-harness scoring.

Run from repo root:
    uv run --with pytest pytest tests/test_evals.py -q

These feed synthetic *produced* data (never the live model) to the scorers and
assert the scoring math directly.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.ab import compare, format_comparison
from evals.score import (
    format_report_card,
    score_compliance,
    score_run,
    score_triage,
)

# --------------------------------------------------------------------------- #
# fixtures (synthetic golden + synthetic produced data)
# --------------------------------------------------------------------------- #
GOLDEN = {
    "triage": {
        "PROD-1": {
            "is_incident": True,
            "expected_severity": "P0",
            "expected_correlation_confidence": "confirmed",
            "classification_keywords": ["payment", "checkout"],
        },
        "PROD-2": {
            "is_incident": True,
            "expected_severity": "P2",
            "expected_correlation_confidence": "none",
            "classification_keywords": ["login", "502"],
        },
        "PROD-3": {
            "is_incident": False,
            "expected_severity": None,
            "expected_correlation_confidence": "none",
            "classification_keywords": ["feature request"],
        },
    },
    "compliance": {
        "acme": {
            "clean": False,
            "expected_violations": ["data_residency", "breach_notification"],
        },
        "globex": {"clean": True, "expected_violations": []},
        "sirius": {
            "clean": False,
            "expected_violations": ["liability_cap", "subprocessors"],
        },
    },
}


def _agent_comment(text: str) -> dict:
    return {"author": "always-on-ops-agent", "body": text}


def _perfect_issues() -> dict:
    return {
        "PROD-1": {
            "id": "PROD-1",
            "severity": "P0",
            "comments": [_agent_comment(
                "Real incident. Payment NPE at checkout. "
                "Correlation: confirmed against payment-service deploy."
            )],
        },
        "PROD-2": {
            "id": "PROD-2",
            "severity": "P2",
            "comments": [_agent_comment(
                "Login 502 windows. Correlation: none (deploy is after the issue)."
            )],
        },
        "PROD-3": {
            "id": "PROD-3",
            "severity": None,
            "comments": [_agent_comment(
                "This is a feature request, not an incident. No action / backlog."
            )],
        },
    }


def _perfect_findings() -> dict:
    return {
        "acme": {
            "id": "COMPLIANCE-acme",
            "title": "Compliance violations — Acme",
            "body": (
                "VIOLATION data residency: EU data may leave the EU. "
                "VIOLATION breach notification: 96h window exceeds 72h."
            ),
            "comments": [],
        },
        # globex is clean -> intentionally absent (no finding filed).
        "sirius": {
            "id": "COMPLIANCE-sirius",
            "title": "Compliance violations — Sirius",
            "body": (
                "VIOLATION liability cap: 3 months < 12 months. "
                "VIOLATION subprocessors: no prior notice."
            ),
            "comments": [],
        },
    }


# --------------------------------------------------------------------------- #
# triage scoring
# --------------------------------------------------------------------------- #
def test_triage_perfect_scores_one():
    res = score_triage(_perfect_issues(), GOLDEN)
    m = res["metrics"]
    assert m["severity_accuracy"] == 1.0
    assert m["is_incident_accuracy"] == 1.0
    assert m["correlation_confidence_accuracy"] == 1.0
    assert m["classification_keyword_hit_rate"] == 1.0
    assert m["score"] == 1.0


def test_triage_wrong_severity_drops_accuracy():
    issues = _perfect_issues()
    issues["PROD-1"]["severity"] = "P2"  # was P0
    res = score_triage(issues, GOLDEN)
    # 1 of 3 severities now wrong -> 2/3.
    assert res["metrics"]["severity_accuracy"] == 2 / 3
    assert res["per_issue"]["PROD-1"]["severity_correct"] is False
    # Other metrics untouched.
    assert res["metrics"]["is_incident_accuracy"] == 1.0
    assert res["metrics"]["correlation_confidence_accuracy"] == 1.0


def test_triage_feature_request_detected_as_non_incident():
    res = score_triage(_perfect_issues(), GOLDEN)
    assert res["per_issue"]["PROD-3"]["got_is_incident"] is False
    assert res["per_issue"]["PROD-3"]["is_incident_correct"] is True


def test_triage_confidence_levels_detected():
    res = score_triage(_perfect_issues(), GOLDEN)
    assert res["per_issue"]["PROD-1"]["got_correlation_confidence"] == "confirmed"
    assert res["per_issue"]["PROD-2"]["got_correlation_confidence"] == "none"


def test_triage_missing_issue_scores_zero_for_that_issue():
    issues = _perfect_issues()
    del issues["PROD-2"]
    res = score_triage(issues, GOLDEN)
    # PROD-2 absent: severity None != P2, confidence None != none.
    assert res["per_issue"]["PROD-2"]["present"] is False
    assert res["per_issue"]["PROD-2"]["severity_correct"] is False
    assert res["metrics"]["severity_accuracy"] == 2 / 3


# --------------------------------------------------------------------------- #
# compliance scoring
# --------------------------------------------------------------------------- #
def test_compliance_perfect_scores_one():
    res = score_compliance(_perfect_findings(), GOLDEN)
    m = res["metrics"]
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["f1"] == 1.0
    assert m["clean_accuracy"] == 1.0
    assert m["score"] == 1.0


def test_compliance_missed_violation_drops_recall():
    findings = _perfect_findings()
    # Drop the breach_notification violation from acme's body (miss 1 of 2).
    findings["acme"]["body"] = "VIOLATION data residency: EU data may leave the EU."
    res = score_compliance(findings, GOLDEN)
    acme = res["per_vendor"]["acme"]
    assert acme["detected_violations"] == ["data_residency"]
    assert acme["recall"] == 0.5  # 1 of 2 expected found
    assert acme["precision"] == 1.0  # the one found is correct
    # Macro recall across 3 vendors: acme 0.5, globex 1.0 (no expected), sirius 1.0.
    assert res["metrics"]["recall"] == (0.5 + 1.0 + 1.0) / 3


def test_compliance_clean_vendor_with_no_finding_is_correct():
    res = score_compliance(_perfect_findings(), GOLDEN)
    globex = res["per_vendor"]["globex"]
    assert globex["filed_finding"] is False
    assert globex["clean_handling_correct"] is True
    assert res["metrics"]["clean_accuracy"] == 1.0


def test_compliance_clean_vendor_wrongly_filed_is_penalized():
    findings = _perfect_findings()
    findings["globex"] = {
        "id": "COMPLIANCE-globex",
        "title": "Compliance violations — Globex",
        "body": "VIOLATION audit rights: too much notice.",
        "comments": [],
    }
    res = score_compliance(findings, GOLDEN)
    globex = res["per_vendor"]["globex"]
    assert globex["filed_finding"] is True
    assert globex["clean_handling_correct"] is False
    # Only globex's clean handling is scored (the one clean vendor) -> 0.0.
    assert res["metrics"]["clean_accuracy"] == 0.0
    # Spurious detection on a no-expected vendor tanks precision for globex.
    assert globex["precision"] == 0.0
    assert globex["false_positives"] == 1


# --------------------------------------------------------------------------- #
# whole-run scoring from a tmp repo layout
# --------------------------------------------------------------------------- #
def _write_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    issues = repo / "issues"
    evals = repo / "evals"
    issues.mkdir(parents=True)
    evals.mkdir(parents=True)

    golden = {
        "triage": {
            "PROD-1": {
                "is_incident": True,
                "expected_severity": "P0",
                "expected_correlation_confidence": "confirmed",
                "classification_keywords": ["payment"],
            },
            "PROD-9": {
                "is_incident": False,
                "expected_severity": None,
                "expected_correlation_confidence": "none",
                "classification_keywords": ["feature request"],
            },
        },
        "compliance": {
            "acme": {"clean": False, "expected_violations": ["data_residency"]},
            "globex": {"clean": True, "expected_violations": []},
        },
    }
    golden_path = evals / "golden.json"
    golden_path.write_text(json.dumps(golden), encoding="utf-8")

    (issues / "PROD-1.json").write_text(json.dumps({
        "id": "PROD-1",
        "severity": "P0",
        "comments": [_agent_comment("Payment incident. Correlation: confirmed.")],
    }), encoding="utf-8")
    (issues / "PROD-9.json").write_text(json.dumps({
        "id": "PROD-9",
        "severity": None,
        "comments": [_agent_comment("Feature request, not an incident. Backlog.")],
    }), encoding="utf-8")
    (issues / "COMPLIANCE-acme.json").write_text(json.dumps({
        "id": "COMPLIANCE-acme",
        "title": "Acme",
        "body": "VIOLATION data residency.",
        "comments": [],
    }), encoding="utf-8")
    # globex clean -> no finding file.
    return repo, golden_path


def test_score_run_reads_tmp_repo_and_builds_card(tmp_path):
    repo, golden_path = _write_repo(tmp_path)
    card = score_run(repo, golden_path)

    assert set(card.keys()) >= {"triage", "compliance", "overall_score"}
    # Everything in this fixture is correct -> perfect scores.
    assert card["triage"]["metrics"]["score"] == 1.0
    assert card["compliance"]["metrics"]["score"] == 1.0
    assert card["overall_score"] == 1.0

    # The COMPLIANCE-* file is routed to findings, not triage.
    assert "acme" in card["compliance"]["per_vendor"]
    assert "PROD-1" in card["triage"]["per_issue"]

    # Report card renders to a non-trivial string.
    text = format_report_card(card)
    assert "OVERALL SCORE" in text
    assert "PROD-1" in text


def test_score_run_handles_missing_issues_dir(tmp_path):
    # golden present, but no issues/ directory -> graceful low score, no crash.
    evals = tmp_path / "evals"
    evals.mkdir()
    golden_path = evals / "golden.json"
    golden_path.write_text(json.dumps(GOLDEN), encoding="utf-8")
    card = score_run(tmp_path, golden_path)
    assert card["overall_score"] >= 0.0
    # No findings filed at all -> the clean vendor (globex) is handled correctly.
    assert card["compliance"]["per_vendor"]["globex"]["clean_handling_correct"] is True


# --------------------------------------------------------------------------- #
# A/B scaffold (scoring/aggregation/table are real and tested)
# --------------------------------------------------------------------------- #
def test_ab_compare_aggregates_and_picks_best(tmp_path):
    repo, golden_path = _write_repo(tmp_path)
    result = compare(
        [
            {"label": "cfg-a", "model": "model-a", "task": "all"},
            {"label": "cfg-b", "model": "model-b", "task": "all"},
        ],
        repo_dir=repo,
        golden_path=golden_path,
        live=False,  # no model call; score the fixture repo as-is
    )
    assert len(result["rows"]) == 2
    # Both score the same (perfect) repo, so best is the first by max().
    assert result["best_score"] == 1.0
    assert result["best_label"] in {"cfg-a", "cfg-b"}
    for row in result["rows"]:
        assert row["live_ran"] is False
        assert row["overall_score"] == 1.0

    table = format_comparison(result)
    assert "A/B Comparison" in table
    assert "cfg-a" in table and "cfg-b" in table

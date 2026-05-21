"""Deterministic scoring for the Always-On Ops Agent eval harness.

No model calls happen here. The scorers take *produced* data (the agent's
output, already loaded into plain dicts) and compare it to the golden ground
truth, returning per-item results plus aggregate metrics.

Public surface:
    score_triage(produced_issues, golden) -> dict
    score_compliance(produced_findings, golden) -> dict
    score_run(repo_dir, golden_path) -> dict
    format_report_card(card) -> str
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# Canonical compliance rule keys, from compliance-policy.md. Each maps to the
# words/phrases we expect to see in a finding body when that rule is flagged.
RULE_KEYS = [
    "data_residency",
    "audit_rights",
    "termination",
    "liability_cap",
    "subprocessors",
    "breach_notification",
    "governing_law",
]

_RULE_PATTERNS: dict[str, list[str]] = {
    "data_residency": ["data_residency", "data residency", "residency", "eu data", "eea"],
    "audit_rights": ["audit_rights", "audit right", "audit"],
    "termination": ["termination", "terminate for convenience", "for convenience"],
    "liability_cap": ["liability_cap", "liability cap", "limitation of liability", "liability"],
    "subprocessors": ["subprocessors", "subprocessor", "sub-processor"],
    "breach_notification": [
        "breach_notification",
        "breach notification",
        "data breach",
        "security incident",
        "breach",
    ],
    "governing_law": ["governing_law", "governing law", "jurisdiction"],
}

_CONFIDENCE_LEVELS = ("confirmed", "suspected", "none")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _agent_comment_text(issue: dict) -> str:
    """Concatenate the text of every comment authored by the agent."""
    parts: list[str] = []
    for comment in issue.get("comments", []) or []:
        if not isinstance(comment, dict):
            continue
        author = str(comment.get("author", "")).lower()
        if "always-on-ops-agent" in author or "ops-agent" in author:
            for key in ("body", "text", "note", "comment"):
                val = comment.get(key)
                if isinstance(val, str):
                    parts.append(val)
            # Some agents stash structured fields directly on the comment.
            for key in ("classification", "correlation", "correlation_confidence",
                        "confidence", "recommended_action", "runbook"):
                val = comment.get(key)
                if isinstance(val, str):
                    parts.append(f"{key}: {val}")
    return "\n".join(parts)


def _detect_is_incident(issue: dict, comment_text: str) -> bool:
    """True unless the agent classified it a feature request / non-incident.

    Signals (any one is enough): the agent says feature-request / not-an-incident
    / backlog / planned work, or it explicitly left severity null while saying so.
    """
    text = comment_text.lower()
    not_incident_markers = (
        "feature request",
        "feature-request",
        "not an incident",
        "not-an-incident",
        "no action / backlog",
        "no action/backlog",
        "backlog",
        "planned work",
        "do not page",
        "don't page",
        "not paged",
    )
    return not any(marker in text for marker in not_incident_markers)


def _extract_severity(issue: dict) -> str | None:
    sev = issue.get("severity")
    if sev is None:
        return None
    sev = str(sev).strip().upper()
    m = re.search(r"P[0-3]", sev)
    return m.group(0) if m else (sev or None)


def _detect_confidence(comment_text: str) -> str | None:
    """Find the agent's stated correlation confidence, case-insensitively.

    Look for an explicit 'correlation ... <level>' phrasing first, else fall
    back to the strongest bare keyword present. 'none' is only inferred when it
    appears near a correlation statement, so unrelated uses of the word 'none'
    don't trip us up.
    """
    text = comment_text.lower()
    if not text:
        return None

    # Prefer a level that sits near the word "correlation".
    for level in _CONFIDENCE_LEVELS:
        if re.search(rf"correlation[^.\n]*\b{level}\b", text) or re.search(
            rf"\b{level}\b[^.\n]*correlation", text
        ):
            return level

    # Fall back to the strongest explicit signal, in priority order.
    if re.search(r"\bconfirmed\b", text):
        return "confirmed"
    if re.search(r"\bsuspect", text):
        return "suspected"
    if re.search(r"\bno (?:deploy )?correlation\b", text) or re.search(
        r"correlation[:=]\s*none", text
    ):
        return "none"
    if re.search(r"\bnone\b", text):
        return "none"
    return None


def _keyword_hit_rate(comment_text: str, severity: str | None, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    haystack = comment_text.lower()
    if severity:
        haystack += " " + severity.lower()
    hits = sum(1 for kw in keywords if kw.lower() in haystack)
    return hits / len(keywords)


def _detect_violation_keys(finding: dict) -> set[str]:
    """Parse rule keys from a compliance finding's text, case-insensitively."""
    text_parts: list[str] = []
    for key in ("body", "title"):
        val = finding.get(key)
        if isinstance(val, str):
            text_parts.append(val)
    for comment in finding.get("comments", []) or []:
        if isinstance(comment, dict):
            for v in comment.values():
                if isinstance(v, str):
                    text_parts.append(v)
    haystack = "\n".join(text_parts).lower()

    found: set[str] = set()
    for key, patterns in _RULE_PATTERNS.items():
        if any(p in haystack for p in patterns):
            found.add(key)
    return found


def _prf(expected: set[str], detected: set[str]) -> dict:
    tp = len(expected & detected)
    fp = len(detected - expected)
    fn = len(expected - detected)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 1.0


# --------------------------------------------------------------------------- #
# triage
# --------------------------------------------------------------------------- #
def score_triage(produced_issues: dict[str, dict], golden: dict) -> dict:
    """Score triage output against golden ground truth.

    produced_issues: issue id -> issue JSON (severity set, agent comment appended).
    golden: the full golden dict (uses golden["triage"]).
    """
    truth = golden.get("triage", {})
    per_issue: dict[str, dict] = {}

    sev_correct: list[float] = []
    incident_correct: list[float] = []
    conf_correct: list[float] = []
    kw_rates: list[float] = []

    for issue_id, expected in truth.items():
        produced = produced_issues.get(issue_id, {}) or {}
        comment_text = _agent_comment_text(produced)

        got_sev = _extract_severity(produced)
        got_incident = _detect_is_incident(produced, comment_text)
        got_conf = _detect_confidence(comment_text)
        # A non-incident (feature request / backlog) has no deploy correlation by
        # definition, so an absent confidence reads as "none" rather than unknown.
        if not got_incident and got_conf is None:
            got_conf = "none"
        kw_rate = _keyword_hit_rate(
            comment_text, got_sev, expected.get("classification_keywords", [])
        )

        sev_ok = got_sev == expected.get("expected_severity")
        incident_ok = got_incident == expected.get("is_incident")
        conf_ok = got_conf == expected.get("expected_correlation_confidence")

        sev_correct.append(1.0 if sev_ok else 0.0)
        incident_correct.append(1.0 if incident_ok else 0.0)
        conf_correct.append(1.0 if conf_ok else 0.0)
        kw_rates.append(kw_rate)

        per_issue[issue_id] = {
            "present": bool(produced),
            "expected_severity": expected.get("expected_severity"),
            "got_severity": got_sev,
            "severity_correct": sev_ok,
            "expected_is_incident": expected.get("is_incident"),
            "got_is_incident": got_incident,
            "is_incident_correct": incident_ok,
            "expected_correlation_confidence": expected.get("expected_correlation_confidence"),
            "got_correlation_confidence": got_conf,
            "correlation_confidence_correct": conf_ok,
            "keyword_hit_rate": kw_rate,
        }

    metrics = {
        "severity_accuracy": _mean(sev_correct),
        "is_incident_accuracy": _mean(incident_correct),
        "correlation_confidence_accuracy": _mean(conf_correct),
        "classification_keyword_hit_rate": _mean(kw_rates),
        "count": len(truth),
    }
    metrics["score"] = _mean(
        [
            metrics["severity_accuracy"],
            metrics["is_incident_accuracy"],
            metrics["correlation_confidence_accuracy"],
            metrics["classification_keyword_hit_rate"],
        ]
    )
    return {"per_issue": per_issue, "metrics": metrics}


# --------------------------------------------------------------------------- #
# compliance
# --------------------------------------------------------------------------- #
def score_compliance(produced_findings: dict[str, dict], golden: dict) -> dict:
    """Score compliance findings against golden ground truth.

    produced_findings: vendor slug -> finding JSON, or vendor absent / None
    when no finding was filed for that vendor.
    """
    truth = golden.get("compliance", {})
    per_vendor: dict[str, dict] = {}

    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    clean_correct: list[float] = []

    for vendor, expected in truth.items():
        finding = produced_findings.get(vendor)
        has_finding = bool(finding)
        expected_clean = expected.get("clean", False)
        expected_set = set(expected.get("expected_violations", []))

        detected_set = _detect_violation_keys(finding) if has_finding else set()
        prf = _prf(expected_set, detected_set)

        precisions.append(prf["precision"])
        recalls.append(prf["recall"])
        f1s.append(prf["f1"])

        if expected_clean:
            # A clean vendor is scored purely on whether a finding was wrongly filed.
            clean_ok = not has_finding
            clean_correct.append(1.0 if clean_ok else 0.0)
        else:
            clean_ok = None  # not applicable

        per_vendor[vendor] = {
            "expected_clean": expected_clean,
            "filed_finding": has_finding,
            "expected_violations": sorted(expected_set),
            "detected_violations": sorted(detected_set),
            "clean_handling_correct": clean_ok,
            **prf,
        }

    metrics = {
        "precision": _mean(precisions),
        "recall": _mean(recalls),
        "f1": _mean(f1s),
        "clean_accuracy": _mean(clean_correct),
        "count": len(truth),
    }
    # Overall compliance score blends F1 over violations with clean-handling.
    score_components = [metrics["f1"]]
    if clean_correct:
        score_components.append(metrics["clean_accuracy"])
    metrics["score"] = _mean(score_components)
    return {"per_vendor": per_vendor, "metrics": metrics}


# --------------------------------------------------------------------------- #
# whole-run scoring
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _load_produced(repo_dir: Path, golden: dict) -> tuple[dict, dict]:
    """Read issues/ from repo_dir into produced_issues + produced_findings.

    produced_issues keyed by the issue ids present in golden["triage"].
    produced_findings keyed by vendor slug present in golden["compliance"].
    """
    issues_dir = repo_dir / "issues"
    produced_issues: dict[str, dict] = {}
    produced_findings: dict[str, dict] = {}

    if not issues_dir.is_dir():
        return produced_issues, produced_findings

    for path in sorted(issues_dir.glob("*.json")):
        try:
            data = _load_json(path)
        except (json.JSONDecodeError, OSError):
            continue
        stem = path.stem  # e.g. PROD-4521 or COMPLIANCE-acme-data-platform
        if stem.upper().startswith("COMPLIANCE-"):
            slug = stem[len("COMPLIANCE-"):].lower()
            produced_findings[slug] = data
        else:
            issue_id = data.get("id", stem)
            produced_issues[issue_id] = data

    return produced_issues, produced_findings


def score_run(repo_dir: Path, golden_path: Path) -> dict:
    """Load the repo's issues/, score triage + compliance, return a report card."""
    repo_dir = Path(repo_dir)
    golden = _load_json(Path(golden_path))

    produced_issues, produced_findings = _load_produced(repo_dir, golden)

    triage = score_triage(produced_issues, golden)
    compliance = score_compliance(produced_findings, golden)

    overall = _mean([triage["metrics"]["score"], compliance["metrics"]["score"]])

    return {
        "repo_dir": str(repo_dir),
        "golden_path": str(golden_path),
        "triage": triage,
        "compliance": compliance,
        "overall_score": overall,
    }


# --------------------------------------------------------------------------- #
# pretty printing
# --------------------------------------------------------------------------- #
def _pct(x: float) -> str:
    return f"{x * 100:5.1f}%"


def _check(ok) -> str:
    if ok is None:
        return " - "
    return "PASS" if ok else "FAIL"


def format_report_card(card: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("Always-On Ops Agent — Eval Report Card")
    lines.append("=" * 64)
    lines.append(f"repo:   {card.get('repo_dir', '?')}")
    lines.append(f"golden: {card.get('golden_path', '?')}")
    lines.append("")

    # --- triage ---
    tri = card["triage"]
    tm = tri["metrics"]
    lines.append("TRIAGE")
    lines.append("-" * 64)
    lines.append(
        f"{'issue':<12}{'sev(exp/got)':<18}{'incid':<7}{'corr(exp/got)':<24}{'kw':>6}"
    )
    for issue_id, r in tri["per_issue"].items():
        sev = f"{r['expected_severity']}/{r['got_severity']}"
        incid = _check(r["is_incident_correct"])
        corr = f"{r['expected_correlation_confidence']}/{r['got_correlation_confidence']}"
        lines.append(
            f"{issue_id:<12}{sev:<18}{incid:<7}{corr:<24}{_pct(r['keyword_hit_rate']):>6}"
        )
    lines.append("")
    lines.append(f"  severity accuracy            {_pct(tm['severity_accuracy'])}")
    lines.append(f"  is_incident accuracy         {_pct(tm['is_incident_accuracy'])}")
    lines.append(f"  correlation-conf accuracy    {_pct(tm['correlation_confidence_accuracy'])}")
    lines.append(f"  classification keyword rate  {_pct(tm['classification_keyword_hit_rate'])}")
    lines.append(f"  >> triage score              {_pct(tm['score'])}")
    lines.append("")

    # --- compliance ---
    comp = card["compliance"]
    cm = comp["metrics"]
    lines.append("COMPLIANCE")
    lines.append("-" * 64)
    lines.append(f"{'vendor':<26}{'filed':<7}{'P':>7}{'R':>8}{'F1':>8}{'clean':>8}")
    for vendor, r in comp["per_vendor"].items():
        lines.append(
            f"{vendor:<26}"
            f"{('yes' if r['filed_finding'] else 'no'):<7}"
            f"{_pct(r['precision']):>7}{_pct(r['recall']):>8}{_pct(r['f1']):>8}"
            f"{_check(r['clean_handling_correct']):>8}"
        )
    lines.append("")
    lines.append(f"  precision (macro)            {_pct(cm['precision'])}")
    lines.append(f"  recall (macro)               {_pct(cm['recall'])}")
    lines.append(f"  f1 (macro)                   {_pct(cm['f1'])}")
    lines.append(f"  clean-handling accuracy      {_pct(cm['clean_accuracy'])}")
    lines.append(f"  >> compliance score          {_pct(cm['score'])}")
    lines.append("")

    lines.append("=" * 64)
    lines.append(f"OVERALL SCORE: {_pct(card['overall_score'])}")
    lines.append("=" * 64)
    return "\n".join(lines)

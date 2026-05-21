"""Unit tests for agent.memory (statefulness & feedback module)."""

from __future__ import annotations

import json

from agent.memory import (
    Outcome,
    content_hash,
    fingerprint,
    load_outcomes,
    prior_context_prompt,
    record_outcome,
    register_incident,
)

# --------------------------------------------------------------------------- #
# Fixtures: representative issue shapes
# --------------------------------------------------------------------------- #
NPE_ISSUE = {
    "id": "PROD-4521",
    "title": "NullPointerException in PaymentService at checkout",
    "body": (
        "p99 latency on /checkout jumped. Stack trace:\n\n"
        "java.lang.NullPointerException\n"
        "  at com.bts.payments.PaymentService.processCharge(PaymentService.java:142)\n"
        "  at com.bts.payments.PaymentController.charge(PaymentController.java:67)\n"
    ),
}

# Same underlying incident: same top frame, different surrounding prose.
NPE_ISSUE_DUP = {
    "id": "PROD-9999",
    "title": "Checkout failing with NullPointerException for many users",
    "body": (
        "Alarms firing again. Sampled stack:\n\n"
        "java.lang.NullPointerException\n"
        "  at com.bts.payments.PaymentService.processCharge(PaymentService.java:142)\n"
        "  at com.bts.payments.PaymentController.charge(PaymentController.java:67)\n"
    ),
}

UNRELATED_ISSUE = {
    "id": "PROD-4498",
    "title": "Login endpoint occasionally returns 502 for ~30s windows",
    "body": (
        "Synthetic monitoring detected 3 separate 502 windows. Load balancer "
        "logs show upstream timeouts. Possible connection pool exhaustion?"
    ),
}


# --------------------------------------------------------------------------- #
# Outcome record/load round-trip
# --------------------------------------------------------------------------- #
def test_record_load_roundtrip_preserves_all_fields(tmp_path):
    o1 = Outcome(
        issue_id="PROD-4521",
        agent_severity="P0",
        agent_classification="incident",
        human_severity="P1",
        human_verdict="downgraded",
        note="tenant-specific, not all customers",
        recorded_at="2026-05-20T10:00:00Z",
    )
    o2 = Outcome(
        issue_id="PROD-4506",
        agent_severity=None,
        agent_classification="feature-request",
    )

    record_outcome(o1, state_dir=tmp_path)
    record_outcome(o2, state_dir=tmp_path)

    loaded = load_outcomes(state_dir=tmp_path)
    assert len(loaded) == 2
    assert loaded[0] == o1  # all fields preserved exactly
    assert loaded[1].issue_id == "PROD-4506"
    assert loaded[1].agent_severity is None
    assert loaded[1].agent_classification == "feature-request"
    assert loaded[1].human_severity is None
    assert loaded[1].note == ""
    assert loaded[1].recorded_at  # auto-populated ISO timestamp


def test_record_outcome_writes_one_json_line_per_call(tmp_path):
    record_outcome(
        Outcome(issue_id="A", agent_severity="P2", agent_classification="incident"),
        state_dir=tmp_path,
    )
    record_outcome(
        Outcome(issue_id="B", agent_severity="P3", agent_classification="incident"),
        state_dir=tmp_path,
    )
    path = tmp_path / "outcomes.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["issue_id"] == "A"
    assert json.loads(lines[1])["issue_id"] == "B"


def test_load_outcomes_missing_file_returns_empty(tmp_path):
    assert load_outcomes(state_dir=tmp_path) == []


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #
def test_fingerprint_is_stable_across_calls(tmp_path):
    fp1 = fingerprint(NPE_ISSUE)
    fp2 = fingerprint(NPE_ISSUE)
    assert fp1 == fp2
    assert len(fp1) == 12
    assert all(c in "0123456789abcdef" for c in fp1)


def test_fingerprint_collides_for_same_npe_frame():
    assert fingerprint(NPE_ISSUE) == fingerprint(NPE_ISSUE_DUP)


def test_fingerprint_differs_for_unrelated_issues():
    assert fingerprint(NPE_ISSUE) != fingerprint(UNRELATED_ISSUE)


def test_fingerprint_handles_missing_fields():
    # No body / no title should not raise and should be deterministic.
    fp = fingerprint({})
    assert len(fp) == 12
    assert fingerprint({}) == fp


# --------------------------------------------------------------------------- #
# register_incident
# --------------------------------------------------------------------------- #
def test_register_incident_first_then_recurrence_and_count(tmp_path):
    fp1, recurred1 = register_incident(NPE_ISSUE, state_dir=tmp_path)
    assert recurred1 is False

    fp2, recurred2 = register_incident(NPE_ISSUE_DUP, state_dir=tmp_path)
    assert recurred2 is True
    assert fp1 == fp2  # same incident

    data = json.loads((tmp_path / "incidents.json").read_text())
    assert data[fp1]["count"] == 2
    assert data[fp1]["first_issue_id"] == "PROD-4521"
    assert data[fp1]["first_seen"]

    # A third, unrelated incident is its own first sighting.
    fp3, recurred3 = register_incident(UNRELATED_ISSUE, state_dir=tmp_path)
    assert recurred3 is False
    assert fp3 != fp1


# --------------------------------------------------------------------------- #
# content_hash
# --------------------------------------------------------------------------- #
def test_content_hash_order_independent_for_dicts():
    a = {"x": 1, "y": [2, 3], "z": {"k": "v"}}
    b = {"z": {"k": "v"}, "y": [2, 3], "x": 1}
    assert content_hash(a) == content_hash(b)


def test_content_hash_deterministic_and_distinguishing():
    payload = {"a": 1, "b": 2}
    assert content_hash(payload) == content_hash(payload)
    assert content_hash(payload) != content_hash({"a": 1, "b": 3})
    assert content_hash("hello") == content_hash("hello")
    assert content_hash("hello") != content_hash("world")


# --------------------------------------------------------------------------- #
# prior_context_prompt
# --------------------------------------------------------------------------- #
def test_prior_context_prompt_empty_with_no_state(tmp_path):
    assert prior_context_prompt(state_dir=tmp_path) == ""


def test_prior_context_prompt_includes_outcomes_and_recurrence(tmp_path):
    record_outcome(
        Outcome(
            issue_id="PROD-4521",
            agent_severity="P0",
            agent_classification="incident",
            human_severity="P1",
            human_verdict="downgraded",
            note="tenant-specific only",
        ),
        state_dir=tmp_path,
    )
    register_incident(NPE_ISSUE, state_dir=tmp_path)
    register_incident(NPE_ISSUE_DUP, state_dir=tmp_path)

    text = prior_context_prompt(state_dir=tmp_path)
    assert text != ""
    # Outcome / override info present.
    assert "PROD-4521" in text
    assert "P1" in text
    assert "downgraded" in text
    # Recurrence info present.
    assert "recurring" in text.lower()
    assert "2x" in text
    # Stays concise.
    assert len(text.split()) < 400


def test_prior_context_prompt_truncates_long_history(tmp_path):
    # Many outcomes + many recurrences must still fit the word budget.
    for i in range(50):
        record_outcome(
            Outcome(
                issue_id=f"PROD-{i}",
                agent_severity="P2",
                agent_classification="incident",
                human_severity="P3",
                note="x" * 300,
            ),
            state_dir=tmp_path,
        )
    for i in range(30):
        issue = {"id": f"PROD-{i}", "title": f"unique error number {i} deadlock", "body": ""}
        register_incident(issue, state_dir=tmp_path)
        register_incident(issue, state_dir=tmp_path)

    text = prior_context_prompt(state_dir=tmp_path)
    assert len(text.split()) < 400

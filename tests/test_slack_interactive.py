"""Unit tests for the interactive-Slack module. Standard-library only logic;
run with: uv run --with pytest pytest tests/test_slack_interactive.py -q
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.parse

from agent.slack_interactive import (
    ack_response,
    build_findings_message,
    decide_from_action,
    parse_action_payload,
    verify_slack_signature,
)


def _action_ids(payload: dict) -> list[str]:
    ids = []
    for block in payload["blocks"]:
        if block.get("type") == "actions":
            ids.extend(el["action_id"] for el in block["elements"])
    return ids


def test_build_message_contains_buttons_pr_and_titles():
    findings = [
        {"id": "PROD-1", "title": "Checkout 500s", "severity": "high"},
        {"id": "PROD-2", "title": "Slow search", "severity": "low"},
    ]
    msg = build_findings_message(
        summary="2 findings triaged",
        pr_url="https://github.com/acme/repo/pull/7",
        findings=findings,
        action_token="tok-abc",
    )

    assert "blocks" in msg and "text" in msg

    ids = _action_ids(msg)
    assert ids == ["ops_approve", "ops_pr_only", "ops_dismiss"]

    blob = json.dumps(msg)
    assert "https://github.com/acme/repo/pull/7" in blob
    assert "Checkout 500s" in blob
    assert "Slow search" in blob

    # Every button must carry the action token so the handler can correlate.
    for block in msg["blocks"]:
        if block.get("type") == "actions":
            for el in block["elements"]:
                assert el["value"] == "tok-abc"


def test_build_message_no_pr_url():
    msg = build_findings_message(
        summary="nothing to do",
        pr_url=None,
        findings=[],
        action_token="t",
    )
    assert _action_ids(msg) == ["ops_approve", "ops_pr_only", "ops_dismiss"]
    assert "PR:" not in msg["text"]


def test_build_message_stays_within_50_blocks():
    findings = [
        {"id": f"PROD-{i}", "title": f"Issue {i}", "severity": "high"}
        for i in range(100)
    ]
    msg = build_findings_message(
        summary="lots",
        pr_url="https://example.com/pr/1",
        findings=findings,
        action_token="tok",
    )
    assert len(msg["blocks"]) <= 50
    # The actions block survives truncation.
    assert _action_ids(msg) == ["ops_approve", "ops_pr_only", "ops_dismiss"]


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"v0:{timestamp}:{body.decode()}".encode()
    return "v0=" + hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()


def test_verify_signature_good():
    secret = "s3cr3t"
    ts = "1700000000"
    body = b"payload=hello"
    sig = _sign(secret, ts, body)
    assert verify_slack_signature(
        signing_secret=secret,
        timestamp=ts,
        signature=sig,
        body=body,
        now=1700000000,
    )


def test_verify_signature_tampered():
    secret = "s3cr3t"
    ts = "1700000000"
    body = b"payload=hello"
    sig = _sign(secret, ts, body)
    tampered = sig[:-1] + ("0" if sig[-1] != "0" else "1")
    assert not verify_slack_signature(
        signing_secret=secret,
        timestamp=ts,
        signature=tampered,
        body=body,
        now=1700000000,
    )


def test_verify_signature_replay_rejected():
    secret = "s3cr3t"
    ts = "1700000000"
    body = b"payload=hello"
    sig = _sign(secret, ts, body)
    # now far ahead of the timestamp -> outside max_skew_s -> rejected
    assert not verify_slack_signature(
        signing_secret=secret,
        timestamp=ts,
        signature=sig,
        body=body,
        now=1700000000 + 10_000,
    )


def test_verify_signature_bad_timestamp():
    assert not verify_slack_signature(
        signing_secret="s",
        timestamp="not-a-number",
        signature="v0=abc",
        body=b"x",
        now=0,
    )


def test_parse_action_payload_round_trips():
    raw = {
        "actions": [{"action_id": "ops_approve", "value": "tok-xyz"}],
        "user": {"id": "U123", "username": "alice"},
        "response_url": "https://hooks.slack.com/actions/abc",
    }
    form = urllib.parse.urlencode({"payload": json.dumps(raw)})

    parsed = parse_action_payload(form)
    assert parsed["action_id"] == "ops_approve"
    assert parsed["value"] == "tok-xyz"
    assert parsed["action_token"] == "tok-xyz"
    assert parsed["user"] == "alice"
    assert parsed["response_url"] == "https://hooks.slack.com/actions/abc"
    assert parsed["raw"] == raw


def test_parse_action_payload_falls_back_to_user_id():
    raw = {
        "actions": [{"action_id": "ops_dismiss", "value": "t"}],
        "user": {"id": "U999"},
    }
    form = urllib.parse.urlencode({"payload": json.dumps(raw)})
    parsed = parse_action_payload(form)
    assert parsed["user"] == "U999"
    assert parsed["response_url"] is None


def test_decide_from_action_mapping():
    assert decide_from_action({"action_id": "ops_approve"}) == {
        "decision": "approve",
        "page": True,
        "message": "Approved — paging on-call.",
    }
    pr_only = decide_from_action({"action_id": "ops_pr_only"})
    assert pr_only["decision"] == "pr_only" and pr_only["page"] is False
    dismiss = decide_from_action({"action_id": "ops_dismiss"})
    assert dismiss["decision"] == "dismiss" and dismiss["page"] is False
    unknown = decide_from_action({"action_id": "wat"})
    assert unknown["decision"] == "unknown" and unknown["page"] is False


def test_ack_response_shape():
    ack = ack_response({"message": "ok!"})
    assert ack == {"replace_original": True, "text": "ok!"}
    # falls back when no message present
    assert ack_response({})["text"] == "Action received."

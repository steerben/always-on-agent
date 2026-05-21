"""Interactive Slack: Block Kit messages with approve/reject buttons and the
pure helpers needed to verify, parse, and act on Slack button clicks.

Standard library only. Every function here is pure / network-free so the
orchestrator can wire them into a FastAPI `/slack/actions` endpoint and unit
tests can exercise them deterministically.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse

# Slack hard-caps a message at 50 blocks. We reserve a few for the header,
# summary, PR link, and the actions block, leaving the rest for findings.
_MAX_BLOCKS = 50

_ACTION_APPROVE = "ops_approve"
_ACTION_PR_ONLY = "ops_pr_only"
_ACTION_DISMISS = "ops_dismiss"


def build_findings_message(
    *,
    summary: str,
    pr_url: str | None,
    findings: list[dict],
    action_token: str,
) -> dict:
    """Build a Slack Block Kit payload for a completed run.

    Shows the summary, an optional PR link, one section + context block per
    finding, and a single actions block with the three decision buttons. The
    `action_token` is encoded into each button's `value` so the click handler
    can correlate the action back to this run. Stays within Slack's 50-block
    limit, truncating findings if necessary.
    """
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Ops Agent run complete", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary or "(no summary)"},
        },
    ]

    if pr_url:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Pull request:* <{pr_url}|{pr_url}>"},
            }
        )

    # Fixed blocks above plus the actions block we always append at the end;
    # each finding costs two blocks (section + context).
    actions_block = {
        "type": "actions",
        "block_id": "ops_actions",
        "elements": [
            {
                "type": "button",
                "action_id": _ACTION_APPROVE,
                "style": "primary",
                "text": {"type": "plain_text", "text": "Approve & page on-call", "emoji": True},
                "value": action_token,
            },
            {
                "type": "button",
                "action_id": _ACTION_PR_ONLY,
                "text": {"type": "plain_text", "text": "PR only — don't page", "emoji": True},
                "value": action_token,
            },
            {
                "type": "button",
                "action_id": _ACTION_DISMISS,
                "style": "danger",
                "text": {"type": "plain_text", "text": "Dismiss", "emoji": True},
                "value": action_token,
            },
        ],
    }

    # Budget remaining for finding blocks (reserve one for the actions block
    # and, if we truncate, one for the "and N more" context note).
    reserved = len(blocks) + 1
    budget = _MAX_BLOCKS - reserved
    max_findings = budget // 2

    shown = findings[:max_findings] if max_findings >= 0 else []
    for f in shown:
        title = f.get("title", "(untitled)")
        fid = f.get("id", "?")
        severity = f.get("severity", "unknown")
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title}*"},
            }
        )
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"`{fid}` · severity: *{severity}*"},
                ],
            }
        )

    remaining = len(findings) - len(shown)
    if remaining > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_…and {remaining} more finding(s) not shown._"},
                ],
            }
        )

    blocks.append(actions_block)

    # Defensive: never exceed the cap (keep the actions block at the tail).
    if len(blocks) > _MAX_BLOCKS:
        blocks = blocks[: _MAX_BLOCKS - 1] + [actions_block]

    text_fallback = summary or "Ops Agent run complete"
    if pr_url:
        text_fallback = f"{text_fallback}\nPR: {pr_url}"

    return {"text": text_fallback, "blocks": blocks}


def verify_slack_signature(
    *,
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    now: float | None = None,
    max_skew_s: int = 300,
) -> bool:
    """Verify a Slack request signature (v0 scheme) with replay protection.

    basestring = "v0:{timestamp}:{body}"; HMAC-SHA256 with `signing_secret`;
    compare "v0=" + hexdigest to `signature` in constant time. Returns False on
    a bad/missing timestamp or if it is older/newer than `max_skew_s` seconds.
    """
    if now is None:
        now = time.time()

    try:
        ts_int = int(timestamp)
    except (TypeError, ValueError):
        return False

    if abs(now - ts_int) > max_skew_s:
        return False

    body_decoded = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body
    basestring = f"v0:{timestamp}:{body_decoded}".encode("utf-8")
    digest = hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected = "v0=" + digest

    return hmac.compare_digest(expected, signature or "")


def parse_action_payload(form_body: str) -> dict:
    """Parse Slack's `payload=<urlencoded JSON>` interactive-action body.

    Returns a normalized dict with action_id, value, action_token (the value
    decoded — here the value *is* the token), user, response_url, and the raw
    parsed JSON.
    """
    fields = urllib.parse.parse_qs(form_body)
    payloads = fields.get("payload")
    if not payloads:
        raise ValueError("no 'payload' field in form body")

    raw = json.loads(payloads[0])

    actions = raw.get("actions") or []
    first = actions[0] if actions else {}
    action_id = first.get("action_id", "")
    value = first.get("value", "")

    user = raw.get("user") or {}
    username = user.get("username") or user.get("id") or ""

    return {
        "action_id": action_id,
        "value": value,
        "action_token": value,
        "user": username,
        "response_url": raw.get("response_url"),
        "raw": raw,
    }


def decide_from_action(parsed: dict) -> dict:
    """Pure mapping from a parsed action to a decision. No side effects."""
    action_id = parsed.get("action_id", "")

    if action_id == _ACTION_APPROVE:
        return {
            "decision": "approve",
            "page": True,
            "message": "Approved — paging on-call.",
        }
    if action_id == _ACTION_PR_ONLY:
        return {
            "decision": "pr_only",
            "page": False,
            "message": "Acknowledged — keeping the PR open, no page.",
        }
    if action_id == _ACTION_DISMISS:
        return {
            "decision": "dismiss",
            "page": False,
            "message": "Dismissed — no action taken.",
        }
    return {
        "decision": "unknown",
        "page": False,
        "message": f"Unrecognized action: {action_id!r}",
    }


def ack_response(decision: dict) -> dict:
    """Build a Slack message-replacement payload acknowledging the click.

    Suitable to POST back to the interaction's `response_url`. This function
    does not perform any network I/O.
    """
    return {
        "replace_original": True,
        "text": decision.get("message", "Action received."),
    }

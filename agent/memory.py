"""Statefulness & feedback for the Always-On Ops Agent.

Future agent runs learn from past ones by reading two small files under a
state directory (``state/`` at the repo root by default):

- ``outcomes.jsonl`` — one JSON object per line, recording how a human
  ultimately judged an agent triage (severity overrides, verdicts, notes).
- ``incidents.json`` — a dict mapping an incident *fingerprint* to metadata
  (``first_seen``, ``first_issue_id``, ``count``), used to detect recurrences.

Everything here is standard-library only. Each public function accepts an
optional ``state_dir`` so tests can point it at a ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Repo root = parent of this package directory (mirrors agent/config.py's
# _DEFAULT_REPO_DIR). State lives in a sibling ``state/`` directory.
STATE_DIR = Path(__file__).resolve().parent.parent / "state"

_OUTCOMES_FILE = "outcomes.jsonl"
_INCIDENTS_FILE = "incidents.json"


def _now_iso() -> str:
    """Current time as an ISO-8601 UTC string (seconds precision, 'Z' suffix)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _resolve(state_dir: Path | None) -> Path:
    return Path(state_dir) if state_dir is not None else STATE_DIR


# --------------------------------------------------------------------------- #
# Human outcomes / feedback
# --------------------------------------------------------------------------- #
@dataclass
class Outcome:
    """A single recorded outcome: what the agent decided vs. what humans did.

    ``agent_*`` capture the agent's own triage; ``human_*`` capture any
    correction or verdict a human later applied (left ``None`` when unknown).
    """

    issue_id: str
    agent_severity: str | None
    agent_classification: str
    human_severity: str | None = None
    human_verdict: str | None = None
    note: str = ""
    recorded_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Outcome":
        # Tolerate extra keys; supply defaults for any missing optional fields.
        return cls(
            issue_id=data["issue_id"],
            agent_severity=data.get("agent_severity"),
            agent_classification=data["agent_classification"],
            human_severity=data.get("human_severity"),
            human_verdict=data.get("human_verdict"),
            note=data.get("note", ""),
            recorded_at=data.get("recorded_at") or _now_iso(),
        )


def record_outcome(outcome: Outcome, *, state_dir: Path | None = None) -> None:
    """Append ``outcome`` as one JSON line to ``<state_dir>/outcomes.jsonl``."""
    target = _resolve(state_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / _OUTCOMES_FILE
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(outcome.to_dict(), sort_keys=True) + "\n")


def load_outcomes(*, state_dir: Path | None = None) -> list[Outcome]:
    """Read all recorded outcomes back. Returns ``[]`` if the file is missing."""
    path = _resolve(state_dir) / _OUTCOMES_FILE
    if not path.exists():
        return []
    outcomes: list[Outcome] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        outcomes.append(Outcome.from_dict(json.loads(line)))
    return outcomes


# --------------------------------------------------------------------------- #
# Incident fingerprinting & recurrence tracking
# --------------------------------------------------------------------------- #
# Common English / log-noise words stripped from titles so the signature keys
# on the words that actually identify an incident.
_STOPWORDS = frozenset(
    """
    a an and are as at be by for from has have in into is it its of on or that
    the to was were will with not no nor but if then else when while during
    """.split()
)

# A Java/JVM-style exception class, e.g. ``java.lang.NullPointerException`` or
# ``com.acme.FooException``.
_EXCEPTION_RE = re.compile(
    r"\b((?:[a-zA-Z_][\w]*\.)+[A-Z][A-Za-z0-9_]*(?:Exception|Error|Throwable))\b"
)

# The top stack frame, e.g. ``at com.bts.payments.PaymentService.processCharge``
# (we keep through the method name and drop the file:line, which varies).
_FRAME_RE = re.compile(r"^\s*at\s+([\w.$]+)", re.MULTILINE)

# Generic error tokens that meaningfully describe a failure mode.
_ERROR_TOKEN_RE = re.compile(
    r"\b(timeout|timeouts|502|503|500|504|deadlock|oom|exhaustion|"
    r"latency|nullpointerexception|connection|pool)\b",
    re.IGNORECASE,
)


def _title_tokens(title: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def _signature(issue: dict) -> str:
    """Build the normalized incident signature that the fingerprint hashes.

    Heuristic (in priority order, all lowercased and de-duplicated):

    1. The top stack-trace frame from the body, if present. We match the first
       ``at <symbol>`` line and keep through the method name (dropping the
       ``(File.java:NN)`` suffix, which is brittle). If no frame is present we
       fall back to the first JVM-style exception class name in the body.
    2. Significant title tokens (lowercased, stopwords and very short words
       removed), sorted for stability.
    3. Key error tokens found anywhere in title+body (timeout, 502, deadlock,
       NPE, connection pool, …), sorted.

    A *strong* signal (a concrete stack frame, or failing that an exception
    class) is treated as authoritative: when present, the signature keys on it
    alone, so two reports of the same crash at the same method collide even
    though their prose differs. Only when there is no such signal do we fall
    back to the title tokens + error tokens, which still separate unrelated
    issues (e.g. a 502/timeout report vs. a slow-upload report).
    """
    body = str(issue.get("body", "") or "")
    title = str(issue.get("title", "") or "")

    # Strongest signal: the top stack frame, else the first exception class.
    frame_match = _FRAME_RE.search(body)
    if frame_match:
        return f"frame={frame_match.group(1).lower()}"
    exc_match = _EXCEPTION_RE.search(body)
    if exc_match:
        return f"exception={exc_match.group(1).lower()}"

    # No stack signal: fall back to title tokens + error tokens.
    title_tokens = sorted(set(_title_tokens(title)))
    error_tokens = sorted(
        {m.group(0).lower() for m in _ERROR_TOKEN_RE.finditer(f"{title}\n{body}")}
    )
    return "|".join(
        [
            f"title={' '.join(title_tokens)}",
            f"errors={' '.join(error_tokens)}",
        ]
    )


def fingerprint(issue: dict) -> str:
    """Stable 12-char sha256 hex of an issue's normalized incident signature.

    See ``_signature`` for the heuristic. Stable across calls and across
    processes; designed so two reports of the same incident collide while
    unrelated issues do not.
    """
    sig = _signature(issue)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:12]


def _load_incidents(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def register_incident(
    issue: dict, *, state_dir: Path | None = None
) -> tuple[str, bool]:
    """Record an incident and report whether it is a recurrence.

    Returns ``(fingerprint, recurred)``. The first time a fingerprint is seen,
    stores ``{first_seen, first_issue_id, count: 1}`` and returns ``False``. On
    later sightings, increments ``count`` and returns ``True``. Persists
    ``<state_dir>/incidents.json``.
    """
    target = _resolve(state_dir)
    target.mkdir(parents=True, exist_ok=True)
    path = target / _INCIDENTS_FILE

    incidents = _load_incidents(path)
    fp = fingerprint(issue)
    issue_id = str(issue.get("id", "") or "")

    if fp in incidents:
        entry = incidents[fp]
        entry["count"] = int(entry.get("count", 1)) + 1
        recurred = True
    else:
        incidents[fp] = {
            "first_seen": _now_iso(),
            "first_issue_id": issue_id,
            "count": 1,
        }
        recurred = False

    path.write_text(
        json.dumps(incidents, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return fp, recurred


# --------------------------------------------------------------------------- #
# Content idempotency
# --------------------------------------------------------------------------- #
def content_hash(payload: dict | str) -> str:
    """Deterministic sha256 hex of ``payload``.

    For dicts, hashes canonical JSON (``sort_keys=True``) so key order does not
    matter. For strings, hashes the UTF-8 bytes directly. Used to skip
    re-writing a finding whose content has not changed.
    """
    if isinstance(payload, str):
        material = payload
    else:
        material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Prompt context
# --------------------------------------------------------------------------- #
def prior_context_prompt(*, state_dir: Path | None = None) -> str:
    """Render a concise plain-text block of prior state for prompt injection.

    Summarizes (a) recent human outcomes/overrides and (b) known recurring
    incident fingerprints, so the agent can calibrate. Returns ``""`` when
    there is no state. Stays well under ~400 words by capping how many entries
    are shown.
    """
    target = _resolve(state_dir)
    outcomes = load_outcomes(state_dir=target)
    incidents = _load_incidents(target / _INCIDENTS_FILE)

    if not outcomes and not incidents:
        return ""

    lines: list[str] = ["PRIOR RUN MEMORY (learn from this; do not blindly repeat past calls):"]

    # (a) Human outcomes — show the most recent few, prioritizing overrides.
    if outcomes:
        ordered = sorted(outcomes, key=lambda o: o.recorded_at, reverse=True)
        overrides = [o for o in ordered if o.human_severity or o.human_verdict]
        shown = (overrides + [o for o in ordered if o not in overrides])[:8]
        lines.append("")
        lines.append(f"Recent human feedback ({len(outcomes)} total, showing {len(shown)}):")
        for o in shown:
            parts = [f"- {o.issue_id}: agent={o.agent_classification}/{o.agent_severity}"]
            if o.human_severity and o.human_severity != o.agent_severity:
                parts.append(f"-> human corrected severity to {o.human_severity}")
            if o.human_verdict:
                parts.append(f"verdict={o.human_verdict}")
            if o.note:
                note = o.note if len(o.note) <= 120 else o.note[:117] + "..."
                parts.append(f"({note})")
            lines.append(" ".join(parts))

    # (b) Recurring incidents — those seen more than once, most frequent first.
    if incidents:
        recurring = sorted(
            (
                (fp, meta)
                for fp, meta in incidents.items()
                if int(meta.get("count", 1)) > 1
            ),
            key=lambda kv: int(kv[1].get("count", 1)),
            reverse=True,
        )[:8]
        if recurring:
            lines.append("")
            lines.append("Known recurring incidents (treat repeats as known patterns):")
            for fp, meta in recurring:
                lines.append(
                    f"- {fp}: seen {meta.get('count', 1)}x "
                    f"(first {meta.get('first_issue_id', '?')} "
                    f"on {meta.get('first_seen', '?')})"
                )

    return "\n".join(lines).strip()

# Eval Harness — Always-On Ops Agent

A golden-test evaluation package that scores an agent run against hand-derived
ground truth. The scoring path is **deterministic** (no model calls); an
optional **live** mode runs the agent first.

## Files

| File              | Purpose                                                        |
| ----------------- | -------------------------------------------------------------- |
| `golden.json`     | Ground truth (triage + compliance), with `_why` rationale.     |
| `score.py`        | Pure scoring: `score_triage`, `score_compliance`, `score_run`. |
| `run_eval.py`     | CLI: `python -m evals.run_eval` (score / live, CI gate).       |
| `ab.py`           | A/B scaffold: `compare(...)`, `format_comparison(...)`.        |
| `../tests/test_evals.py` | Pytest suite over the scoring math.                     |

Standard library only. `agent.core` is imported **lazily, inside live mode only**.

## Usage

```bash
# Score the repo's current issues/ against golden.json and print the report card.
# Exit 0 if overall score >= threshold (default 0.8), else 1 -> usable as a CI gate.
python -m evals.run_eval                      # score mode (default)
python -m evals.run_eval --threshold 0.9
python -m evals.run_eval --repo-dir /path/to/repo --golden evals/golden.json

# Run one live agent pass first, then score. Degrades gracefully with no API key
# or if the SDK import/run fails (prints a message, still scores the repo as-is).
python -m evals.run_eval --mode live --task all
```

Run the tests from the repo root (no pyproject edits needed):

```bash
uv run --with pytest pytest tests/test_evals.py -q
```

## What's scored

### Triage (per PROD issue)
Severity is read from the issue's `severity` field. Classification and
correlation confidence are parsed from the comment authored by
`always-on-ops-agent` (case-insensitive).

- **severity accuracy** — exact match of `P0..P3`/`null` vs golden.
- **is_incident accuracy** — did the agent correctly call it an incident vs a
  feature request / backlog item (detected via markers like "feature request",
  "not an incident", "backlog", "do not page").
- **correlation-confidence accuracy** — `confirmed` / `suspected` / `none`,
  preferring a level stated next to the word "correlation".
- **classification keyword hit rate** — fraction of the golden keywords present
  in the agent's note (+ severity).

The triage **score** is the mean of those four metrics.

### Compliance (per vendor)
Violation rule keys are parsed from the `COMPLIANCE-<slug>.json` finding body
(case-insensitive) against the seven policy rules: `data_residency`,
`audit_rights`, `termination`, `liability_cap`, `subprocessors`,
`breach_notification`, `governing_law`.

- **precision / recall / F1** of detected violation keys vs expected, macro-averaged.
- **clean accuracy** — for vendors that should be clean, scores whether *no*
  finding was filed (filing one is penalised; missing one is correct).

The compliance **score** blends macro-F1 with clean-handling accuracy.

### Overall
`overall_score = mean(triage_score, compliance_score)`.

## How to read the report card

```
TRIAGE
issue       sev(exp/got)      incid  corr(exp/got)            kw
PROD-4521   P0/P0             PASS   confirmed/confirmed   100.0%
...
  >> triage score              92.5%

COMPLIANCE
vendor                    filed      P       R      F1   clean
acme-data-platform        yes    100.0%  100.0% 100.0%     -
globex-messaging          no     100.0%  100.0% 100.0%  PASS
...
  >> compliance score          95.0%

OVERALL SCORE:  93.8%
```

`sev(exp/got)` shows expected vs detected severity; `incid` is whether the
incident-vs-feature call was right; `corr(exp/got)` shows expected vs detected
correlation confidence; `kw` is the keyword hit rate. For compliance, `clean`
is `PASS`/`FAIL` only for vendors that should be clean (`-` otherwise).

## Ground truth encoded

### Triage
| Issue       | Incident? | Severity | Correlation | Reasoning (short) |
| ----------- | --------- | -------- | ----------- | ----------------- |
| PROD-4521   | yes       | P0       | confirmed   | NPE `PaymentService.java:142` = known guest-checkout bug; payment-service v4.8.2 deployed 14 min before; all customers. |
| PROD-4487   | yes       | P1       | suspected   | One tenant (Acme); guest-checkout flag flipped for Acme's cohort 21 min before; no stack trace, inferred. |
| PROD-4519   | yes       | P2       | suspected   | signing-service TTL-reduce deploy ~22h earlier; runbook expects <6h, so timing is fuzzy. |
| PROD-4498   | yes       | P2       | none        | Connection-pool exhaustion; the only auth-service deploy is *after* the issue opened, so it can't be causal. |
| PROD-4506   | no        | null     | none        | Feature request (parallel batch jobs); must not be paged. |

### Compliance
| Vendor               | Clean? | Violations |
| -------------------- | ------ | ---------- |
| acme-data-platform   | no     | `data_residency`, `subprocessors`, `breach_notification` |
| globex-messaging     | yes    | (none) |
| sirius-storage       | no     | all seven |

**globex-messaging — clean.** Relied on: §2 "processed exclusively within the
EEA"; §3 "30 days' written notice"; §4 "terminate for convenience on 60 days";
§5 "12 months ... cap does not apply to ... data breach"; §6 "at least 30 days'
written notice before engaging any new subprocessor"; §7 "within 24 hours";
§8 "laws of England and Wales".

**sirius-storage — violates all seven.** §2 "data centres in Malaysia and
Singapore" (no EU residency); §3 "180 days' notice" (>90); §4 "Termination for
convenience is not permitted during the initial term" (5-year term); §5 "capped
at three (3) months of fees" (<12); §6 "without prior notice to or consent";
§7 "in due course" (no specific window); §8 "laws of Malaysia" (not a recognised
mature data-protection regime per the policy list).

## Adding new golden cases

1. **New incident:** add an entry under `golden.json -> triage` keyed by the
   issue id, with `is_incident`, `expected_severity` (`P0`..`P3` or `null`),
   `expected_correlation_confidence` (`confirmed`/`suspected`/`none`), and
   `classification_keywords`. Include a `_why` note documenting the deploy/runbook
   evidence you relied on.
2. **New contract:** add an entry under `golden.json -> compliance` keyed by the
   vendor slug (the part after `COMPLIANCE-` in the finding filename), with
   `clean` and `expected_violations` (subset of the seven rule keys). Document
   the offending clause in `_why`.
3. If a finding uses wording the parser doesn't recognise, extend
   `_RULE_PATTERNS` in `score.py` with the new phrasing.
4. Re-run `uv run --with pytest pytest tests/test_evals.py -q`.

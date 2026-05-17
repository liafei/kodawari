# Contract Change Log

This file records every breaking or additive change to wire-contract schemas
(JSON Schema files under `src/kodawari/schemas/`).  
Pair each entry with a `MERGED_CONTRACT_VERSION` bump in
`src/kodawari/infra/contract_version.py`.

The automated freeze guard (`tests/test_contract_version_freeze.py`) will
fail CI if any schema enum changes without a corresponding bump.

---

## ws113.v1 — 2026-04-22 (freeze baseline)

**Schemas frozen at this version:**

| Schema file | Enum field | Values |
|---|---|---|
| `runtime/peer_review_response.schema.json` | `gate_recommendation` | `APPROVED`, `ESCALATE_TO_HUMAN`, `PROCEED_TO_GATE`, `REVIEW_FIX_REQUIRED`, `REVIEW_PENDING`, `REVIEW_SCOPE_CONFLICT` |
| `runtime/peer_review_response.schema.json` | `severity` | `critical`, `high`, `info`, `low`, `medium` |
| `runtime/peer_review_response.schema.json` | `global_consistency_verdict` | `FAIL`, `INSUFFICIENT_CONTEXT`, `PASS` |
| `runtime/peer_review_response.schema.json` | `local_implementation_verdict` | `FAIL`, `PASS` |
| `observability/review_evidence.schema.json` | `status` | `FAIL`, `MISSING`, `PASS`, `UNKNOWN`, `WARN` |
| `observability/review_evidence.schema.json` | `review_mode` | `real_peer_review`, `simulated` |
| `observability/verify_report.schema.json` | `status` | `BLOCKED`, `FAIL`, `PASS`, `UNKNOWN` |
| `observability/eval_report.schema.json` | `status` | `BLOCKED`, `PASS` |
| `observability/field_report.schema.json` | `status` | `in_progress`, `open`, `resolved` |
| `observability/telemetry_snapshot.schema.json` | `signals.reasoning_tier` | `deep_reasoning`, `economy`, `standard` |
| `observability/worktree_baseline.schema.json` | `status` | `FAIL`, `PASS`, `WARN` |
| `contract_first/compliance_report.schema.json` | `checks[].check_name` | 12 values (see schema) |
| `coverage_matrix.schema.json` | `items[].status` | `FAIL`, `PARTIAL`, `PASS` |
| `spec.schema.json` | `priority` | `P0`, `P1`, `P2` |

**Breaking changes included in this freeze:**

### `review_mode`: `real_opus` → `real_peer_review`

- **Schema:** `observability/review_evidence.schema.json`
- **Field:** `review_mode`
- **Before:** `["real_opus", "simulated"]`
- **After:** `["real_peer_review", "simulated"]`
- **Why:** The value `real_opus` encoded a vendor name. The code was
  already emitting `real_peer_review` in `delivery_review.py:235` and
  `status_runtime.py:158`. The old value was never emitted by any
  current code path; this rename removes a dead enum arm and makes the
  schema consistent with actual runtime output.
- **Migration:** Any consumer that hardcoded `"real_opus"` must switch
  to `"real_peer_review"`. No code path in this repo emits `"real_opus"`.

### `gate_recommendation`: expanded from 3 to 6 values

- **Schema:** `runtime/peer_review_response.schema.json`
- **Field:** `gate_recommendation`
- **Before:** `["ESCALATE_TO_HUMAN", "PROCEED_TO_GATE", "REVIEW_FIX_REQUIRED"]`
- **After:** added `"APPROVED"`, `"REVIEW_PENDING"`, `"REVIEW_SCOPE_CONFLICT"`
- **Why:** `gate_round.py` and `collaboration_flow.py` were emitting these
  three values without schema coverage; this freeze adds them to the schema
  so the freeze guard can detect accidental renames.
- **Migration:** Additive — no consumer breakage. Consumers that used
  exhaustive match/switch should add handling for the three new values.

---

## How to make a future contract change

1. Edit the schema JSON file.
2. Bump `MERGED_CONTRACT_VERSION` in
   `src/kodawari/infra/contract_version.py`.
3. Run `pytest tests/test_contract_version_freeze.py -v` — the test
   will print the new baseline JSON; copy it into `_ENUM_BASELINE`.
4. Add an entry to this file describing what changed and why.
5. Commit all four files in one PR.

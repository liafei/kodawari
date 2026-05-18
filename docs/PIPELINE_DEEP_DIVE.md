# Pipeline Deep Dive

> 中文版本：[PIPELINE_DEEP_DIVE.zh-CN.md](PIPELINE_DEEP_DIVE.zh-CN.md)

This document describes what actually happens when you run
`kodawari work-all --feature X --prd Y`. The user-facing CLI is two flags;
the internal pipeline runs ~8 distinct stages with strict guarantees at every
boundary. Read this if you want to understand the engine, contribute, or
debug an unusual failure.

## Stage map

```
                              kodawari work-all
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 0 — Complexity tier detection       │
            │  → tier ∈ {lite, standard, heavy}          │
            │  → resolves max_cycles, max_rounds budget │
            └───────────────────────────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 1 — PRD slice detection (E1)        │
            │  → extracts ## Slice N: markers            │
            │  → if 0 or 1: single-slice flow            │
            │  → if 2+:    multi-slice loop              │
            └───────────────────────────────────────────┘
                                    │
                ┌───────────────────┴────────────────────┐
                ▼                                        ▼
       ┌──────────────────┐                ┌─────────────────────────┐
       │ Single-slice     │                │ Multi-slice loop        │
       │ (legacy default) │                │ For each slice in       │
       │                  │                │ order:                  │
       │                  │                │   STAGE 2..6 per slice  │
       └──────────────────┘                └─────────────────────────┘
                │                                        │
                └────────────────────┬───────────────────┘
                                     ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 2 — Planning conversation           │
            │  (planner ↔ plan_reviewer, multi-round)    │
            │  Phase B/C meta-blocker demotion           │
            │  Phase C late-round single-shot recovery  │
            │  Greenfield mode: PLANNING MODE hint       │
            │  → final_plan with confidence + ADRs       │
            └───────────────────────────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 3 — Architecture plan + scaffold    │
            │  → ARCHITECTURE_PLAN.json                  │
            │  → REPO_INVENTORY.json                     │
            │  → (greenfield) SCAFFOLD_MANIFEST.json     │
            └───────────────────────────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 4 — Task graph generation           │
            │  → TASK_GRAPH.json with 5-7 vertical-slice │
            │    tasks                                   │
            │  greenfield: maxItems=5 per task           │
            │  existing:   maxItems=3 per task           │
            │  → TASK_CARD_T1.json … TASK_CARD_Tn.json   │
            └───────────────────────────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 5 — task_cycle (auto-loop)          │
            │  next_task_selector picks the next task   │
            │  with blocked_by closure tracing.         │
            │                                            │
            │  For each task in dependency order:        │
            │  ┌────────────────────────────────────┐   │
            │  │ DESIGN     opus/planner ADR         │   │
            │  │   ↓                                 │   │
            │  │ IMPLEMENT  executor tool-use        │   │
            │  │   - read_file / str_replace /       │   │
            │  │   - write_new_file / check_complexity│   │
            │  │   ↓                                 │   │
            │  │ VERIFY     real pytest invocation  │   │
            │  │   (never silent-pass, see E1)      │   │
            │  │   ↓                                 │   │
            │  │ RULES_GATE static code redline      │   │
            │  │   ↓                                 │   │
            │  │ PEER_REVIEW impl_reviewer audit    │   │
            │  │   ↓                                 │   │
            │  │ FIX_ROUND  (if reviewer must_fix)  │   │
            │  │   loops back to IMPLEMENT          │   │
            │  │   ↓                                 │   │
            │  │ PROCEED_TO_GATE                    │   │
            │  └────────────────────────────────────┘   │
            │                                            │
            │  Safety mechanisms layered in:             │
            │  - Read-loop stall recovery (B fix)        │
            │  - Wall-clock watchdog (D1)                │
            │  - blocked_by closure trace (B1)           │
            │  - max_cycles + max_rounds bounds          │
            └───────────────────────────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 6 — Review bundle                   │
            │  Aggregate changed files, dual-review     │
            │  evidence, verify artifacts                │
            └───────────────────────────────────────────┘
                                    │
                                    ▼
            ┌───────────────────────────────────────────┐
            │  STAGE 7 — Release gate                    │
            │  Stops at AWAITING_DECISION by design     │
            │  User runs `kodawari decide` to ship       │
            └───────────────────────────────────────────┘
```

## Stage 0 — Complexity tier detection

**File**: `src/kodawari/autopilot/planning/complexity_detector.py`

Looks at PRD length, changed-file count, requested layers, and historical
signals (if `--tier auto`) to decide one of three lanes:

| Tier | Use case | Defaults |
|---|---|---|
| `lite` | Single-file refactor, doc tweak | max_cycles 2, no peer review by default |
| `standard` | Feature with 2-5 tasks | max_cycles 5, peer review on |
| `heavy` | Multi-task feature, greenfield, cross-surface | max_cycles 8, peer review on + strict gates |

The user sees the chosen tier in the first line of `work-all` output:
`[autopilot] auto-detected tier=heavy (source=hard_rule)`.

Override with `--tier lite|standard|heavy` if the auto-detection disagrees
with your intent. Common reasons to force: PRD looks tiny but actually
crosses multiple surfaces; PRD looks huge but is mostly out-of-scope prose.

## Stage 1 — PRD slice detection (E1)

**File**: `src/kodawari/autopilot/planning/prd_contract.py::extract_prd_slices`

Looks for `## Slice N: <title>` (or `## 切片 N:`, `## Phase N:`, `## Part N:`)
markers in the PRD. Two or more markers triggers **multi-slice mode**:

- Each slice is treated as a self-contained shipping unit.
- Per-slice planning_dir under `planning/<feature>/slice_NN/`.
- Each slice's plan + work runs independently, then a single
  parent-level review + release runs once across all slices.
- Resume support: completed slices persist in
  `.multi_slice_state.json` and are skipped on re-run (unless
  `--force-rerun`).

Zero or one marker → single-slice mode (the historical default, unchanged).

## Stage 2 — Planning conversation

**File**: `src/kodawari/autopilot/planning/planning_orchestrator.py::run_planning_conversation`

This is the multi-round dual-model conversation:

1. **Round 1**: planner role drafts a plan. PLANNING MODE prompt
   distinguishes greenfield vs existing so the planner doesn't propose
   gating on files that don't yet exist.
2. **Plan review**: plan_reviewer role audits the plan, emits
   `must_fix[]`, `should_fix[]`, score, gate_recommendation.
3. **Round 2+**: planner revises based on findings, declaring every
   change in `change_log[]`. Reviewer re-audits.
4. **Convergence**: reviewer returns `approved=true`, OR
   `relaxed_score_auto_approve` triggers (planner score ≥ 8.5 + reviewer
   score ≥ 8.0 with no blocking findings), OR Phase B/C meta-blocker
   streak demotion fires (≥3 consecutive rounds of only meta_blocker
   findings).

Hard ceiling: `max_rounds` (default 7 in standard tier).

Output: `final_plan` payload with confidence + every task spec + ADRs.

### Safety nets in the planning conversation

| Mechanism | What it prevents |
|---|---|
| Phase B meta-blocker streak demote | Reviewer that keeps blocking on "cite the round X" / recursive evidence demands |
| Phase C late-round single-shot recovery | Final-round meta blockers when planner+reviewer scores are both high |
| G prompt — validator boundary | Reviewer overreach into orchestrator-validated territory |
| L5 prompt — approval semantics | `approved=true + empty findings` is legitimate, not "incomplete review" |
| review_evidence_scout | Catches recursive `evidence_resolutions` loops |

## Stage 3 — Architecture plan + scaffold

**File**: `src/kodawari/autopilot/planning/architecture_plan.py`

Resolves project archetype (`fastapi_api`, `flask_api`, `django_web`, etc.)
and capabilities (`docs_runbook`, `lane_history_fetch`, etc.). For
greenfield, prefers explicit `SCAFFOLD_MANIFEST.json` archetype over
auto-detection (the A2/A3 fixes).

If `init` step ran first, the SCAFFOLD_MANIFEST.json records every
created file so the planner doesn't propose creating files that already
exist from the scaffold.

## Stage 4 — Task graph generation

**File**: `src/kodawari/autopilot/planning/task_graph.py`

Converts the `final_plan` into a `TASK_GRAPH.json` with topologically
ordered tasks, each carrying:

- `task_id`, `task_name`, `depends_on[]`
- `layer_owner` (schema / repository / service / route / view)
- `core_files[]` and `new_files[]` (subset of core_files)
- `verify_cmd`, `invariants[]`, `test_proof`
- `executability` (PASS / WARN / FAIL with reasons)
- `coverage_hints[]` for verify targeting

The graph drives Stage 5's `next_task_selector`.

### task_card files_to_change cap (A4)

Greenfield-mode task cards may declare up to **5 files** per task to
accommodate the bootstrap pattern (schema + model + repo + service +
test as a single vertical slice). Existing-mode caps at **3** to keep
refactor scope tight. The JSON schema allows 5; the Python validator is
the mode-aware gate.

## Stage 5 — task_cycle (the auto-loop)

**File**: `src/kodawari/cli/runtime/autopilot_workflow_runtime.py::_task_cycle_runtime`

Iterates through the task graph in dependency order. For each task,
runs the full collaboration loop. The peer-review setting (E2 fix)
honors the user's `--real-peer-review` (or the defaults.yaml setting)
with a per-entry `max_rounds` cap to prevent token-budget blowup on
N-task backlogs.

### The collaboration loop (per task)

| Action | Actor | Output |
|---|---|---|
| DESIGN | planner/opus | ADR + technical approach |
| IMPLEMENT | executor (mimo/codex/claude) via strict tool-use | Code changes, scope_drift PASS |
| VERIFY | system → pytest subprocess | command_executed=true, returncode 0 |
| RULES_GATE | system → code_redline | BLOCK/WARN/DASHBOARD verdict |
| PEER_REVIEW | impl_reviewer | approved + must_fix[] / score |
| FIX_ROUND | executor (if must_fix > 0) | Re-implement → back to VERIFY |
| PROCEED_TO_GATE | system | Mark task complete in next_task_selector |

### Safety mechanisms in task_cycle

| Mechanism | What it prevents |
|---|---|
| Read-loop stall recovery (B fix) | Executor that reads files in a loop without writing (common on weak instruction-following models like mimo) |
| `action_only_mode` on no_write_stall recovery | After detector fires, retry drops read tools — executor MUST write |
| Wall-clock watchdog (D1) | Whole-loop hang past `--max-wall-clock-seconds` → writes ABORT_REPORT.json, exit 124 |
| blocked_by closure trace (B1) | T_k FAIL → all downstream tasks report `blocked_by: [T_k]` in skipped_tasks output |
| `_no_fake_run_strict` gate | `WORKFLOW_REVIEW_ENABLED=1` makes verify/review silent-pass paths fail-closed |
| Scope-drift guard | Executor cannot modify files outside the task's declared `core_files` |

### The no-fake-run policy

Three fail-closed gates in production-strict mode (`WORKFLOW_REVIEW_ENABLED=1`,
no `PYTEST_CURRENT_TEST`, no `WORKFLOW_SDK_TEST_MODE`):

1. **Verify**: if pytest didn't actually run, `_build_compat_verify_payload`
   sets `passed=false` (E1 fix at runtime_checks.py)
2. **Self-review**: `LOCAL_DEFAULT_NOT_A_REVIEW` / `NOOP_FALLBACK_NOT_A_REVIEW`
   error codes block silent-pass on `local_adapter.self_review`
3. **Peer review**: empty review entries → `approved=False,
   approved_reason="no_peer_review_ran"`; degraded reviewer in production
   strict blocks proceed (Fix 9)

Dev / subscription-mode runs without `WORKFLOW_REVIEW_ENABLED` keep the
old simulation behavior so local iteration still works.

## Stage 6 — Review bundle

**File**: `src/kodawari/cli/evidence/review_cmd.py`

Aggregates the audit trail across all tasks: `changed_files`,
peer-review summaries, verify artifacts, gate verdicts. Emits
`.review_bundle.json` for the release gate to consume.

In multi-slice mode, this runs **once** at the parent planning_dir
level, combining evidence from all slices.

## Stage 7 — Release gate

**File**: `src/kodawari/cli/delivery/release_cmd.py`

Final ship-readiness check. By design, terminates at
`AWAITING_DECISION` rather than auto-shipping. The user runs:

```bash
kodawari decide --feature X --action accept  # ship
# or
kodawari decide --feature X --action reject  # halt
```

This is intentional friction — kodawari's whole posture is "no silent
pass". Letting an LLM-driven pipeline directly ship to main is exactly
the kind of trust-no-checks behavior the no-fake-run policy refuses.

## Artifact chain (single source of truth)

Each stage writes a typed, schema-validated JSON to `planning/<feature>/`:

```
PRD.md
  ↓ (Stage 1 + 2)
PRD_INTAKE.json     ← Stage 2 input
  ↓
PLANNING_CONVERSATION.json  ← multi-round audit trail
  ↓ (Stage 3)
ARCHITECTURE_PLAN.json
REPO_INVENTORY.json
SCAFFOLD_MANIFEST.json (greenfield only)
  ↓ (Stage 4)
TASK_GRAPH.json
TASK_CARD_T1.json ... TASK_CARD_Tn.json
  ↓ (Stage 5 — per task)
.autopilot_rounds.jsonl   ← every DESIGN/IMPLEMENT/VERIFY/REVIEW round
.autopilot_state.json     ← current_stage, completed_tasks, cycle counter
.execution_result.json    ← per-task executor output
.review_result.json       ← per-task reviewer output
.verify_report.json
.gate_result.json
.run_truth.json           ← aggregated truth at end of cycle
  ↓ (Stage 6)
.review_bundle.json
  ↓ (Stage 7)
RELEASE.md
ABORT_REPORT.json (if wall-clock or hard-stop)
```

Multi-slice mode mirrors this under `planning/<feature>/slice_NN/` per
slice, with `.multi_slice_state.json` at the parent level tracking
overall progress.

## Configuration surface

| Layer | Mechanism | Scope |
|---|---|---|
| CLI flag | `--max-cycles 10` | Single invocation |
| Project settings | `.claude/workflow/defaults.yaml` | Per project |
| Model config | `.claude/workflow/models.yaml` | Per project (transports + roles) |
| Env vars | `WORKFLOW_*` (see `docs/contracts/ENV_VAR_REFERENCE.md`) | Per shell |
| Built-in | `BUILTIN_DEFAULTS` in `workflow_defaults.py` | Process default |

Precedence (highest first): CLI flag > defaults.yaml > env var > built-in.

## Where the safety guarantees live in code

| Guarantee | File |
|---|---|
| No-fake-run verify | `autopilot/core/runtime_checks.py::_build_compat_verify_payload` |
| No-fake-run reviewer | `autopilot/engine/engine_review_mixin.py::_default_review_feedback` |
| No-fake-run peer summary | `autopilot/review/review_bridge.py::summarize_peer_review` |
| Read-loop stall recovery | `autopilot/recovery/stall_recovery.py` + `engine/engine_recovery_mixin.py` |
| `action_only_mode` retry | `autopilot/execution/execution_openai_tool_use.py::_apply_recovery_card_action_only_mode` |
| Wall-clock watchdog | `cli/runtime/autopilot_cmd.py::_start_wall_clock_watchdog` |
| Scope-drift guard | `autopilot/execution/execution_isolation.py` |
| blocked_by closure trace | `cli/contract/next_task_selector.py::_trace_blocking_ancestors` |
| Greenfield archetype lock | `cli/contract/generic_bootstrap.py::_scaffold_archetype_hint` |
| Multi-slice loop | `cli/runtime/work_all_runtime.py::_run_work_all_multi_slice` |

## See also

- [QUICKSTART.md](QUICKSTART.md) — first-run walkthrough
- [USER_GUIDE.md](USER_GUIDE.md) — operator manual
- [WRITING_PRD.md](WRITING_PRD.md) — how to write a PRD kodawari understands
- [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) — error code index
- [contracts/ENV_VAR_REFERENCE.md](contracts/ENV_VAR_REFERENCE.md) — every env var
- [CAPABILITY_MAP.md](CAPABILITY_MAP.md) — backend × capability wiring

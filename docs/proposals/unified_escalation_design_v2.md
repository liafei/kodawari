# Unified Workflow Escalation Design v2 (Final)

> Changelog vs v1 (after dual-agent review):
> - **[BLOCKING fix]** Concurrency: mandate `path_lock()` + `atomic_write_json()` from existing `infra/io_atomic.py` (no new primitives) — Explore agent confirmed both helpers exist
> - **[BLOCKING fix]** Feature-split semantics: sub-features start fresh count / parent SUPERSEDED read-only / `max_split_depth=2` / `depends_on` topological cycle check
> - **[HIGH fix]** Kind split: `PLANNING_TASK_TOO_LARGE` → `PLANNING_DEADLOCK` + `PLANNING_PREREQ_MISSING` (different Planner prompts)
> - **[HIGH fix]** Missing failure modes added: `RATE_LIMIT_429`, `VERIFY_CMD_MISSING`, `WORKTREE_MERGE_CONFLICT`, `STATE_LOCK_CONTENTION`, `CTRL_C_INTERRUPTED`
> - **[HIGH fix]** Backwards compat: keep `is_gate_complexity_exhausted()` + `.executor_redesign_*` filenames exported as deprecated aliases for ≥1 release; `WORKFLOW_ESCALATION_LEGACY=1` env var to force-route through old path
> - **[MEDIUM fix]** Skip semantics explicit per-phase (planning skip = abort feature; executor skip = drop task; gate skip = lower threshold then retry)
> - **[MEDIUM fix]** Cost cap: `WORKFLOW_ESCALATION_BUDGET_USD` env var; abort feature if exceeded
> - **[MEDIUM fix]** Test combinatorics: pytest parametrize fixtures, 15 functions × 8 params ≈ same coverage as 120 manual tests
> - **[NEW]** `kodawari decide --abort` and `--status` subcommands
> - **[NEW]** `escalation_policy.yaml` for org-level customization (auto-skip vs require-human per kind)

---

## 1. Final EscalationKind Catalog (12 kinds covering 30 failure codes)

| EscalationKind | failure_codes (real names from codebase) | Planner prompt scope | Auto-skip allowed |
|---|---|---|---|
| `EXECUTOR_STUCK` | EXECUTOR_STALLED_{NO_WRITE_PROGRESS,REDUNDANT_READS,FRAGMENTED_READS,REPEATED_SEARCH,PATCH_PLAN_REQUIRED,BUDGET_PRESSURE}; MAX_TOOL_ITERATIONS; INVALID_TOOL_CALL; REVIEW_BLOCKED_PERSISTENT | "give 3 alternatives to unstuck this task" | Yes |
| `EXECUTOR_PATCH_BROKEN` | EXECUTOR_STALLED_PATCH_FAILURES; PATCH_PLAN_MISSING; PATCH_TARGET_MISSING; PATCH_PRECONDITION_MISMATCH | "task_card patch_plan is wrong, rewrite" | Yes |
| `EXECUTOR_PRECONDITION_MISSING` | TASK_BLOCKED_BY_PRECONDITION; task_cycle.blocked_task | "insert prerequisite task before this one" | No (planning re-do) |
| `EXECUTOR_MODEL_INCAPABLE` | (subset of EXECUTOR_STUCK when wasted_read_count high) | "model can't progress, suggest stronger model" | Yes |
| `GATE_REFACTOR_NEEDED` | GATE_BLOCKED:{gate_complexity,gate_nesting,duplication,semantics} | 3 refactor options (current gate_complexity path) | Yes (with threshold adjust) |
| `GATE_FILE_SPLIT_NEEDED` | GATE_BLOCKED:{file_length,file_complexity_sum} | "split file into modules" | Yes (with threshold adjust) |
| `GATE_TASK_CARD_DESIGN_BUG` | GATE_BLOCKED:{scope_contract,import_rules}; TASK_CARD_FILES_TO_CHANGE_WRONG | "Planner rewrite task_card.files_to_change / requires" | No (planning re-do) |
| `COMPLIANCE_BLOCK` | GATE_BLOCKED:compliance | "must be human-resolved" | **No** |
| `PLANNING_DEADLOCK` | PLANNER_REVIEWER_DEADLOCK; PLANNER_STUBBORN_ROUND_LIMIT | "split feature into N sub-features" | Yes (abort feature) |
| `PLANNING_PREREQ_MISSING` | task_input_INFEASIBLE | "insert prerequisite work before this feature" | Yes (abort feature) |
| `PLANNING_ENV_FAIL` | PLANNER_HTTP_502_PERSISTENT; PLANNER_TIMEOUT; PLANNER_OUTPUT_TRUNCATED_EMPTY; PLANNING_CONTEXT_OVERSIZE; RATE_LIMIT_429 | "transport problem, switch model/transport" | Yes (abort feature) |
| `INFRA_INTERRUPTION` | WORKTREE_MERGE_CONFLICT; STATE_LOCK_CONTENTION; CTRL_C_INTERRUPTED | "infra issue, resume from snapshot" | No (must resume) |

**Not escalated (config errors, fail fast):**
- `PRD_FILE_NOT_FOUND`, `TASK_CARD_PATH_INVALID`, `VERIFY_CMD_MISSING`, `TASK_CARD_SCHEMA_INVALID`, `WORKFLOW_CHAIN_ARTIFACT_MISSING`

---

## 2. File Layout

### Phase-scoped decision files (3 pairs + 1 split-proposal)

```
planning_dir/
├── .planning_decision_request.json    # Phase=planning escalation request
├── .planning_decision_response.json   # Phase=planning user choice
├── .planning_decision_context.json    # Phase=planning escalation_count
├── .planning_split_proposal.json      # NEW: feature-split spec from Planner
├── .executor_decision_request.json    # Phase=executor escalation request
├── .executor_decision_response.json
├── .executor_decision_context.json
├── .gate_decision_request.json        # Phase=gate escalation request
├── .gate_decision_response.json
├── .gate_decision_context.json
└── .decide.lock                       # Advisory lock for `kodawari decide` concurrency
```

### Backwards-compat aliases (deprecated, ≥1 release)

```
.executor_redesign_request.json   → mirrors .executor_decision_request.json (gate_complexity kind only)
.executor_redesign_response.json  → mirrors .executor_decision_response.json
.executor_redesign_context.json   → mirrors .executor_decision_context.json
.user_redesign_decision.json      → unchanged (sticky decision for must_fix injection)
```

Old filename writes go through `escalation/legacy_compat.py` translator.

---

## 3. Unified Schema

### 3.1 Request file schema

```json
{
  "schema_version": "workflow.decision_request.v1",
  "escalation_kind": "PLANNING_DEADLOCK",
  "failure_code": "PLANNER_REVIEWER_DEADLOCK",
  "phase": "planning",
  "feature": "social_dual_page_split",
  "task_id": null,
  "failure_summary": "<message>",
  "context": {
    "round_count": 7,
    "blocking_findings_history": [2, 3, 7, 1, 3, 6],
    "last_plan_tasks_count": 6
  },
  "completed_task_ids": [],
  "escalation_count": 1,
  "max_escalations": 2,
  "max_split_depth": 2,
  "current_split_depth": 0,
  "issued_at": "2026-05-15T...",
  "consumed_at": null
}
```

### 3.2 Response schema (unified across phases)

```json
{
  "schema_version": "workflow.decision_response.v1",
  "phase": "planning",
  "escalation_kind": "PLANNING_DEADLOCK",
  "action": "accept",          // "accept" | "skip" | "custom" | "abort"
  "option_index": 0,           // for accept
  "option": {...},             // chosen option full data (for resume)
  "description": "...",        // for custom
  "consumed_at": "2026-05-15T..."
}
```

### 3.3 Split-proposal schema (NEW, for PLANNING_DEADLOCK accept)

```json
{
  "schema_version": "workflow.split_proposal.v1",
  "parent_feature": "social_dual_page_split",
  "parent_split_depth": 0,
  "sub_features": [
    {
      "name": "social_schema_migration",
      "depends_on": [],
      "task_summary": "Migrate social_thread_snapshots to add cluster_id / kol_*",
      "prd_excerpt": "...",
      "approximate_task_count": 3
    },
    {
      "name": "social_kol_scrapers",
      "depends_on": ["social_schema_migration"],
      "task_summary": "X retweet + Reddit cross-post extractors",
      "prd_excerpt": "...",
      "approximate_task_count": 4
    },
    {
      "name": "social_event_page",
      "depends_on": ["social_schema_migration", "social_kol_scrapers"],
      "task_summary": "New /events/{id}/social API + frontend",
      "prd_excerpt": "...",
      "approximate_task_count": 4
    }
  ],
  "topological_validated": true,
  "max_depth_check": {"current": 0, "limit": 2, "allowed": true}
}
```

---

## 4. Code Changes (FINAL clean list)

### 4.1 New files

| Path | Purpose | LOC |
|---|---|---|
| `autopilot/escalation/__init__.py` | Module entry, re-exports public API | 30 |
| `autopilot/escalation/kinds.py` | `EscalationKind` enum + `classify(failure_event, phase) → kind` | 120 |
| `autopilot/escalation/handler.py` | `maybe_escalate()` / `write_decision_request()` / `read_decision_response()` / `consume_decision()` — uses `path_lock()` + `atomic_write_json()` from `infra/io_atomic.py` | 300 |
| `autopilot/escalation/planner_prompts.py` | 12 kind-specific Planner prompts | 250 |
| `autopilot/escalation/policy.py` | Load `escalation_policy.yaml` if exists; default behavior per kind | 80 |
| `autopilot/escalation/budget.py` | `EscalationBudget` cost tracker (reads `WORKFLOW_ESCALATION_BUDGET_USD`) | 60 |
| `autopilot/escalation/legacy_compat.py` | Read/write `.executor_redesign_*` ↔ new schema translator | 100 |
| `tests/autopilot/escalation/` | Parametrized fixtures + 15 test functions × 8 params | 600 |

### 4.2 Modified files

| Path | Change |
|---|---|
| `cli/runtime/decide_cmd.py` | Rewrite: kind dispatcher, per-kind Planner prompt routing, support 3 phases; `--abort` + `--status` flags. **Old code kept as `decide_cmd_legacy.py` for ≥1 release** |
| `autopilot/engine/engine_recovery_mixin.py` | Replace `is_gate_complexity_exhausted` branch with `escalation.handler.maybe_escalate(failure_event, phase="executor")`; old function re-exported as deprecated alias |
| `autopilot/engine/engine_session_mixin.py` | Resume: detect all 3 phase decision_response files; apply per-kind resume action |
| `autopilot/planning/planning_orchestrator.py` | Replace escalation_required exit with `maybe_escalate(planning_diagnostics, phase="planning")`; preserve `.planning_failure.json` for audit |
| `autopilot/gate/gate_engine.py` | Add `maybe_escalate(gate_check, phase="gate")` hook after GATE_BLOCKED |
| `cli/runtime/autopilot_cmd.py` | Resume logic: detect SUPERSEDED parent, walk sub_features in topological order |
| `cli/runtime/gate_config_cmd.py` | `apply-threshold-adjust` subcommand for GATE escalation auto-skip path |
| `gui/redesign_chooser.py` | Per-kind UI: tree view for split, list for refactor, etc. |
| `cli/parser_registry.py` | Register `decide --abort` + `decide --status` |

### 4.3 Deprecated files (retain 1 release with warnings)

| Path | Behavior |
|---|---|
| `autopilot/recovery/escalation_handler.py` | Imports + warns; re-routes to `autopilot/escalation/handler.py` via `legacy_compat.py` |
| Old `.executor_redesign_*` file writes | Continue mirroring during transition (dual-write) |

---

## 5. Resume Semantics (BLOCKING fix from review)

### 5.1 Feature split (PLANNING_DEADLOCK accept)

1. `decide` writes `.planning_split_proposal.json` with `topological_validated: true`
2. autopilot resume detects proposal:
   - Mark parent feature `SUPERSEDED_BY_SPLIT` in state.json (read-only audit)
   - For each sub_feature in topological order: `mkdir planning/<sub_name>`, copy prd_excerpt, run autopilot non-interactively
   - **Sub-features start with fresh `escalation_count=0`** (do not inherit)
   - Sub-feature `current_split_depth = parent + 1`; abort if `> max_split_depth=2`
   - Cycle check: `_topological_sort(sub_features)` raises if cycle detected
3. Parent dir kept on disk with `SUPERSEDED.md` marker explaining split rationale

### 5.2 Skip semantics per phase

| Phase | "skip" action | Side effects |
|---|---|---|
| Planning | Abort entire feature | State `final_status=SKIPPED_BY_USER`; no sub-features spawned |
| Executor | Drop one task, advance cursor | `completed_tasks` includes task with `outcome=SKIPPED`; next task starts normally |
| Gate | Lower threshold then retry once | Persist threshold change in `gate_policy.yaml`; if still blocked → final BLOCKED |

### 5.3 Custom action semantics per phase

| Phase | "custom" action | Example |
|---|---|---|
| Planning | User-written feature description | "split into 2 features: X handles schema, Y handles UI" |
| Executor | User-written must_fix instruction | "use existing utility lib X instead of inlining" |
| Gate | User-written refactor instruction | (current gate_complexity path) |

### 5.4 Concurrent escalations (multiple files exist)

Resume processes in priority: **planning > executor > gate**. Suppressed files retain `consumed_at: null` and are processed in next resume tick.

---

## 6. Test Migration Plan

### Phase 1 (Step 1-2): NEW tests only, OLD tests intact
- Add parametrized `test_escalation_unified.py` with 8 kinds × 4 actions = ~32 test cases
- Don't touch `test_escalation_integration.py` (which tests old gate_complexity path)

### Phase 2 (Step 5-6): Dual-write verification
- Add `test_escalation_legacy_compat.py`: verify old `.executor_redesign_request.json` is written when new path runs
- `test_escalation_integration.py` continues to pass (reads old filenames)

### Phase 3 (Step 7-8): Legacy path retirement
- Mark `test_escalation_integration.py` with `@pytest.mark.deprecated`; retain for 1 release
- Add new authoritative tests under `tests/autopilot/escalation/`

---

## 7. Risk Register (FINAL)

| Risk | Mitigation |
|---|---|
| LLM cost explosion (≤ 4 LLM calls per escalation × 12 kinds) | `WORKFLOW_ESCALATION_BUDGET_USD` env var (default $10/feature); abort beyond |
| Test combinatorics | Parametrized fixtures: 15 functions × 8 params ≈ 120 effective tests via `pytest.mark.parametrize` |
| Concurrent `decide` + autopilot | Mandatory `path_lock()` on every decision file write; `.decide.lock` advisory lock |
| `decide` crash mid-flight | `atomic_write_json()` for all decision file writes (tmp → os.replace) |
| Sub-feature recursive split deadlock | `max_split_depth=2` enforced; topological cycle check before split |
| Ctrl+C during escalation | SIGINT handler writes `.decide.aborted` marker; resume detects + clears |
| `WORKFLOW_ESCALATION_LEGACY=1` rollback path | Env var routes all escalation through `legacy_compat.py` (old code path); retain ≥1 release |
| Failed migration of `is_gate_complexity_exhausted` callers | Function kept as deprecated alias for ≥1 release; emits `DeprecationWarning` |
| Audit trail | Parent SUPERSEDED dir retained with full plan/code artifacts; never deleted |

---

## 8. Staged Rollout (8 steps, each 2-3 hours)

| Step | Scope | Test gate |
|---|---|---|
| 1 | Create `autopilot/escalation/` module + `kinds.py` + classify() + 30 unit tests | All new tests pass |
| 2 | Implement `handler.py` with phase=executor branch (parallel to legacy) | Legacy tests still pass + new tests pass |
| 3 | Implement `handler.py` with phase=planning branch + `planning_orchestrator` hook | Add 1 integration test reproducing social_dual_page_split deadlock → escalate → decide |
| 4 | Implement `handler.py` with phase=gate branch + `gate_engine` hook for non-complexity codes | Add 1 integration test for file_length escalation |
| 5 | Rewrite `decide_cmd.py` with kind dispatcher + 12 prompts + `--abort` + `--status` | All decide unit tests + 3 e2e per phase |
| 6 | Autopilot resume logic: feature split + skip + custom | Integration test for split: parent → 3 sub-features → all complete |
| 7 | Migrate legacy `escalation_handler.gate_complexity` path to use new system; keep dual-write | All 141 regression tests + new tests pass; legacy filenames still produced |
| 8 | Add deprecation warnings + `WORKFLOW_ESCALATION_LEGACY=1` toggle; update docs | Final regression sweep |

**Total: ~24 hours including tests.**

---

## 9. What this does NOT change

- Existing ReadCache v3.1 behavior
- Existing `gate_complexity` escalation user-facing flow (just routed through new handler)
- `models.yaml` schema
- `TASK_GRAPH.json` / `TASK_CARD_ACTIVE.json` schemas
- `infra/io_atomic.py` (reused as-is)
- 141 existing regression tests (must continue to pass)

---

## 10. Pending decisions (for user)

- Approve cost cap default ($10/feature)? Or set differently?
- Approve `max_split_depth=2`? Some teams may want 3 for very large initiatives
- Approve 8-step staged rollout, or do all-at-once? (8 steps safer, ~3 days vs ~1 day)

# kodawari Capability Map

**Last audited:** 2026-04-17 (verify scoping + tiered redline alignment)
**Audience:** any agent (Claude, Codex, GPT, human reviewer) starting work on this repo.
**Rule:** read this file **before** proposing refactors or "new features". Many
capabilities already exist but are not wired to every backend.

## Why this document exists

This repo has 50+ modules under `src/kodawari/autopilot/`. The existing
`backend_capability_truth` descriptors use `{descriptor_value, runtime_state}`
which does not distinguish between "orchestrator has the primitive" and
"this backend actually calls the primitive". This ambiguity caused external
agents (Claude, GPT) to recommend re-implementing features that already exist.

This document is the **authoritative mapping** from capability → orchestrator
module → which backends actually use it.

## Legend

| Symbol | Meaning |
|--------|---------|
| ✅ wired | capability is actively called on this execution path |
| ⚙️ available | module exists at orchestrator layer but this path does not call it |
| ❌ absent | capability not implemented anywhere yet |
| 🚧 partial | called in some code paths, not in the single-task main path |

## Capability × Execution Path Matrix

| Capability | Module | codex_cli (single-task) | claude_code (single-task) | parallel_coordinator | notes |
|------------|--------|-------------------------|---------------------------|----------------------|-------|
| Directory isolation per worker | [worktree_manager.py](../src/kodawari/autopilot/worktree_manager.py) | ⚙️ available | ⚙️ available | ✅ wired | parallel worker allocation (mkdir); **not** `git worktree` |
| Single-task isolation workspace | [execution_isolation.py](../src/kodawari/autopilot/execution_isolation.py) | ✅ wired (default on; opt-out via `WORKFLOW_CODEX_ISOLATION=0` or `isolation_workspace=False`) | ✅ wired (always) | N/A | **Phase B + hardening**: per-task copy of project_root; isolation-aware request file written to workspace so subprocess reads correct project_root; allowed_files sync back on success only; failure leaves workspace for debugging |
| File-level rollback checkpoint | [rollback.py](../src/kodawari/autopilot/rollback.py) | ✅ wired via `gate_round._maybe_rollback` | ✅ wired via `gate_round._maybe_rollback` | ⚙️ available | snapshot + git-dirty diff; `git checkout` is deliberately skipped to avoid overwriting concurrent edits |
| Parallel worker planning | [parallel_coordinator.py](../src/kodawari/autopilot/parallel_coordinator.py) | ⚙️ available | ⚙️ available | ✅ wired | only activates when `--parallel-workers > 1` |
| Multi-worker merge semantics | [merge_semantics.py](../src/kodawari/autopilot/merge_semantics.py) | ⚙️ available | ⚙️ available | ✅ wired | |
| Planner/reviewer path resolution | [planning_context.py](../src/kodawari/autopilot/planning_context.py) (`resolve_plan_paths`) | N/A (runs before execution) | N/A | N/A | two-layer: canonical hints + auto-resolver |
| Deterministic review precheck | [review_precheck.py](../src/kodawari/autopilot/review_precheck.py) | ✅ wired via local_adapter | ✅ wired via local_adapter | ✅ wired | includes scope-conflict gate |
| Test-file detection (path-aware) | `review_precheck._is_test_file` | ✅ wired | ✅ wired | ✅ wired | path-segment + suffix; **do not** use substring `"test"` heuristics |
| Review scope conflict gating | [local_adapter.py](../src/kodawari/autopilot/local_adapter.py) + [review_precheck.apply_deterministic_review_guard](../src/kodawari/autopilot/review_precheck.py) | ✅ wired | ✅ wired | ✅ wired | `REVIEW_SCOPE_CONFLICT` precedes `REVIEW_FIX_REQUIRED` |
| Execution command guard (deny/allow/ask) | [execution_guard.py](../src/kodawari/autopilot/execution_guard.py) | ✅ wired | ✅ wired | ✅ wired | 3-tier: deny > explicit allow (safe diagnostic tools) > ask; newline/null sanitization against bypass |
| Three-tier permission policy | [permission_policy.py](../src/kodawari/autopilot/permission_policy.py) + [permission.default.yaml](../src/kodawari/safety/policies/permission.default.yaml) | ✅ wired | ✅ wired | ✅ wired | **Phase D → E(a)**: declarative `block`/`prompt`/`allow` by `tool` + `path_glob`. `find_blocked_writes()` is called post-execution in `engine_implementation_mixin` for every round's `changed_files`; a BLOCK-tier hit stops the loop with `PROTECTED_FILE_BLOCK` regardless of isolation mode. Isolation sync-back still independently filters on the isolation-workspace path. |
| Prompt injection classifier | — | ❌ absent | ❌ absent | ❌ absent | Phase E(b) — **separated from Phase E(a)**. Needs independent-model or API-key gateway; runtime cost + latency; deferred until the advisor layer (F1/F2) has production evidence |
| JIT context tool loop | [planning_agent.py](../src/kodawari/autopilot/planning/planning_agent.py) `_build_command` + `_build_prompt` | N/A (planner-only) | N/A | N/A | Planner CLI invoked with `--allowedTools Read,Grep,Glob`; planner cwd = project_root; default on for trusted workspaces, disable via `WORKFLOW_PLANNER_JIT_CONTEXT=0`. Prompt teaches model about read-only tool access and v1.1 test-mutation fields. |
| Planner failing-baseline probe | [planning_context.py](../src/kodawari/autopilot/planning/planning_context.py) `collect_failing_baseline` | N/A (planner-only) | N/A | N/A | Default on. Runs bounded pytest collect/run only for explicitly mentioned existing tests and injects a `Failing Baseline Probe` block into planner context; disable via `WORKFLOW_PLANNER_BASELINE_PROBE=0`. |
| Planner/reviewer auth double-mode | [local_adapter.py:72](../src/kodawari/autopilot/local_adapter.py#L72) (`opus_gateway_api_key` + `base_url`) | ✅ subscription via `claude -p` CLI | ✅ subscription via `claude` CLI | ✅ wired | default path = subscription CLI; API-key gateway is opt-in |
| Streaming planner progress | — | ❌ absent (black-box subprocess until exit) | ❌ absent | ❌ absent | only coarse `[planning] round N: ...` lines; Phase A1+ streaming considered |
| Subprocess error classification | [planner_errors.py](../src/kodawari/autopilot/planner_errors.py) | ✅ wired | ✅ wired | ✅ wired | **Phase A1**: closed enum (`NESTED_SESSION`, `AUTH_FORBIDDEN`, `AUTH_MISSING`, `MAX_TURNS`, `API_TIMEOUT`, `API_ERROR`, `TIMEOUT`, `EXECUTABLE_MISSING`, `EMPTY_OUTPUT`, `INVALID_JSON`, `UNKNOWN`); every diagnosis carries remediation hint |
| task-run terminal state sync | [task_run_state_sync.py](../src/kodawari/cli/task_run_state_sync.py) | ✅ wired | ✅ wired | N/A | **Phase A2**: after task-run completes, writes `current_stage=COMPLETED` + final_status + stop_reason + last_error into `.autopilot_state.json` so `kodawari status` reports real truth (was leaking stale RUNNING) |
| Verify default command baseline | [execution_backend.verify_expectation_text](../src/kodawari/autopilot/execution_backend.py) | ✅ wired | ✅ wired | ✅ wired | default request remains `pytest -q`; runtime may narrow targets before execution |
| Scoped verify (pytest on affected tests) | [verify_targeting.py](../src/kodawari/autopilot/verify_targeting.py) + [runtime_checks.py](../src/kodawari/autopilot/runtime_checks.py) | ✅ wired | ✅ wired | ✅ wired | supports changed test files, source-derived test mapping, task keyword `-k`, and instinct hint fallback |
| Verify failure analyzer | [failure_analyzer.py](../src/kodawari/autopilot/verify/failure_analyzer.py) + [gate_round.py](../src/kodawari/autopilot/engine/gate_round.py) | ✅ wired | ✅ wired | ✅ wired | Default on. Classifies pytest failures into Tier A stale literal vs Tier B implementation/environment failures during fix_round; authorized Tier A requires `allowed_test_mutations`; disable via `WORKFLOW_VERIFY_ANALYZER=0`. |
| Schema-validated artifact chain | [contract_first_schema.py](../src/kodawari/cli/contract_first_schema.py) + [schemas/contract_first/](../src/kodawari/schemas/contract_first/) | ✅ wired | ✅ wired | ✅ wired | 6 artifact types; all draft-07 validated |
| Decision interaction state machine | [autopilot_decision_runtime.py](../src/kodawari/cli/autopilot_decision_runtime.py) | ✅ wired | ✅ wired | ✅ wired | `decision_request_present` = **pending**, not "file exists" |
| Human-approve CLI (`kodawari approve`) | [approve_cmd.py](../src/kodawari/cli/approve_cmd.py) | ✅ wired | ✅ wired | N/A | Reads `.decision_request.json`, writes `.decision_response.json` + appends to `.decision_history.json`; defaults to `recommended_option`; `--option`, `--rationale`, `--force`, `--planning-dir` flags |
| Contract-first planning (planner+reviewer) | [planning_orchestrator.py](../src/kodawari/autopilot/planning_orchestrator.py) + [planning_agent.py](../src/kodawari/autopilot/planning_agent.py) + [plan_reviewer.py](../src/kodawari/autopilot/plan_reviewer.py) | N/A | N/A | N/A | 3-round peer-review loop; claude + codex subprocess |
| Instinct learning | [instincts/](../src/kodawari/instincts/) | ✅ wired | ✅ wired | ✅ wired | pattern accumulation from past failures; project store at `<project>/.workflow/instincts.json`, distinct-run promotion (PR2.5), error_code-gated learnable filter (PR3) |
| Cross-project instinct global store | [instincts/global_store.py](../src/kodawari/instincts/global_store.py) | ✅ wired | ✅ wired | ✅ wired | High-confidence portable LearnedInstincts (error_code-keyed) auto-publish to `~/.kodawari/instincts.json`; `WORKFLOW_INSTINCTS_GLOBAL_PATH` env override; atomic write + path lock; project hints win on conflict (PR4) |
| Model-advised instinct pattern | [model_advisor.py](../src/kodawari/autopilot/model_advisor.py) | ✅ wired (default-off) | ✅ wired (default-off) | ⚙️ available | **Phase F1**: Sonnet 4.6 suggests glob pattern at first promotion; heuristic fallback if advisor not configured. Gate: `ANTHROPIC_API_KEY` (or `WORKFLOW_ADVISOR_API_KEY`) + `WORKFLOW_MODEL_ADVISOR≠0`. |
| Model-driven compact compression | [model_advisor.py](../src/kodawari/autopilot/model_advisor.py) + [semantic_compact.py](../src/kodawari/autopilot/semantic_compact.py) | ✅ wired (default-off) | ✅ wired (default-off) | ⚙️ available | **Phase F2**: Sonnet compresses must_fix/recent_errors when count exceeds budget (>5 / >3); payload stamped `compact_source: model_compressed`. Heuristic (no compression) when advisor not configured. |
| Tiered redline enforcement | [profiles.py](../src/kodawari/gate/profiles.py) + [policy_loader.py](../src/kodawari/gate/policy_loader.py) | ✅ wired | ✅ wired | ✅ wired | canonical thresholds come from `code-redline`; advisory/blocking/tiered enforce complexity + file-complexity tiers; `strict` is a compatibility alias of `blocking` |

## Known gaps that look like bugs but are design choices

1. **`rollback.py` does not `git checkout` unsnapshot files.** Comment at
   `rollback.py:137` explains: parallel/user edits could be clobbered. If you
   want `git worktree` + disposable workspace semantics, that is a separate
   feature (not a rollback fix).

2. **`directory_isolation` is `mkdir`, not `git worktree`.** The name is
   literal. Do not assume git-level isolation.

3. **`decision_request_present = pending`, not "file on disk".** Per
   contract (`二、运行操作、门禁规则与后续路线.md` §403). A responded decision
   reports `decision_request_present = False` even though the JSON file is
   still on disk.

4. **`runtime_state: "planned"` in capability descriptors.** Reflects the
   **backend's native support**, not whether the orchestrator has the
   primitive. This document is the authoritative answer.

## If you are an agent starting work

1. If you want to add capability X, **search this table first**.
2. If the row says ⚙️ available for your backend, the work is "wire it in",
   not "build it". Tell the user this explicitly.
3. If the row says ❌ absent, check the phase plan below before assuming it
   is a greenfield feature.
4. When you finish any capability work, **update this file in the same PR**.

## Hardening wave (2026-04-16)

| Change | Where | What changed |
|--------|-------|--------------|
| Isolation default-on | `execution_codex_cli._codex_isolation_enabled` | codex_cli isolation now defaults to ON; opt-out via `WORKFLOW_CODEX_ISOLATION=0` or `isolation_workspace=False`; isolation-aware request file written to workspace |
| Parallel file conflict detection | `planning_agent._parallel_file_conflicts` | `_validate_plan()` now detects tasks that can run in parallel but claim the same `files_to_change` paths |
| Gate hook attempt field | `gate_round._gate_attempt_number` | `pre_gate`, `post_gate` hook payloads and round_record `details` now include `gate_attempt` (1-based counter) for observability |
| Compact dedup | `semantic_compact._compact_is_unchanged` | `materialize_semantic_compact` skips I/O write when key dynamic fields are identical to existing file |
| Evidence dedup | `engine_review_mixin._append_review_deduped` | `peer_reviews` list skips duplicate entries with the same `review_iteration` |
| Review override transparency | `engine_review_mixin._default_review_feedback` | Logs `WARNING review_override:` and stamps `review_source: simulated_default` when no real adapter is available |
| Guard allowlist tier wired | `safety/execution_guard.evaluate_execution_guard` | `allow` rules now evaluated between `deny` and `ask`; default policy populated with safe diagnostic patterns (`pytest`, `git status`, etc.) |
| Planner ACTIVE WORKSPACE ROOT guardrail | `planning_agent._build_prompt` | prompt now injects the resolved `project_root` as an **ACTIVE WORKSPACE ROOT** declaration and tells planner to ignore foreign absolute paths seen in CLAUDE.md / docs. Fixes reviewer blocking on cross-project path leakage |
| Planner forbid-meta-tasks rule | `planning_agent._build_prompt` | Hard constraint: tasks with empty `files_to_change` or whose sole purpose is running a verify script (pytest, check_code_redlines.py) are forbidden — those belong in `verify_recipes[]`, not `tasks[]` |
| Verify-command fail-fast | `planning_agent._validate_verify_commands` | `_validate_plan()` now inspects `verify_recipes[].command` and `tasks[].verify_cmd` for Windows-style absolute paths that escape the active workspace root; rejects at validator layer before reviewer has to catch it |
| Planner bundle impl+test rule | `planning_agent._build_prompt` | Hard constraint: a task editing a source file MUST include its corresponding test file in the same `files_to_change`. Matches `review_precheck._test_scope_available()` which checks scope per-task — splitting impl (T1) and test (T2) into separate tasks triggered `REVIEW_SCOPE_CONFLICT` in realworld run-3 |
| Verify `-k` keyword quoting | `verify_targeting._scoped_pytest_cmd` | Wraps the `-k` expression in double quotes with embedded-quote escaping so multi-word boolean expressions (`"foo and bar"`) stay intact when pytest receives them. Previously `-k foo and bar` was emitted unquoted |
| Verify argv shell-safe parsing | `verify_execution._command_payload` | Uses `shlex.split(..., posix=False)` to tokenize verify commands instead of `str.split()`, then strips outer quotes. Plain split destroyed quoted `-k` arguments; pytest saw `and`/`bar` as positional files and failed with `ERROR: file or directory not found: and` |
| Gate CLI scope selection | `cli/gate_cmd._resolve_gate_targets` + `--scope` flag | `kodawari gate` now defaults to **`--scope=auto`**: reads `changed_files` from the planning dir's `.execution_result.json` when present; falls back to full project only when no evidence exists. Explicit `--scope=full` forces whole-project scan (pre-release audit); `--scope=changed` hard-fails if no `.execution_result.json` is found. Explicit `--path` still wins over `--scope`. Payload now carries `scope_used` + `scope_source` for observability |
| Planner auth regex tightened | `planner_errors._AUTH_403_PATTERNS` | Removed `r"authentication"` from the fallback regex. The word appears in legitimate plan/result content (auth endpoints, middleware, tokens) and caused false-positive AUTH_FORBIDDEN classification for unrelated API errors. Only `\\b403\\b`, `Request not allowed`, and `forbidden` remain — narrower and more discriminative |

## Phase landings

| Phase | Target | Status | Commits |
|-------|--------|--------|---------|
| A1 | Subprocess error classification | ✅ landed | `b930e6c` |
| A2 | `kodawari status` terminal state truth | ✅ landed | `b930e6c` |
| B | codex_cli opt-in directory isolation + execution_isolation shared module; descriptor flips | ✅ landed | (this wave) |
| C | JIT context tool loop: planner CLI `--allowedTools Read,Grep,Glob` + cwd=project_root + prompt hint; env gate `WORKFLOW_PLANNER_JIT_CONTEXT` | ✅ landed | (this wave) |
| D | Declarative three-tier permission policy (`permission_policy.py` + `permission.default.yaml`); tier matcher + rule loader; not yet wired to execution pipeline | ✅ landed | (this wave) |
| E(a) | Permission policy wired into post-execution pipeline: `find_blocked_writes()` call in `engine_implementation_mixin` gates every round's `changed_files` through `permission.default.yaml` BLOCK tier; covers non-isolation path | ✅ landed | (this wave) |
| E(b) | Prompt injection classifier (depends on independent-model or API-key gateway) | 📋 planned | — |
| F1 | Model-advised instinct pattern suggestion (Sonnet 4.6; default-off; `ANTHROPIC_API_KEY` gate + heuristic fallback) | ✅ landed | (this wave) |
| F2 | Model-driven compact compression (Sonnet 4.6; default-off; fires when must_fix>5 or errors>3; heuristic fallback) | ✅ landed | (this wave) |
| A1+ | Streaming planner progress (token-level) | 📋 planned | — |

## Update protocol

- Any commit that adds/wires/deletes a row above must update this file.
- If a descriptor `runtime_state` is flipped, record the commit SHA and date.
- Do not use this document as release notes; use commit messages for that.

## Related documents

- [CLAUDE.md (workspace root)](../../CLAUDE.md) — working agreement and
  blocking rules
- [newsapp/docs/开发交付现状.md](../../newsapp/docs/开发交付现状.md) — feature
  delivery status
- [newsapp/docs/任务计划_v1.1.md](../../newsapp/docs/任务计划_v1.1.md) — task plan

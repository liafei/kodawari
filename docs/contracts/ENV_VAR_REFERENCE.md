# ENV VAR REFERENCE

> Comprehensive index of environment variables consumed by kodawari runtime
> code. For the deprecated `WORKFLOW_OPUS_*` → `WORKFLOW_REVIEWER_*` rename
> migration table, see [ENV_VAR_MIGRATION.md](ENV_VAR_MIGRATION.md).

---

## Reviewer

| Variable | Default | Purpose |
|---|---|---|
| `WORKFLOW_REVIEW_ENABLED` | `0` | `1` to enable real peer review via the reviewer gateway. |
| `WORKFLOW_REVIEW_REQUIRED` | `0` | `1` to make peer review a hard requirement (fail-closed when reviewer unreachable). |
| `WORKFLOW_REVIEWER_BASE_URL` | `""` | Reviewer gateway base URL. |
| `WORKFLOW_REVIEWER_API_KEY` | `""` | Reviewer gateway API key. |
| `WORKFLOW_REVIEWER_MODEL` | `""` | Reviewer model name/ID. |
| `WORKFLOW_REVIEWER_API_FORMAT` | `""` | Wire format: `openai` or `anthropic`. |
| `WORKFLOW_REVIEWER_BACKEND` | `auto` | Backend selector: `api`, `cli`, `codex`, `auto`. |
| `WORKFLOW_REVIEWER_EXECUTABLE` | `""` | Generic fallback executable for the reviewer. |
| `WORKFLOW_REVIEWER_CLAUDE_EXECUTABLE` | `""` | Executable override for the Claude/CLI reviewer backend. |
| `WORKFLOW_REVIEWER_CODEX_EXECUTABLE` | `""` | Executable override for the Codex reviewer backend. |
| `WORKFLOW_REVIEWER_TIMEOUT` | `300` | Plan-reviewer subprocess timeout (seconds). |

## Planner

| Variable | Default | Purpose |
|---|---|---|
| `WORKFLOW_PLANNER_MODEL` | `""` | Model override for the Claude planner subprocess. |
| `WORKFLOW_PLANNER_JIT_CONTEXT` | `1` | Claude planner Read/Grep/Glob access during planning. Set to `0`/`false`/`off` to disable. |
| `WORKFLOW_PLANNER_BASELINE_PROBE` | `1` | Run a bounded pre-planning pytest probe for explicitly mentioned tests. |
| `WORKFLOW_VERIFY_ANALYZER` | `1` | Add Tier A/B verify-failure analysis to fix rounds. |
| `WORKFLOW_PLANNING_AUTO_ACCEPT` | `1` | Auto-accept `PLANNING_APPROVAL_REQUIRED` decisions (opt-out via `0`). |

## Executor

| Variable | Default | Purpose |
|---|---|---|
| `WORKFLOW_EXECUTOR_TIMEOUT_SECONDS` | `600` | Max wall-clock seconds for a single executor round (per-round, NOT whole-loop — see autopilot's `--max-wall-clock-seconds` flag). |
| `WORKFLOW_SELF_REVIEW_BACKEND` | `""` | Backend for post-execution self-review: `codex`, `claude`, or `""` (disabled). |
| `WORKFLOW_TASK_CARD_V1_1` | `0` | Set to `1` to emit task_card schema v1.1 (preserves additional fields). |

## Self-Repair / Fidelity Gates

| Variable | Default | Purpose |
|---|---|---|
| `WORKFLOW_FIDELITY_GATE` | `0` | Set to `1` to enable post-implement fidelity gate (compares plan against actual file edits). |

## Testing Hooks

| Variable | Default | Purpose |
|---|---|---|
| `WORKFLOW_SDK_TEST_MODE` | unset | Set to `1` in unit/state-machine smokes; loosens production-strict gates such as `_no_fake_run_strict`. |
| `PYTEST_CURRENT_TEST` | (set by pytest) | Used by `_no_fake_run_strict` to detect test contexts and disable fail-closed silent-pass guards. |

## Cross-Reference

- Per-round vs whole-loop budget: `WORKFLOW_EXECUTOR_TIMEOUT_SECONDS` (per
  round) and `--max-wall-clock-seconds` (whole loop) compose; the smaller one
  wins for any individual round, the larger one is the absolute upper bound
  for the entire autopilot invocation.
- Deprecated OPUS-prefixed vars: see [ENV_VAR_MIGRATION.md](ENV_VAR_MIGRATION.md).
- Reviewer mode gate: `_no_fake_run_strict` fires when
  `WORKFLOW_REVIEW_ENABLED=1` AND `PYTEST_CURRENT_TEST` is unset AND
  `WORKFLOW_SDK_TEST_MODE` is unset — that's "production strict mode" for
  fake-run policy enforcement.

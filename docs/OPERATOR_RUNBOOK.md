# kodawari Operator Runbook

This runbook is for maintainers operating CI lanes, real-review integrations,
release checks, and local recovery. For the user path, start with
[USER_GUIDE.md](USER_GUIDE.md).

## Operating Model

`kodawari` has four execution modes that must not be confused:

| Mode | What Runs | What It Proves |
|---|---|---|
| `noop_test_only` smoke | test shims and local artifact writers | state-machine wiring only |
| `simulate_local` / degraded review | local fallback review behavior | loop resilience, not real peer approval |
| real CLI review | logged-in CLI reviewer via configured executable | subscription CLI path works |
| API-key gateway review | opt-in HTTP gateway with API key | integration backend works |

Do not report `noop_test_only` or degraded review as a real end-to-end proof.

## Local Lane Commands

List the always-on lane without running it:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane.ps1 -ListOnly
```

Run the always-on lane:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane.ps1
```

Run the focused kodawari regression used by the refactor plan:

```powershell
$env:WORKFLOW_SDK_PYTEST_SUMMARY_JSON = "planning\pytest_summary_latest.json"
.\.workflow_runtime\local-env\.venv\Scripts\python.exe -m pytest `
  tests\test_code_health_snapshot.py `
  tests\test_no_duplicate_module_paths.py `
  tests\test_import_rule.py `
  tests\test_public_api_contract.py `
  tests\test_artifact_schema_present.py -q
```

The pytest summary writer must record a nonzero `collected` count and matching
`passed` / `failed` / `skipped` counts.

## Integration Environment

Use the new env vars for reviewer integration:

```powershell
$env:WORKFLOW_REVIEWER_BACKEND = "api"
$env:WORKFLOW_REVIEWER_API_KEY = "..."
$env:WORKFLOW_REVIEWER_BASE_URL = "https://..."
```

Legacy `WORKFLOW_OPUS_*` variables are compatibility fallbacks and emit
`DeprecationWarning`. They are scheduled for removal after 2026-11-01. See
[contracts/ENV_VAR_MIGRATION.md](contracts/ENV_VAR_MIGRATION.md).

When integration credentials are absent, integration lanes should report a
structured `SKIP`. Use `-FailIfSkipped` only when the environment is expected to
contain credentials.

## Real Executor Checklist

Before running a real executor:

- confirm `kodawari --help` shows the stable user surface
- confirm the target project is the intended `--project-root`
- confirm task scope is represented in task-card `files_to_change`
- confirm rollback policy for dirty files
- confirm the reviewer backend is real when `--real-peer-review` is set

For Claude subscription CLI runs, authenticate first:

```powershell
claude auth login
```

The API-key gateway is opt-in. Do not replace the subscription CLI path with an
SDK/API path unless the requested workflow explicitly needs it.

## Status And Recovery

Use:

```powershell
kodawari status --project-root E:\path\to\project --feature FEATURE
```

Common states:

| Signal | Operator Action |
|---|---|
| `AWAITING_DECISION` | run `kodawari approve` or provide the requested decision |
| auth missing / forbidden | authenticate the CLI or configure reviewer API env vars |
| `REVIEW_SCOPE_CONFLICT` | update the task card so implementation and tests are in scope |
| `PROTECTED_FILE_BLOCK` | remove blocked writes or update permission policy through review |
| verify failure | inspect verify report; implementation/environment fixes come before test mutation |
| release approval | collect release evidence, then approve or stop |

## Artifact Rules

All machine artifacts written by the SDK must include `schema_version`.
Compatibility follows `<artifact>.v<MAJOR>.<MINOR>`:

- added fields: minor version bump
- removed fields or changed semantics: major version bump
- old major readers remain for at least 90 days

Run:

```powershell
.\.workflow_runtime\local-env\.venv\Scripts\python.exe -m pytest tests\test_artifact_schema_present.py -q
```

## Code Health Baseline

Refresh code-health metrics after structural refactors:

```powershell
.\.workflow_runtime\local-env\.venv\Scripts\python.exe scripts\snapshot_code_health.py --project-root . --output planning\baseline_v2_post_hygiene.json
Copy-Item -LiteralPath planning\baseline_v2_post_hygiene.json -Destination planning\code_health_baseline.json -Force
```

Current 9-point gates:

- `files_large_and_complex_block = 0`
- `files_over_1500_lines = 0`
- `files_over_1000_lines <= 8`
- `files_large_and_complex_warn <= 8`
- `runtime_contract_scatter_conflicts <= 4`
- `layer_boundary_violations = 0`

## Security And Release Audit

Run security configuration checks:

```powershell
.\.workflow_runtime\local-env\.venv\Scripts\python.exe -m pytest tests\test_security_scan_config.py -q
```

Run release package audit after building a distribution directory:

```powershell
.\.workflow_runtime\local-env\.venv\Scripts\python.exe scripts\release_audit.py .workflow_runtime\release-dist
```

Release audit must not include runtime homes, credentials, planning leftovers,
large evidence logs, or unapproved fixtures in the package.

## Desktop Operations

Desktop telemetry is off by default. It only turns on when
`WORKFLOW_DESKTOP_TELEMETRY` is explicitly set to `1`, `true`, `yes`, or `on`.
The Tauri shell resolves `kodawari` by `WORKFLOWCTL_BIN`, the repo-local
venv, a legacy developer venv, then `PATH`.

## Error Code Index

When a CLI command exits non-zero it emits a JSON payload with an `error_code`
field. The following codes are stable and indexed here so an operator can
identify the root cause without grepping source. Codes are kept generic — the
remediation strings inside the payload are the most specific guidance.

### Planning / Init

| Code | Cause | First check |
|---|---|---|
| `init_invalid_arguments` | `kodawari init` called without an explicit archetype (or with `auto`). | Re-run with `--archetype <name>`. Valid: `fastapi_api`, `flask_api`, `django_web`, `node_api`, `react_web`, `fullstack_fastapi_react`, `fullstack_django_react`. |
| `planning_conversation_invalid` | Planner produced 0 tasks (typically an empty PRD or impossible scope). | Inspect `planning/<feature>/PLANNING_CONVERSATION.json`; widen PRD or split feature. |
| `task_card_invalid` | Generated card failed validation (missing fields, too many `files_to_change`). | Check `validation_errors` in the payload. Greenfield caps at 5 files; existing at 3. |
| `artifact_schema_version_invalid` | Planning artifact predates a schema bump. | Run `kodawari migrate-artifacts --project-root <root> --feature <feature>`. |
| `artifact_corrupt` | Quarantined a corrupt artifact. | Inspect the quarantine path in the payload; regenerate the artifact. |

### Execution / Review

| Code | Cause | First check |
|---|---|---|
| `LOCAL_DEFAULT_NOT_A_REVIEW` | Production-strict mode hit a local-default self-review path that isn't a real review. | The branch is fail-closed in strict mode by design — set `WORKFLOW_REVIEW_ENABLED=1` and configure a real reviewer backend, or run in non-strict (test/dev) mode. |
| `NOOP_FALLBACK_NOT_A_REVIEW` | Production-strict mode hit the noop fallback in `local_adapter.self_review`. | Same as above — configure a real reviewer. |
| `no_peer_review_ran` | `summarize_peer_review` saw zero peer-review entries. | Confirm `--real-peer-review` is passed AND `WORKFLOW_REVIEWER_API_KEY`/`BASE_URL` env vars are set. |
| `MAX_CYCLES_REACHED` | Engine exhausted `--max-cycles`. | Increase `--max-cycles`, or split the task. Inspect `runtime` payload for the stuck stage. |
| Wall-clock abort (exit 124, `ABORT_REPORT.json`) | `--max-wall-clock-seconds` exceeded. | Read `planning/<feature>/ABORT_REPORT.json`. Either increase budget or investigate which round took too long via `runtime.round_records`. |

### Stall Diagnostics (executor)

When the executor is stalling without writing files:

- `planning/<feature>/.execution_recovery_decision.json` exists → stall
  recovery triggered. Look at `recovery_card.detector_name`.
- `stall_counters.no_write_iterations > 10` → read-loop stall (model is
  reading but not writing). Confirm a model-family nudge policy is in
  effect via `planning/<feature>/.executor_runtime.json:nudge_policy`.
- `last_error: "openai: timeout"` → upstream gateway issue, NOT a workflow
  stall. Check the reviewer/executor transport rather than autopilot logic.

### Doctor

| Code | Cause | First check |
|---|---|---|
| `doctor preflight FAIL` (rc=2) | One or more static checks failed. | Run with `--output preflight.json` and inspect each FAIL entry's `remediation`. |

### Multi-slice diagnostics

When a PRD declares two or more `## Slice N:` markers, `work-all` runs in
multi-slice mode. Useful artifacts to inspect on failure:

- `planning/<feature>/.multi_slice_state.json` — overall progress:
  completed positions, current_position, status (`running` /
  `all_slices_complete` / `halted`).
- `planning/<feature>/slice_NN/` — per-slice planning_dir, contains the
  same artifact set a single-slice run produces.
- `planning/<feature>/slice_NN/PRD_SLICE.md` — the per-slice PRD that
  was actually fed to the planner; inspect if planner produced
  unexpected scope.

Common failure pattern: slice 0 passes, slice 1 plan step times out →
`.multi_slice_state.json:status="halted", current_position=1,
completed_positions=[0]`. Fix the underlying issue (often slice content
too vague), then re-run `kodawari work-all`; slice 0 is auto-skipped via
resume. Use `--force-rerun` to recompute all slices.

## Source Of Truth

- capabilities: [CAPABILITY_MAP.md](CAPABILITY_MAP.md)
- user workflow: [USER_GUIDE.md](USER_GUIDE.md)
- quick smoke: [QUICKSTART.md](QUICKSTART.md)
- public compatibility: [../STABILITY.md](../STABILITY.md)
- desktop distribution: [DESKTOP_DISTRIBUTION.md](DESKTOP_DISTRIBUTION.md)

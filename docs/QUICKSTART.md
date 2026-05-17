# kodawari Quickstart

This guide is for a fresh developer checkout on Windows PowerShell with
Python 3.11+ available.

## Install

```powershell
cd kodawari
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_kodawari.ps1 -SkipPipUpgrade
.\scripts\kodawari.ps1 --help
```

The repo-local environment is created under:

```text
.workflow_runtime/local-env/.venv/
```

That directory is local runtime state and must not be committed.

## 30 Second Demo: No Keys

This smoke proves the local state machine and artifact writers work. It uses
test shims, `noop_test_only` self review, and does not require API keys or a
logged-in AI CLI.

```powershell
$env:WORKFLOW_SDK_TEST_MODE = "1"
$env:WORKFLOW_SELF_REVIEW_BACKEND = "noop_test_only"
.\.workflow_runtime\local-env\.venv\Scripts\python.exe -m pytest tests\test_autopilot_codex_cli_smoke.py -q
```

Expected result:

```text
1 passed
```

This is not a real end-to-end executor/reviewer proof. It is a fast local
smoke for wiring, status, and canonical runtime artifacts.

## CLI Smoke

```powershell
.\scripts\kodawari.ps1 --help
.\scripts\kodawari.ps1 status --help
.\scripts\run_always_on_lane.ps1 -ListOnly
```

The lane list command prints the fixed always-on targets without executing the
full lane.

## 10 Minute Demo: Claude CLI Subscription

Prerequisites:

- Python 3.11+
- `claude` CLI installed and authenticated with `claude auth login`
- A small target project with a PRD markdown file

Example:

```powershell
.\scripts\kodawari.ps1 work-all `
  --project-root E:\path\to\target-project `
  --feature quickstart-real-cli `
  --prd E:\path\to\target-project\PRD.md `
  --planner-route model `
  --executor-backend claude_code `
  --reviewer-backend cli `
  --real-peer-review `
  --max-cycles 1 `
  --rollback-on-failure
```

Then inspect:

```powershell
.\scripts\kodawari.ps1 status --project-root E:\path\to\target-project --feature quickstart-real-cli
```

If auth is missing or the CLI is unavailable, the command should fail with a
structured remediation message rather than pretending the real review passed.

## Greenfield: From Empty Dir

When the target directory does NOT yet contain a project (no FastAPI app, no
package.json, no manage.py), kodawari's greenfield path lets you go from
PRD to first-task in five steps. Use this when starting a fresh CLI tool,
library, data pipeline, or service from scratch.

Prerequisites: an empty (or near-empty) project directory and a PRD markdown
file (a couple of paragraphs is enough — the planner asks for clarification
when it isn't).

```powershell
$projectRoot = "E:\path\to\new-cli-tool"
$prd = "E:\path\to\new-cli-tool\PRD.md"
$feature = "bootstrap"

# 0. (Optional) Static preflight — checks .gitignore, planning_dir writability,
#    PRD size. No network calls.
.\scripts\kodawari.ps1 doctor preflight `
  --project-root $projectRoot --feature $feature --prd $prd

# 1. PRD intake — extracts source-of-truth, layers, path_type.
.\scripts\kodawari.ps1 prd-intake --project-root $projectRoot --feature $feature --prd $prd

# 2. Architecture plan — infers archetype/capabilities and surfaces.
.\scripts\kodawari.ps1 architecture-plan --project-root $projectRoot --feature $feature

# 3. Init — scaffold the chosen archetype skeleton and persist a manifest.
#    The manifest pins the archetype so subsequent rounds don't re-infer
#    fastapi_api on the near-empty directory.
.\scripts\kodawari.ps1 init --project-root $projectRoot --archetype fastapi_api --capability docs_runbook

# 4. Task plan — materialize TASK_GRAPH.json.
.\scripts\kodawari.ps1 task-plan --project-root $projectRoot --feature $feature

# 5. Work all — run planning + work + review + release end-to-end.
.\scripts\kodawari.ps1 work-all `
  --project-root $projectRoot --feature $feature --prd $prd `
  --planner-route model --executor-backend claude_code `
  --reviewer-backend cli --real-peer-review `
  --max-cycles 4 --max-wall-clock-seconds 3600 `
  --rollback-on-failure
```

Notes:

- `--max-wall-clock-seconds 3600` (1 hour) is a whole-process budget that
  catches stuck loops the per-round timeout cannot. When exceeded, autopilot
  writes `planning/<feature>/ABORT_REPORT.json` and exits 124.
- Greenfield tasks may declare up to 5 files per task (bootstrap vertical
  slice: schema+model+repo+service+test); existing-project tasks are still
  capped at 3 to keep refactor scope tight.
- `kodawari status` includes a `first_run_hint` field showing the next
  command to run — useful when a run pauses for a decision or stops mid-flow.

## Integration Environment

API-key gateway review is opt-in. Set these only for integration lanes:

```powershell
$env:WORKFLOW_REVIEWER_API_KEY = "..."
$env:WORKFLOW_REVIEWER_BASE_URL = "..."
.\scripts\run_integration_lane.ps1 -FailIfSkipped
```

When the env vars are absent, integration lanes report structured `SKIP`.

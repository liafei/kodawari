# Changelog

All notable changes to kodawari will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-05-18

Initial public release. Forked from internal workflow-sdk after the strict
no-fake-run policy and contract-first artifact chain stabilized.

### Highlights

- **Greenfield-first**: empty dir + PRD → end-to-end shipping path.
  `kodawari init-wizard` generates `.claude/workflow/models.yaml` and
  `.env.example` from one of three opinionated presets in seconds.
- **No-fake-run policy**: production-strict mode (`KODAWARI_REVIEW_ENABLED=1`)
  refuses silent-pass fallbacks in reviewer, verify, and gate paths.
- **Wall-clock watchdog**: `--max-wall-clock-seconds` writes
  `ABORT_REPORT.json` and exits 124 on whole-loop timeout.
- **`doctor preflight`**: static configuration checks before a real run;
  no network calls.
- **`status first_run_hint`**: every status output includes the next
  actionable command derived from artifact state.
- **Closure-tracing dependency skips**: failed tasks emit
  `blocked_by: [<failed-ancestor>]` instead of just the immediate dep.

### Initial capability set

- Planner / plan-reviewer / impl-reviewer / executor as four distinct
  configurable roles via `.claude/workflow/models.yaml`.
- Three init-wizard presets: `claude-subscription`, `openai-compatible`,
  `multi-provider`.
- Code-quality gate with three-tier severity (BLOCK / WARN / DASHBOARD).
- Self-repair proposals + learn loop (opt-in).
- Schema-validated contract-first artifact chain (PRD_INTAKE → ARCHITECTURE_PLAN
  → REPO_INVENTORY → TASK_GRAPH → TASK_CARD).

### Known limits

- PRD intake heuristic is conservative; non-FastAPI shapes may produce
  low-confidence intake. Workaround: `kodawari init --archetype <name>`.
- Release gate stops at `AWAITING_DECISION` — explicit `kodawari decide`
  step required.
- Env vars remain `WORKFLOW_*` prefixed (legacy from pre-rename); `KODAWARI_*`
  rename planned for v0.2.

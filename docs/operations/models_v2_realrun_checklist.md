# Models V2 Real-Run Checklist

Use this before running the Codex planner + Opus reviewer + Mimo executor stack.

1. Copy `docs/operations/models_v2_codex_opus_mimo_template.yaml` to the target project's `.claude/workflow/models.yaml`.
2. Set secrets in the shell, not in YAML:
   - `WORKFLOW_MIMO_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1`
   - `WORKFLOW_MIMO_KEY=<token>`
   - `WORKFLOW_IMPL_REVIEWER_BASE_URL=https://token-plan-sgp.xiaomimimo.com/anthropic`
   - `WORKFLOW_IMPL_REVIEWER_KEY=<token>`
3. Verify local Codex login can run `gpt-5.5` through the `codex` CLI.
4. Run static model checks:
   - `kodawari doctor models --project-root <project> --offline`
5. Probe real tool-calling support:
   - `kodawari doctor models --project-root <project> --probe-tools --no-cache`
6. Run the real Mimo executor smoke:
   - `kodawari doctor models --project-root <project> --smoke=real --no-cache`
7. Run the exact patch protocol smoke:
   - `kodawari doctor models --project-root <project> --smoke=patch-real --no-cache`

The `openai_tool_use` executor is intentionally an execution-only driver. It expects the planner and plan reviewer to have already reduced the task to a bounded `files_to_change` set. In the default `full_file_v1` protocol it supports full-file writes, deletes, bounded reads, scratch verify, and commit-after-verify. It does not expose shell or rename/move tools.
When `roles.executor.execution_protocol: exact_str_replace_v1` is configured, the executor exposes only hash/read-partial/exact `str_replace`/finish tools and records `.execution_patch_attempts.jsonl` for reviewer evidence. It still does not expose shell, rename/move, unified diff, or free-form patch tools.

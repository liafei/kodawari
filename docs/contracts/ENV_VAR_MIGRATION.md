# ENV VAR MIGRATION: `WORKFLOW_OPUS_*` → `WORKFLOW_REVIEWER_*`

> **Status**: v1 — dual-support active with deprecation warnings

This document tracks the planned replacement of all `WORKFLOW_OPUS_*`
environment variables with provider-neutral `WORKFLOW_REVIEWER_*` (or
`WORKFLOW_REVIEW_*`) names. The old names were tightly coupled to a
specific model vendor ("Opus"); the new names reflect the role
("Reviewer") and are model-agnostic.

---

## Migration Table

All three variables flagged as **required migration** by the task scope,
plus every other `WORKFLOW_OPUS_*` variable found in the codebase, are
listed below.

| Old variable (deprecated)        | New variable (canonical)                 | Notes                                                        |
|----------------------------------|------------------------------------------|--------------------------------------------------------------|
| `WORKFLOW_OPUS_GATEWAY`          | `WORKFLOW_REVIEWER_BASE_URL`             | Base URL for the reviewer API gateway. **Primary target.**   |
| `WORKFLOW_OPUS_API_KEY`          | `WORKFLOW_REVIEWER_API_KEY`              | API key for the reviewer. **Primary target.**                |
| `WORKFLOW_OPUS_MODEL`            | `WORKFLOW_REVIEWER_MODEL`                | Model name/ID for the reviewer. **Primary target.**          |
| `WORKFLOW_OPUS_API_FORMAT`       | `WORKFLOW_REVIEWER_API_FORMAT`           | Wire format: `openai` or `anthropic`.                        |
| `WORKFLOW_OPUS_REVIEW_ENABLED`   | `WORKFLOW_REVIEW_ENABLED`                | `1`/`0` flag; note prefix change (`OPUS_` dropped entirely). |
| `WORKFLOW_OPUS_REVIEW_REQUIRED`  | `WORKFLOW_REVIEW_REQUIRED`               | `1`/`0` flag; prefix change same as above.                   |
| `WORKFLOW_OPUS_REVIEWER_BACKEND` | `WORKFLOW_REVIEWER_BACKEND`              | Backend selector: `api`, `cli`, `codex`, `auto`, …          |
| `WORKFLOW_OPUS_REVIEWER_EXECUTABLE` | `WORKFLOW_REVIEWER_EXECUTABLE`        | Generic fallback executable; prefer the per-backend vars below. |
| *(no old equivalent)*            | `WORKFLOW_REVIEWER_CLAUDE_EXECUTABLE`    | Executable override for the Claude/CLI backend specifically. |
| *(no old equivalent)*            | `WORKFLOW_REVIEWER_CODEX_EXECUTABLE`     | Executable override for the Codex backend specifically.      |
| `WORKFLOW_OPUS_REVIEWER_TIMEOUT` | `WORKFLOW_REVIEWER_TIMEOUT`              | Per-call reviewer timeout in seconds.                        |
| `WORKFLOW_OPUS_TIMEOUT_SECONDS`  | *(not yet assigned a canonical name)*    | Gateway-level HTTP timeout — migration pending (post-v1).    |
| `WORKFLOW_OPUS_RETRY_ATTEMPTS`   | *(not yet assigned a canonical name)*    | Gateway retry count — migration pending (post-v1).           |
| `WORKFLOW_OPUS_MAX_TOKENS`       | *(not yet assigned a canonical name)*    | Max tokens per reviewer call — migration pending (post-v1).  |

> **Resolution rule (current, v0)**: the adapter reads the new name first;
> if it is absent or empty, it falls back to the old name. Both names are
> therefore fully functional right now. New deployments should use the
> canonical names only.

---

## Three-Phase Schedule

### Phase v0 — Dual-support (completed)

- **Env-var layer**: the adapter (`local_adapter.py`) reads the new canonical
  env-var name first and falls back to the old name when the new name is
  absent.
- **Config-field layer** (already ahead of schedule): the
  `LocalCodexAdapterConfig` dataclass *does* emit a `DeprecationWarning`
  as of P112 when any `opus_gateway_*` kwarg is passed non-default
  (`opus_gateway_base_url`, `opus_gateway_api_key`, `opus_gateway_model`,
  `opus_gateway_api_format`). The warning text references this file.
- CI secret names remain unchanged during this phase to avoid breaking
  existing pipelines.
- **Action for users**: migrate secrets and `.env` files from old names to
  new names at your own pace before 2026-08-01. Python callers constructing
  `LocalCodexAdapterConfig` directly should also migrate kwargs now to
  silence the warning.

### Phase v1 — Deprecation warnings in code (current)

- The fallback path in `local_adapter.py` will emit a `DeprecationWarning`
  (Python `warnings.warn`) whenever an old `WORKFLOW_OPUS_*` name is read
  and the new canonical name is absent.
- Warning text will follow the pattern:
  ```
  DeprecationWarning: Environment variable WORKFLOW_OPUS_GATEWAY is
  deprecated; set WORKFLOW_REVIEWER_BASE_URL instead.
  WORKFLOW_OPUS_GATEWAY will be removed after 2026-11-01.
  ```
- The fallback logic is otherwise identical to v0; nothing breaks.
- `REMOVE_AFTER: 2026-11-01` is encoded as `LEGACY_ENV_REMOVE_AFTER` in
  `src/kodawari/autopilot/core/_env_helpers.py`.
- CI templates (`.github/workflows/`) should move to the new names before v2.
- `lane_triage_cmd.py` diagnostic messages will reference both old and new
  names in their setup instructions.

### Phase v2 — Removal (target: **2026-11-01**)

- All fallback code that reads `WORKFLOW_OPUS_*` names will be deleted.
- The old variable names will be silently ignored (not read at all).
- Any deployment that still sets only the old names will silently get empty
  values and the reviewer will be disabled/blocked.
- Code sites that emit warnings reference `LEGACY_ENV_REMOVE_AFTER`.

---

## What Is NOT Being Renamed

### `CollaborationRole.OPUS`

The Python enum value `CollaborationRole.OPUS = "opus"` (defined in
`src/kodawari/autopilot/collaboration.py`) is **intentionally preserved
across all three phases**.

The role identifier is written into collaboration state files and read back
by the loop orchestrator. Renaming it would silently break in-flight
sessions and require a state-file migration that carries significant risk.
The env-var layer and the role layer are separate concerns:

- **Env vars** configure *how the reviewer is reached* (URL, key, model).
- **`CollaborationRole`** identifies *which agent is acting* in a
  conversation turn.

These are orthogonal. The env-var migration does not imply a role rename.

---

## Migration Checklist for Operators

- [ ] Replace `WORKFLOW_OPUS_GATEWAY` with `WORKFLOW_REVIEWER_BASE_URL`
      in all CI secret stores, `.env` files, and deployment configs.
- [ ] Replace `WORKFLOW_OPUS_API_KEY` with `WORKFLOW_REVIEWER_API_KEY`.
- [ ] Replace `WORKFLOW_OPUS_MODEL` with `WORKFLOW_REVIEWER_MODEL`.
- [ ] Replace `WORKFLOW_OPUS_API_FORMAT` with `WORKFLOW_REVIEWER_API_FORMAT`.
- [ ] Replace `WORKFLOW_OPUS_REVIEW_ENABLED` with `WORKFLOW_REVIEW_ENABLED`.
- [ ] Replace `WORKFLOW_OPUS_REVIEW_REQUIRED` with `WORKFLOW_REVIEW_REQUIRED`.
- [ ] Replace `WORKFLOW_OPUS_REVIEWER_BACKEND` with `WORKFLOW_REVIEWER_BACKEND`.
- [ ] Replace `WORKFLOW_OPUS_REVIEWER_EXECUTABLE` with
      `WORKFLOW_REVIEWER_EXECUTABLE` (or the per-backend variants
      `WORKFLOW_REVIEWER_CLAUDE_EXECUTABLE` / `WORKFLOW_REVIEWER_CODEX_EXECUTABLE`).
- [ ] Replace `WORKFLOW_OPUS_REVIEWER_TIMEOUT` with `WORKFLOW_REVIEWER_TIMEOUT`.
- [ ] Complete before **2026-08-01** to avoid deprecation-warning noise.
- [ ] Complete before **2026-11-01** to avoid reviewer silently disabling.

---

## Related Files

- `src/kodawari/autopilot/local_adapter.py` — fallback resolution logic
  (`_env_new_or_old`, `_env_flag_new_or_old` helpers)
- `src/kodawari/cli/lane_triage_cmd.py` — diagnostic messages referencing
  old variable names (to be updated at v1)
- `.github/workflows/kodawari-integration.yml` — CI secrets using old names
  (to be updated at v1)
- `docs/planning/优化改动方案.md` §P1.1 — original design rationale for this migration

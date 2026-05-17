# Deprecated Imports and Legacy Entrypoints

Last updated: 2026-04-30

These paths remain available for backward compatibility, but new code should not depend on them.

| Legacy path | Replacement | Remove after | Notes |
|---|---|---|---|
| `kodawari.cli.legacy_cmds` | `kodawari.cli.core.legacy_cmds` for compatibility only; prefer canonical CLI commands | 2026-08-01 | Flat shim retained for historical `kodawari` shells. |
| `kodawari.cli.legacy_runtime_invocation` | `kodawari.cli.core.legacy_runtime_invocation` for compatibility only | 2026-08-01 | Runtime payloads now include a `deprecation` block when legacy shells run. |
| `kodawari.cli.legacy_shell_runtime` | `kodawari.cli.core.legacy_shell_runtime` for compatibility only | 2026-08-01 | Use `kodawari autopilot`, `kodawari status`, and `kodawari gate` directly. |
| `kodawari.autopilot.core.state_legacy` | `kodawari.autopilot.core.state` / `kodawari.autopilot.core.state_models` | 2026-08-01 | Pure re-export shim after the state canonical migration. |
| `kodawari.autopilot.state_legacy` | `kodawari.autopilot.core.state` / `kodawari.autopilot.core.state_models` | 2026-08-01 | Flat shim retained for historical imports. |

Legacy CLI invocations emit `DeprecationWarning` and include structured telemetry under `payload["deprecation"]`. Import shims are intentionally quiet so old state readers and discovery tools do not fail during migration.

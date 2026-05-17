# WorkflowCTL Desktop Distribution

## Strategy

WorkflowCTL uses a Tauri shell around the React UI in `web/`. The desktop app
starts `kodawari serve` as a local sidecar and talks to it over
`127.0.0.1`.

The current distribution strategy is **BYO Python 3.11+**:

- Users install Python 3.11 or newer.
- Users install the package with `pip install kodawari[serve]`.
- The Tauri shell locates `kodawari` from `WORKFLOWCTL_BIN`, the developer
  checkout venv, or `PATH`.

PyInstaller packaging is intentionally deferred until BYO Python becomes a
proven onboarding blocker.

## Onboarding Targets

- Cold start: Python + package install + CLI auth in 15 minutes or less.
- Warm start: existing Python and auth, launch desktop app in 3 minutes or less.

These targets are separate from the developer quickstart 10 minute gate, which
only covers editable install plus the `noop_test_only` smoke.

## Binary Resolution Order

1. `WORKFLOWCTL_BIN`
2. `<repo>/.workflow_runtime/local-env/.venv/Scripts/kodawari.exe`
3. Legacy developer fallback: `<repo>/.venv/Scripts/kodawari.exe`
4. `kodawari(.exe)` on `PATH`

The legacy fallback exists only to avoid breaking already-running developer
sessions. New local environments are created under `.workflow_runtime/`.

## Telemetry

Desktop telemetry is off by default. It only turns on when
`WORKFLOW_DESKTOP_TELEMETRY` is explicitly set to one of:

```text
1, true, yes, on
```

No analytics package is bundled in the React app by default.

## Build

```powershell
cd kodawari\web
npm install
npm run build
npm run tauri:build
```

Build outputs are ignored:

- `web/node_modules/`
- `web/dist/`
- `web/src-tauri/target/`


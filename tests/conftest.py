from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Iterator

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
existing_pythonpath = os.environ.get("PYTHONPATH", "")
pythonpath_parts = [part for part in existing_pythonpath.split(os.pathsep) if part]
for required_path in (str(SRC), str(ROOT)):
    if required_path not in pythonpath_parts:
        pythonpath_parts.insert(0, required_path)
os.environ["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

# Ensure rootdir is on sys.path so tests._helpers is importable as a package.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Subprocess isolation helpers — imported here so conftest.py is a direct
# consumer of subprocess_isolation.py (separate from _helpers/__init__.py).
# ---------------------------------------------------------------------------
from tests._helpers.subprocess_isolation import (  # noqa: E402
    SubprocessResult,
    assert_no_leaked_module,
    run_python_isolated,
)
from kodawari.testing.pytest_summary_writer import (  # noqa: E402
    build_pytest_summary_payload,
    utc_now_iso,
    write_pytest_summary,
)

# Re-export for tests that import from conftest directly (rare, but supported).
__all__ = ["SubprocessResult", "assert_no_leaked_module", "run_python_isolated"]


def pytest_configure(config: pytest.Config) -> None:
    config._workflow_summary_started_at = utc_now_iso()  # type: ignore[attr-defined]
    config._workflow_summary_started_monotonic = time.monotonic()  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    raw_target = os.getenv("WORKFLOW_SDK_PYTEST_SUMMARY_JSON", "").strip()
    if not raw_target:
        return
    target = Path(raw_target)
    if not target.is_absolute():
        target = ROOT / target
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(terminal, "stats", {}) if terminal is not None else {}
    started_monotonic = getattr(session.config, "_workflow_summary_started_monotonic", None)
    duration = time.monotonic() - started_monotonic if isinstance(started_monotonic, float) else None
    payload = build_pytest_summary_payload(
        collected=int(getattr(session, "testscollected", 0) or 0),
        exit_code=int(exitstatus),
        stats=stats,
        started_at_utc=str(getattr(session.config, "_workflow_summary_started_at", "")),
        finished_at_utc=utc_now_iso(),
        duration_seconds=duration,
    )
    write_pytest_summary(target, payload)


# ---------------------------------------------------------------------------
# clean_env_state — autouse fixture: env var + cwd isolation per test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_env_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore process-global cwd and workflow env vars after every test.

    Rationale
    ---------
    Tests that call ``os.chdir()`` directly (not via ``monkeypatch.chdir``)
    leave the process in a dirty state that causes unrelated tests to fail
    when they are run in a different order.  With ``pytest-random-order``
    enabled, any such cwd leak becomes a flaky-test root cause.

    ``monkeypatch`` handles ``monkeypatch.setenv / delenv`` reverts, but a few
    integration-style tests intentionally exercise direct dotenv loading into
    ``os.environ``.  Snapshot the workflow-scoped env too so one test cannot
    turn on real review for later simulate-mode tests.

    Invariants
    ----------
    * ``sys.modules`` is intentionally **not** rolled back here — rolling
      back all added modules corrupts legitimate lazy imports in subsequent
      tests.  Use :func:`~tests._helpers.subprocess_isolation.run_python_isolated`
      or a scoped ``patch.dict(sys.modules, ...)`` for module-level
      isolation instead.
    * Only env vars and cwd are guarded — both are naturally reversible
      without risking import-graph corruption.
    """
    guarded_prefixes = ("WORKFLOW_", "KODAWARI_")
    original_cwd = Path.cwd()
    original_env = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(guarded_prefixes)
    }
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith(guarded_prefixes) and key not in original_env:
                os.environ.pop(key, None)
        for key, value in original_env.items():
            os.environ[key] = value
        try:
            os.chdir(original_cwd)
        except OSError:
            # Original cwd was deleted during the test — nothing to restore.
            pass


# ---------------------------------------------------------------------------
# run_isolated_python — opt-in fixture for subprocess-level isolation
# ---------------------------------------------------------------------------


@pytest.fixture()
def run_isolated_python():
    """Return :func:`~tests._helpers.subprocess_isolation.run_python_isolated`.

    Tests that need to verify import boundaries or module-level side-effects
    should use this fixture rather than importing ``subprocess`` directly, so
    the isolation contract remains centralised and auditable.

    Usage::

        def test_no_cli_leak(run_isolated_python):
            result = run_isolated_python(
                "import kodawari.autopilot"
            )
            assert result.ok

    The fixture intentionally does *not* restore ``sys.modules`` — the whole
    point is that the code under test runs in a separate process.
    """
    return run_python_isolated

"""Tests for run_id-aware staleness reset in task_run_state_sync.

Closes the wf-test scenario where a 2-week-old `codex_cli execution timed out`
in error_history survived across sessions and showed up next to a fresh
PASS in `kodawari status` and downstream review evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.runtime.task_run_state_sync import sync_task_run_terminal_state


def _write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_sync_with_new_run_id_resets_session_scoped_fields(tmp_path: Path) -> None:
    """The wf-test footgun: stale errors from prior session must NOT survive."""
    state = tmp_path / ".autopilot_state.json"
    _write_state(state, {
        "run_id": "OLD_RUN_ID_FROM_2WEEKS_AGO",
        "error_history": ["codex_cli execution timed out"],
        "error_events": [{"timestamp": "2026-04-14T09:44:35Z", "message": "x"}],
        "last_error": "stale-old-error",
        "verify_setup_recovery_attempted": 3,
        "verify_setup_recovery_succeeded": 1,
        "verify_setup_recovery_last_error": "old recovery glitch",
        "warning_noise_events": 12,
        "warning_noise_degraded_events": 5,
        "warning_noise_by_task": {"T1": 8, "T2": 4},
    })

    sync_task_run_terminal_state(
        state_path=state,
        run_result={"reason": "PROCEED_TO_GATE"},
        run_id="FRESH_RUN_ID",
    )

    payload = _read_state(state)
    # Session-scoped fields must be cleared.
    assert payload["error_history"] == []
    assert payload["error_events"] == []
    assert payload["last_error"] is None  # blocking_reason absent for PASS
    assert payload["verify_setup_recovery_attempted"] == 0
    assert payload["verify_setup_recovery_succeeded"] == 0
    assert payload["verify_setup_recovery_last_error"] is None
    assert payload["warning_noise_events"] == 0
    assert payload["warning_noise_degraded_events"] == 0
    assert payload["warning_noise_by_task"] == {}
    # New run_id is now stamped.
    assert payload["run_id"] == "FRESH_RUN_ID"
    # Terminal fields applied.
    assert payload["final_status"] == "PASS"
    assert payload["stop_reason"] == "PASS"


def test_sync_with_matching_run_id_preserves_session_fields(tmp_path: Path) -> None:
    """Same run retrying must NOT lose its in-session error history."""
    state = tmp_path / ".autopilot_state.json"
    _write_state(state, {
        "run_id": "SAME_RUN",
        "error_history": ["transient codex retry 1"],
        "warning_noise_events": 2,
    })

    sync_task_run_terminal_state(
        state_path=state,
        run_result={"reason": "OPUS_REVIEW_BLOCKED", "blocking_reason": "review fail"},
        run_id="SAME_RUN",
    )

    payload = _read_state(state)
    # Same run_id => session-scoped fields preserved.
    assert payload["error_history"] == ["transient codex retry 1"]
    assert payload["warning_noise_events"] == 2
    assert payload["run_id"] == "SAME_RUN"
    # Terminal fields still applied.
    assert payload["final_status"] == "BLOCKED"
    assert payload["stop_reason"] == "HARD_ERROR"
    assert payload["last_error"] == "review fail"


def test_sync_with_legacy_state_no_run_id_resets_when_new_run_id_supplied(tmp_path: Path) -> None:
    """Legacy state files (pre-run_id) must not silently inherit stale fields
    once a new run with a real run_id arrives."""
    state = tmp_path / ".autopilot_state.json"
    _write_state(state, {
        # NOTE: no run_id field — this is what existing on-disk state looks like
        # before this commit lands.
        "error_history": ["legacy stale error"],
        "verify_setup_recovery_attempted": 7,
    })

    sync_task_run_terminal_state(
        state_path=state,
        run_result={"reason": "PROCEED_TO_GATE"},
        run_id="FIRST_RUN_AFTER_UPGRADE",
    )

    payload = _read_state(state)
    assert payload["error_history"] == []
    assert payload["verify_setup_recovery_attempted"] == 0
    assert payload["run_id"] == "FIRST_RUN_AFTER_UPGRADE"


def test_sync_without_run_id_arg_preserves_legacy_behavior(tmp_path: Path) -> None:
    """Backward compat: callers that don't pass run_id must NOT trigger
    the session-reset path (otherwise legacy autopilot sessions break)."""
    state = tmp_path / ".autopilot_state.json"
    _write_state(state, {
        "run_id": "ANY_VALUE",
        "error_history": ["preserved-by-legacy-path"],
    })

    sync_task_run_terminal_state(
        state_path=state,
        run_result={"reason": "PROCEED_TO_GATE"},
        # run_id arg omitted (defaults to "")
    )

    payload = _read_state(state)
    # No reset because incoming run_id was empty.
    assert payload["error_history"] == ["preserved-by-legacy-path"]
    # run_id field is also untouched.
    assert payload["run_id"] == "ANY_VALUE"


def test_sync_no_op_when_state_does_not_exist(tmp_path: Path) -> None:
    """task-run is not the bootstrap owner."""
    state = tmp_path / ".autopilot_state.json"
    sync_task_run_terminal_state(
        state_path=state,
        run_result={"reason": "PROCEED_TO_GATE"},
        run_id="FRESH",
    )
    assert not state.exists()


def test_sync_no_op_when_reason_not_terminal(tmp_path: Path) -> None:
    state = tmp_path / ".autopilot_state.json"
    _write_state(state, {"run_id": "OLD", "error_history": ["preserve"]})
    sync_task_run_terminal_state(
        state_path=state,
        run_result={"reason": "INTERMEDIATE_NOT_TERMINAL"},
        run_id="NEW",
    )
    payload = _read_state(state)
    # No-op: reason wasn't recognized as terminal, so nothing was written.
    assert payload == {"run_id": "OLD", "error_history": ["preserve"]}

"""Terminal state sync for `kodawari task-run`.

When task-run finishes with a terminal loop reason (OPUS_REVIEW_BLOCKED,
PROCEED_TO_GATE, SELF_REVIEW_BLOCKED, VERIFY_BLOCKED, ...), this module writes
that outcome back into .autopilot_state.json. Without this sync,
`kodawari status` would read the stale RUNNING/IMPLEMENT stage left over
from a prior autopilot session and report the wrong truth.

The write is deliberately narrow for the run-scoped terminal fields:
  - current_stage  → "COMPLETED"
  - final_status   → PASS | BLOCKED | FAILED
  - stop_reason    → PASS | HARD_ERROR | MAX_CYCLES
  - last_stage_status
  - last_error (from blocking_reason when available)
  - updated_at
  - run_id (this run's identifier — used by the next run to detect staleness)

When the existing state file's ``run_id`` does NOT match the incoming run_id
(or is absent — legacy state predates the field), the sync ALSO clears
session-scoped fields that would otherwise show up next to a fresh outcome:
``error_history``, ``error_events``, ``last_error``, ``verify_setup_recovery_*``,
``warning_noise_*``. This was a real footgun in wf-test: a 2-week-old
``codex_cli execution timed out`` survived across sessions and landed in
review evidence as if it were the current run's failure.

task-run is not the owner of state bootstrap: if .autopilot_state.json does
not exist yet, the sync is a no-op.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.prd_contract import write_json


_TASK_RUN_TERMINAL_REASONS: dict[str, dict[str, str]] = {
    "PROCEED_TO_GATE": {"final_status": "PASS", "stop_reason": "PASS"},
    "PIPELINE_FINISH": {"final_status": "PASS", "stop_reason": "PASS"},
    "OPUS_REVIEW_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "SELF_REVIEW_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "VERIFY_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "GATE_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "PROTECTED_FILE_BLOCK": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "PHASE_GUARD_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "EXECUTION_BACKEND_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "DIRTY_WORKTREE_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "SCOPE_DRIFT_BLOCKED": {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"},
    "MAX_CYCLES_REACHED": {"final_status": "BLOCKED", "stop_reason": "MAX_CYCLES"},
    "MAX_CYCLES": {"final_status": "BLOCKED", "stop_reason": "MAX_CYCLES"},
    "COLLABORATION_ROUND_LIMIT": {"final_status": "BLOCKED", "stop_reason": "MAX_CYCLES"},
    "IMPLEMENTATION_ERROR": {"final_status": "FAILED", "stop_reason": "HARD_ERROR"},
}


_SESSION_SCOPED_FIELDS = (
    "error_history",
    "error_events",
    "verify_setup_recovery_attempted",
    "verify_setup_recovery_succeeded",
    "verify_setup_recovery_last_error",
    "warning_noise_events",
    "warning_noise_degraded_events",
    "warning_noise_by_task",
)


def derive_task_run_terminal_state(run_result: dict[str, Any]) -> dict[str, str] | None:
    """Map a task-run collaboration loop reason → terminal state fields.

    Returns None when the reason is not a known terminal marker (e.g. partial
    stop the caller should not persist).
    """
    reason = str(run_result.get("reason") or "").strip().upper()
    return _TASK_RUN_TERMINAL_REASONS.get(reason)


def _reset_session_scoped_fields(payload: dict[str, Any]) -> None:
    """Clear fields that belong to a single task-run session.

    Numeric counters reset to 0; list/dict containers reset to empty; the
    free-form ``last_error`` is cleared (the caller may overwrite it with a
    fresh blocking_reason immediately after).
    """
    payload["error_history"] = []
    payload["error_events"] = []
    payload["last_error"] = None
    payload["verify_setup_recovery_attempted"] = 0
    payload["verify_setup_recovery_succeeded"] = 0
    payload["verify_setup_recovery_last_error"] = None
    payload["warning_noise_events"] = 0
    payload["warning_noise_degraded_events"] = 0
    payload["warning_noise_by_task"] = {}


def sync_task_run_terminal_state(
    *,
    state_path: Path,
    run_result: dict[str, Any],
    run_id: str = "",
) -> None:
    """Write terminal fields from a task-run result back to .autopilot_state.json.

    When ``run_id`` is supplied AND the existing state file carries a
    different ``run_id`` (or none — legacy state), session-scoped fields are
    reset before the terminal fields are written. This prevents a stale
    error_history from a prior session showing up next to a fresh PASS.

    No-op if:
    - state_path does not exist (task-run is not the bootstrap owner)
    - run_result.reason is not a known terminal marker
    - state_path cannot be read or parsed
    """
    terminal = derive_task_run_terminal_state(run_result)
    if terminal is None or not state_path.exists():
        return
    try:
        payload_text = state_path.read_text(encoding="utf-8")
        payload = json.loads(payload_text)
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    incoming_run_id = str(run_id or "").strip()
    existing_run_id = str(payload.get("run_id") or "").strip()
    if incoming_run_id and incoming_run_id != existing_run_id:
        _reset_session_scoped_fields(payload)
        payload["run_id"] = incoming_run_id
    payload["current_stage"] = "COMPLETED"
    payload["final_status"] = terminal["final_status"]
    payload["stop_reason"] = terminal["stop_reason"]
    payload["last_stage_status"] = terminal["final_status"]
    blocking = str(
        run_result.get("blocking_reason")
        or run_result.get("last_error")
        or ""
    ).strip()
    if blocking:
        payload["last_error"] = blocking
    payload["updated_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        write_json(state_path, payload)
    except OSError:
        return


__all__ = [
    "derive_task_run_terminal_state",
    "sync_task_run_terminal_state",
]


"""state.final_status is derived from stop_reason; the two cannot disagree."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodawari.autopilot.core.state import (
    AutopilotState,
    StopReason,
    derived_final_status,
)


@pytest.mark.parametrize(
    "reason,expected",
    [
        (StopReason.PASS, "PASS"),
        (StopReason.MAX_CYCLES, "BLOCKED"),
        (StopReason.TOKEN_BUDGET, "BLOCKED"),
        (StopReason.STUCK, "BLOCKED"),
        (StopReason.HARD_ERROR, "BLOCKED"),
        (StopReason.NO_PROGRESS, "BLOCKED"),
        (StopReason.USER_INTERRUPT, "BLOCKED"),
    ],
)
def test_each_reason_derives_status(reason: StopReason, expected: str) -> None:
    assert derived_final_status(reason) == expected


def test_string_form_resolves() -> None:
    assert derived_final_status("PASS") == "PASS"
    assert derived_final_status("stuck") == "BLOCKED"


def test_none_or_unknown_blocks() -> None:
    assert derived_final_status(None) == "BLOCKED"
    assert derived_final_status("MADE_UP") == "BLOCKED"


def test_mark_completed_overrides_inconsistent_status_argument() -> None:
    state = AutopilotState(feature="feat", project_root=Path("."))
    # Caller incorrectly tries to mark a STUCK run as PASS.
    state.mark_completed(StopReason.STUCK, "PASS")
    # final_status follows stop_reason, not the bogus caller argument.
    assert state.final_status == "BLOCKED"
    assert state.stop_reason == StopReason.STUCK


def test_mark_completed_pass_yields_pass_final_status() -> None:
    state = AutopilotState(feature="feat", project_root=Path("."))
    state.mark_completed(StopReason.PASS, "BLOCKED")  # bogus caller argument
    assert state.final_status == "PASS"


def test_mark_completed_status_optional() -> None:
    state = AutopilotState(feature="feat", project_root=Path("."))
    state.mark_completed(StopReason.MAX_CYCLES)
    assert state.final_status == "BLOCKED"
    assert state.last_stage_status == "BLOCKED"


def test_last_stage_status_preserves_caller_text_when_given() -> None:
    state = AutopilotState(feature="feat", project_root=Path("."))
    state.mark_completed(StopReason.STUCK, "executor_recovery_total_exhausted")
    # final_status stays canonical
    assert state.final_status == "BLOCKED"
    # but operator-readable last_stage_status keeps the descriptive text
    assert state.last_stage_status == "executor_recovery_total_exhausted"

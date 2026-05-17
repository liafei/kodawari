"""StopReason -> StopAction mapping.

Pin the structured next-step recommendation per StopReason so downstream
callers (release flow, schedulers, UI) branch on a stable contract instead
of parsing the English `_completed_next_action` strings.
"""

from __future__ import annotations

import pytest

from kodawari.autopilot.core.state import StopAction, StopReason, stop_reason_next_action


@pytest.mark.parametrize(
    "reason,expected",
    [
        (StopReason.PASS, StopAction.PROCEED_NEXT_TASK),
        (StopReason.MAX_CYCLES, StopAction.RETRY_WITH_HIGHER_BUDGET),
        (StopReason.TOKEN_BUDGET, StopAction.RETRY_WITH_HIGHER_BUDGET),
        (StopReason.NO_PROGRESS, StopAction.ESCALATE_TO_PLAN),
        (StopReason.STUCK, StopAction.ESCALATE_TO_PLAN),
        (StopReason.HARD_ERROR, StopAction.HARD_STOP),
        (StopReason.USER_INTERRUPT, StopAction.AWAIT_USER),
    ],
)
def test_each_stop_reason_has_explicit_action(reason: StopReason, expected: StopAction) -> None:
    assert stop_reason_next_action(reason) == expected


def test_string_form_resolves() -> None:
    """JSON-deserialized state payloads carry strings, not enum members."""
    assert stop_reason_next_action("STUCK") == StopAction.ESCALATE_TO_PLAN
    assert stop_reason_next_action("max_cycles") == StopAction.RETRY_WITH_HIGHER_BUDGET


def test_unknown_string_defaults_to_hard_stop() -> None:
    assert stop_reason_next_action("MADE_UP_REASON") == StopAction.HARD_STOP


def test_none_defaults_to_hard_stop() -> None:
    """Defensive default — never silently auto-resume on missing data."""
    assert stop_reason_next_action(None) == StopAction.HARD_STOP


def test_action_enum_values_are_stable_strings() -> None:
    """The string values are part of the runtime contract — pin them."""
    assert StopAction.PROCEED_NEXT_TASK.value == "PROCEED_NEXT_TASK"
    assert StopAction.RETRY_WITH_HIGHER_BUDGET.value == "RETRY_WITH_HIGHER_BUDGET"
    assert StopAction.ESCALATE_TO_PLAN.value == "ESCALATE_TO_PLAN"
    assert StopAction.HARD_STOP.value == "HARD_STOP"
    assert StopAction.AWAIT_USER.value == "AWAIT_USER"

from __future__ import annotations

from kodawari.cli.autopilot_interaction_state import (
    InteractionState,
    build_interaction_snapshot,
    classify_interaction_state,
    classify_next_action_type,
    is_environment_error,
)


def test_classify_interaction_state_prioritizes_decision_pending() -> None:
    state = classify_interaction_state(
        decision_pending=True,
        environment_error_code="CODEX_CLI_MISSING",
        final_status="BLOCKED",
        stop_reason="TOKEN_BUDGET",
        blocked=True,
        is_terminal=True,
    )
    assert state == InteractionState.AWAITING_DECISION


def test_classify_interaction_state_detects_environment_block() -> None:
    state = classify_interaction_state(
        environment_error_code="CODEX_CLI_MISSING",
        environment_blocking_reason="codex_cli backend requires the 'codex' executable",
        final_status="BLOCKED",
        blocked=True,
    )
    assert state == InteractionState.AWAITING_ENVIRONMENT
    assert is_environment_error(
        error_code="",
        blocking_reason="external_cli backend requires executor command",
    )


def test_classify_interaction_state_handles_pass_blocked_and_running() -> None:
    assert classify_interaction_state(final_status="PASS") == InteractionState.PASS
    assert classify_interaction_state(stop_reason="TOKEN_BUDGET", is_terminal=True) == InteractionState.BLOCKED
    assert classify_interaction_state(final_status="", stop_reason="", blocked=False) == InteractionState.RUNNING


def test_classify_interaction_state_maps_home_inaccessible_to_env() -> None:
    # CLAUDE_CODE_HOME_INACCESSIBLE must route through AWAITING_ENVIRONMENT
    # so the operator next-action is `await_environment`, not generic
    # `resolve_blocked`. Without this membership in ENVIRONMENT_ERROR_CODES
    # the preflight BLOCKED payload would fall into the BLOCKED bucket.
    state = classify_interaction_state(
        environment_error_code="CLAUDE_CODE_HOME_INACCESSIBLE",
        environment_blocking_reason="Claude CLI cannot lstat Windows user home",
        final_status="BLOCKED",
        blocked=True,
        is_terminal=True,
    )
    assert state == InteractionState.AWAITING_ENVIRONMENT
    assert classify_next_action_type(state) == "await_environment"


def test_build_interaction_snapshot_exposes_next_action_type() -> None:
    snapshot = build_interaction_snapshot(
        decision_pending=False,
        decision_kind="architecture_freeze",
        decision_id="decision-7",
        decision_request_present=True,
        environment_error_code="",
        environment_blocking_reason="",
        final_status="PASS",
        stop_reason="PASS",
    )
    assert snapshot == {
        "interaction_state": "PASS",
        "decision_kind": "architecture_freeze",
        "decision_id": "decision-7",
        "decision_request_present": True,
        "next_action_type": "completed",
    }
    assert classify_next_action_type(InteractionState.AWAITING_ENVIRONMENT) == "await_environment"
    assert classify_next_action_type(InteractionState.BLOCKED) == "resolve_blocked"

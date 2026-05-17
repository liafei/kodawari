"""Interaction-state classifiers for autopilot product semantics."""

from __future__ import annotations

from enum import Enum
from typing import Any


class InteractionState(str, Enum):
    RUNNING = "RUNNING"
    AWAITING_DECISION = "AWAITING_DECISION"
    AWAITING_ENVIRONMENT = "AWAITING_ENVIRONMENT"
    BLOCKED = "BLOCKED"
    PASS = "PASS"


ENVIRONMENT_ERROR_CODES = {
    "CLAUDE_CODE_HOME_INACCESSIBLE",
    "CODEX_CLI_MISSING",
    "EXECUTION_BACKEND_NOT_CONFIGURED",
    "EXECUTOR_BACKEND_MISSING",
    "EXECUTOR_COMMAND_MISSING",
    "EXTERNAL_CLI_COMMAND_MISSING",
    "OPUS_GATEWAY_MISSING",
    "OPUS_API_KEY_MISSING",
    "REAL_REVIEW_ENVIRONMENT_MISSING",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _upper_text(value: Any) -> str:
    return _clean_text(value).upper()


def _lower_text(value: Any) -> str:
    return _clean_text(value).lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _lower_text(value) in {"1", "true", "yes", "on"}


def is_environment_error(*, error_code: Any = None, blocking_reason: Any = None) -> bool:
    code = _upper_text(error_code)
    if code in ENVIRONMENT_ERROR_CODES:
        return True
    reason = _lower_text(blocking_reason)
    if not reason:
        return False
    markers = (
        "api key",
        "gateway",
        "permission denied",
        "network is unreachable",
        "timed out waiting for lock",
        "requires the 'codex'",
        "requires executor command",
        "backend not configured",
        "executable",
    )
    return any(marker in reason for marker in markers)


def classify_interaction_state(
    *,
    decision_pending: bool = False,
    environment_error_code: Any = None,
    environment_blocking_reason: Any = None,
    final_status: Any = None,
    stop_reason: Any = None,
    blocked: bool = False,
    is_terminal: bool = False,
) -> InteractionState:
    if decision_pending:
        return InteractionState.AWAITING_DECISION
    if is_environment_error(
        error_code=environment_error_code,
        blocking_reason=environment_blocking_reason,
    ):
        return InteractionState.AWAITING_ENVIRONMENT
    if _upper_text(final_status) == "PASS" or _upper_text(stop_reason) == "PASS":
        return InteractionState.PASS
    blocked_reasons = {"TOKEN_BUDGET", "MAX_CYCLES", "HARD_ERROR", "STUCK", "NO_PROGRESS", "BLOCKED"}
    if blocked or _upper_text(final_status) == "BLOCKED":
        return InteractionState.BLOCKED
    if _upper_text(stop_reason) in blocked_reasons:
        return InteractionState.BLOCKED
    if is_terminal and _upper_text(final_status) and _upper_text(final_status) != "PASS":
        return InteractionState.BLOCKED
    return InteractionState.RUNNING


def classify_next_action_type(
    interaction_state: InteractionState | str,
) -> str:
    normalized = _upper_text(getattr(interaction_state, "value", interaction_state))
    if normalized == InteractionState.AWAITING_DECISION.value:
        return "await_decision"
    if normalized == InteractionState.AWAITING_ENVIRONMENT.value:
        return "await_environment"
    if normalized == InteractionState.BLOCKED.value:
        return "resolve_blocked"
    if normalized == InteractionState.PASS.value:
        return "completed"
    return "auto_continue"


def build_interaction_snapshot(
    *,
    decision_pending: bool = False,
    decision_kind: Any = None,
    decision_id: Any = None,
    decision_request_present: bool = False,
    environment_error_code: Any = None,
    environment_blocking_reason: Any = None,
    final_status: Any = None,
    stop_reason: Any = None,
    blocked: bool = False,
    is_terminal: bool = False,
) -> dict[str, Any]:
    state = classify_interaction_state(
        decision_pending=decision_pending,
        environment_error_code=environment_error_code,
        environment_blocking_reason=environment_blocking_reason,
        final_status=final_status,
        stop_reason=stop_reason,
        blocked=blocked,
        is_terminal=is_terminal,
    )
    return {
        "interaction_state": state.value,
        "decision_kind": _clean_text(decision_kind),
        "decision_id": _clean_text(decision_id),
        "decision_request_present": bool(decision_request_present),
        "next_action_type": classify_next_action_type(state),
    }


__all__ = [
    "ENVIRONMENT_ERROR_CODES",
    "InteractionState",
    "build_interaction_snapshot",
    "classify_interaction_state",
    "classify_next_action_type",
    "is_environment_error",
]

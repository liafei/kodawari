"""Loop outcome normalization helpers."""

from __future__ import annotations

from typing import Any


_LOOP_REASON_ROUND_OUTCOME: dict[str, str] = {
    "PROCEED_TO_GATE": "ready_for_gate",
    "PIPELINE_FINISH": "ready_for_gate",
    "VERIFY_BLOCKED": "blocked",
    "GATE_BLOCKED": "blocked",
    "OPUS_REVIEW_BLOCKED": "blocked",
    "PROTECTED_FILE_BLOCK": "blocked",
    "MAX_CYCLES_REACHED": "blocked",
    "COLLABORATION_ROUND_LIMIT": "needs_fix",
    "IMPLEMENTATION_ERROR": "error",
}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _last_round_outcome(payload: dict[str, Any]) -> str:
    rounds = payload.get("rounds")
    if not isinstance(rounds, list):
        return ""
    for record in reversed(rounds):
        if not isinstance(record, dict):
            continue
        outcome = _clean_text(record.get("round_outcome"))
        if outcome:
            return outcome
    return ""


def _resolve_exit_category(stop_reason: str, blocked: bool) -> str:
    if stop_reason == "PASS":
        return "pass"
    if blocked:
        return "blocked"
    return "stopped"


def _resolve_blocking_reason(
    *,
    unified_status: dict[str, Any],
    payload: dict[str, Any],
    blocked: bool,
    stop_reason: str,
) -> str:
    if not blocked:
        return ""
    for value in (unified_status.get("blocking_reason"), payload.get("last_error")):
        reason = _clean_text(value)
        if reason:
            return reason
    if blocked and stop_reason and stop_reason != "PASS":
        return stop_reason
    return ""


def _resolve_terminal_round_outcome(payload: dict[str, Any], *, stop_reason: str, blocked: bool) -> str:
    outcome = _last_round_outcome(payload)
    if outcome:
        return outcome
    reason = _clean_text(payload.get("reason")).upper()
    if reason in _LOOP_REASON_ROUND_OUTCOME:
        return _LOOP_REASON_ROUND_OUTCOME[reason]
    if stop_reason == "PASS":
        return "ready_for_gate"
    if blocked:
        return "blocked"
    return "stopped"


def build_loop_outcome(
    payload: dict[str, Any],
    *,
    unified_status: dict[str, Any],
    stop_reason: str,
    blocked: bool,
    must_fix_remaining: int,
) -> dict[str, Any]:
    return {
        "reason": _clean_text(payload.get("reason")),
        "stop_reason": stop_reason,
        "blocked": blocked,
        "blocking_reason": _resolve_blocking_reason(
            unified_status=unified_status,
            payload=payload,
            blocked=blocked,
            stop_reason=stop_reason,
        ),
        "round_outcome": _resolve_terminal_round_outcome(
            payload,
            stop_reason=stop_reason,
            blocked=blocked,
        ),
        "exit_category": _resolve_exit_category(stop_reason, blocked),
        "terminal": bool(payload.get("stopped", False)),
        "stage_status": _clean_text(unified_status.get("stage_status")),
        "review_rounds_used": int(payload.get("review_rounds_used", 0) or 0),
        "must_fix_remaining": must_fix_remaining,
        "gate_recommendation": _clean_text(payload.get("gate_recommendation")),
    }

"""Gate and effective-outcome helpers for workflow-chain runtime."""

from __future__ import annotations

from typing import Any


READY_FOR_GATE_STATUS = "READY_FOR_GATE"
READY_FOR_GATE_REASON = "AWAITING_ADVISORY_GATE"


def gate_summary(payload: dict[str, Any] | None) -> dict[str, Any]:
    gate_payload = dict(payload or {})
    return {
        "total_status": str(gate_payload.get("total_status") or "UNKNOWN").upper(),
        "blocking_violations": int(gate_payload.get("blocking_violations", 0) or 0),
        "total_violations": int(gate_payload.get("total_violations", 0) or 0),
        "profile": _gate_profile_name(gate_payload),
    }


def resolve_gate_blocking_reason(
    gate_payload: dict[str, Any] | None,
    *,
    gate_summary: dict[str, Any] | None = None,
) -> str:
    payload = dict(gate_payload or {})
    summary = gate_summary if gate_summary is not None else _resolved_gate_summary(payload)
    return (
        _explicit_gate_blocking_reason(payload)
        or _first_violation_message(payload)
        or _blocking_violation_fallback(summary)
    )


def runtime_gate_payload(autopilot_payload: dict[str, Any]) -> dict[str, Any] | None:
    payload = autopilot_payload.get("gate_check")
    return dict(payload) if isinstance(payload, dict) else None


def blocked_final_quality_review(payload: Any, *, blocking_reason: str) -> dict[str, Any]:
    review = dict(payload) if isinstance(payload, dict) else {}
    review.update(
        {
            "review_source": "advisory_gate_overlay",
            "status": "BLOCKED",
            "summary": "Advisory gate blocked after workflow chain completion.",
            "blocking_reason": blocking_reason,
        }
    )
    return review


def normalized_chain_final_outcome(payload: Any) -> dict[str, Any]:
    return dict(payload) if isinstance(payload, dict) else {}


def default_effective_final_outcome(chain_final_outcome: dict[str, Any]) -> dict[str, Any]:
    return dict(chain_final_outcome)


def workflow_chain_outcome(chain: dict[str, Any]) -> dict[str, Any]:
    raw = chain.get("chain_final_outcome")
    if isinstance(raw, dict):
        return dict(raw)
    return normalized_chain_final_outcome(chain.get("final_outcome"))


def effective_final_outcome(
    chain_final_outcome: dict[str, Any],
    *,
    gate_payload: dict[str, Any] | None,
    state_payload: dict[str, Any] | None,
    gate_summary: dict[str, Any],
) -> dict[str, Any]:
    if not chain_outcome_passed(chain_final_outcome):
        return dict(chain_final_outcome)
    if gate_summary_blocked(gate_summary) or state_final_status(state_payload) == "BLOCKED":
        return _blocked_final_outcome(
            chain_final_outcome,
            blocking_reason=_effective_blocking_reason(gate_payload, state_payload, gate_summary),
        )
    if gate_summary_passed(gate_summary) or state_final_status(state_payload) == "PASS":
        return dict(chain_final_outcome)
    return dict(chain_final_outcome)


def chain_outcome_passed(chain_final_outcome: dict[str, Any]) -> bool:
    return str(chain_final_outcome.get("status") or "").upper() == "PASS"


def effective_outcome_blocked(payload: dict[str, Any]) -> bool:
    return str(payload.get("status") or "").upper() == "BLOCKED"


def effective_outcome_blocking_reason(payload: dict[str, Any]) -> str:
    return str(payload.get("blocking_reason") or "").strip()


def _gate_profile_name(gate_payload: dict[str, Any]) -> str:
    profile = gate_payload.get("profile")
    if isinstance(profile, dict):
        return str(profile.get("name") or "")
    return str(profile or "")


def _resolved_gate_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return gate_summary(payload)


def _explicit_gate_blocking_reason(payload: dict[str, Any]) -> str:
    return str(payload.get("blocking_reason") or "").strip()


def _blocking_violation_fallback(summary: dict[str, Any]) -> str:
    blocked = int(summary.get("blocking_violations", 0) or 0)
    if blocked > 0:
        return f"Advisory gate blocked ({blocked} blocking violations)"
    return "Advisory gate blocked"


def _first_violation_message(payload: dict[str, Any]) -> str:
    for violation in _gate_violations(payload):
        message = _violation_message(violation)
        if message:
            return message
    return ""


def _gate_violations(payload: dict[str, Any]):
    for item in _gate_items(payload):
        yield from _item_violations(item)


def _gate_items(payload: dict[str, Any]):
    for item in list(payload.get("items") or []):
        if isinstance(item, dict):
            yield item


def _item_violations(item: dict[str, Any]):
    for violation in list(item.get("violations") or []):
        if isinstance(violation, dict):
            yield violation


def _violation_message(violation: dict[str, Any]) -> str:
    message = str(violation.get("message") or "").strip()
    path = str(violation.get("path") or "").strip()
    if message and path:
        return f"{path}: {message}"
    return message


def _blocked_final_outcome(payload: Any, *, blocking_reason: str) -> dict[str, Any]:
    outcome = dict(payload) if isinstance(payload, dict) else {}
    outcome.update(
        {
            "status": "BLOCKED",
            "reason": "ADVISORY_GATE_BLOCKED",
            "blocking_reason": blocking_reason,
        }
    )
    return outcome


def _ready_for_gate_outcome(chain_final_outcome: dict[str, Any]) -> dict[str, Any]:
    outcome = dict(chain_final_outcome)
    outcome.update(
        {
            "status": READY_FOR_GATE_STATUS,
            "reason": READY_FOR_GATE_REASON,
            "blocking_reason": "",
        }
    )
    return outcome


def gate_summary_passed(summary: dict[str, Any]) -> bool:
    return str(summary.get("total_status") or "").upper() == "PASS"


def gate_summary_blocked(summary: dict[str, Any]) -> bool:
    return str(summary.get("total_status") or "").upper() == "BLOCKED"


def state_final_status(state_payload: dict[str, Any] | None) -> str:
    if not isinstance(state_payload, dict):
        return ""
    unified = state_payload.get("unified_status")
    if isinstance(unified, dict):
        status = str(unified.get("final_status") or "").upper()
        if status:
            return status
    return str(state_payload.get("final_status") or "").upper()


def _state_blocking_reason(state_payload: dict[str, Any] | None) -> str:
    if not isinstance(state_payload, dict):
        return ""
    unified = state_payload.get("unified_status")
    if isinstance(unified, dict):
        reason = str(unified.get("blocking_reason") or "").strip()
        if reason:
            return reason
    return str(state_payload.get("last_error") or "").strip()


def _effective_blocking_reason(
    gate_payload: dict[str, Any] | None,
    state_payload: dict[str, Any] | None,
    summary: dict[str, Any],
) -> str:
    return (
        resolve_gate_blocking_reason(gate_payload, gate_summary=summary)
        or _state_blocking_reason(state_payload)
        or "Advisory gate blocked"
    )

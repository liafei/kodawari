"""Runtime semantics helpers extracted from collaboration primitives."""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.engine.loop_outcome import build_loop_outcome
from kodawari.autopilot.review_runtime_policy import classify_review_runtime


_ROUND_OUTCOME_BY_STATUS: dict[str, str] = {
    "ok": "success",
    "pass": "success",
    "ready": "ready_for_gate",
    "changes_requested": "needs_fix",
    "blocked": "blocked",
    "error": "error",
    "max_cycles": "blocked",
    "unknown_action": "unsupported",
}

_LOOP_REASON_STOP_REASON: dict[str, str] = {
    "PROCEED_TO_GATE": "PASS",
    "PIPELINE_FINISH": "PASS",
    "VERIFY_BLOCKED": "HARD_ERROR",
    "GATE_BLOCKED": "HARD_ERROR",
    "OPUS_REVIEW_BLOCKED": "HARD_ERROR",
    "SELF_REVIEW_BLOCKED": "HARD_ERROR",
    "EXECUTION_BACKEND_BLOCKED": "HARD_ERROR",
    "MAX_CYCLES_REACHED": "MAX_CYCLES",
    "COLLABORATION_ROUND_LIMIT": "STUCK",
    "PROTECTED_FILE_BLOCK": "HARD_ERROR",
    "IMPLEMENTATION_ERROR": "HARD_ERROR",
    "TOKEN_BUDGET_EXCEEDED": "TOKEN_BUDGET",
}


def update_round_record_outcome(
    round_record: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    round_record["assigned_role_after"] = context.assigned_role.value
    round_record["review_round_after"] = int(context.review_feedback.review_iteration)
    round_record["must_fix_remaining"] = len(context.review_feedback.must_fix)
    round_record["gate_recommendation"] = context.review_feedback.gate_recommendation
    round_record["round_outcome"] = _resolve_round_outcome(
        stage_status=round_record.get("stage_status"),
        gate_recommendation=round_record.get("gate_recommendation"),
    )
    return round_record


def merge_loop_result_optionals(
    payload: dict[str, Any],
    *,
    pre_compact: dict[str, Any] | None = None,
    post_execution_qa: dict[str, Any] | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    if pre_compact:
        payload["pre_compact"] = dict(pre_compact)
    if post_execution_qa:
        payload["post_execution_qa"] = dict(post_execution_qa)
    if last_error:
        payload["last_error"] = last_error
    _apply_runtime_semantics(payload)
    return payload


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _resolve_round_outcome(stage_status: Any, gate_recommendation: Any) -> str:
    status = _clean_text(stage_status).lower()
    if status in _ROUND_OUTCOME_BY_STATUS:
        return _ROUND_OUTCOME_BY_STATUS[status]
    recommendation = _clean_text(gate_recommendation).upper()
    if recommendation == "PROCEED_TO_GATE":
        return "ready_for_gate"
    if recommendation == "REVIEW_FIX_REQUIRED":
        return "needs_fix"
    return "in_progress"


def _resolve_merged_absorption_status(payload: dict[str, Any]) -> dict[str, str]:
    pre_compact = _dict_or_empty(payload.get("pre_compact"))
    runtime_compact = _dict_or_empty(payload.get("context_compact_runtime"))
    pre_runtime = _dict_or_empty(pre_compact.get("runtime"))
    for item in (
        runtime_compact.get("merged_absorption_status"),
        pre_compact.get("merged_absorption_status"),
        pre_runtime.get("merged_absorption_status"),
    ):
        if isinstance(item, dict):
            return {str(key): str(value) for key, value in item.items()}
    return {}


def _resolve_loop_stop_reason(payload: dict[str, Any]) -> str:
    unified_status = _dict_or_empty(payload.get("unified_status"))
    explicit = _clean_text(unified_status.get("stop_reason"))
    if explicit:
        return explicit
    reason = _clean_text(payload.get("reason")).upper()
    return _LOOP_REASON_STOP_REASON.get(reason, "")


def _build_loop_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    unified_status = _dict_or_empty(payload.get("unified_status"))
    stop_reason = _resolve_loop_stop_reason(payload)
    must_fix_items = payload.get("must_fix_open_items")
    must_fix_remaining = len(must_fix_items) if isinstance(must_fix_items, list) else 0
    if stop_reason == "PASS":
        blocked = False
    else:
        blocked = bool(unified_status.get("is_blocked", False))
    if not blocked and stop_reason != "PASS":
        blocked = stop_reason in {"HARD_ERROR", "STUCK", "MAX_CYCLES"}
    return build_loop_outcome(
        payload,
        unified_status=unified_status,
        stop_reason=stop_reason,
        blocked=blocked,
        must_fix_remaining=must_fix_remaining,
    )


def _build_peer_review_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    summary = _dict_or_empty(payload.get("peer_review_summary"))
    runtime_classification = classify_review_runtime(
        {
            "mode": summary.get("latest_review_mode"),
            "real_requested": summary.get("real_review_requested"),
            "real_required": summary.get("real_review_required"),
            "fallback_used": summary.get("real_review_fallback_used"),
        },
        require_real_peer_review=bool(summary.get("real_review_required")),
    )
    return {
        "review_count": int(summary.get("review_count", 0) or 0),
        "approved": bool(summary.get("approved", False)),
        "review_round": int(summary.get("review_round", 0) or 0),
        "must_fix_remaining": int(summary.get("must_fix_remaining", 0) or 0),
        "last_gate_recommendation": _clean_text(summary.get("last_gate_recommendation")),
        "source": _clean_text(summary.get("latest_source")),
        "mode": _clean_text(summary.get("latest_review_mode")),
        "real_requested": bool(summary.get("real_review_requested")),
        "real_required": bool(summary.get("real_review_required")),
        "fallback_used": bool(summary.get("real_review_fallback_used")),
        "error": _clean_text(summary.get("real_review_error")),
        "review_quality": _clean_text(summary.get("review_quality")) or runtime_classification.review_quality,
        "semantic_review_performed": bool(
            summary.get("semantic_review_performed", runtime_classification.semantic_review_performed)
        ),
    }


def _build_verify_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    verify = _dict_or_empty(payload.get("verify_check"))
    return {
        "status": _clean_text(verify.get("status")),
        "passed": bool(verify.get("passed")),
        "mode": _clean_text(verify.get("mode")),
        "source": _clean_text(verify.get("source")),
        "verify_cmd": _clean_text(verify.get("verify_cmd")),
        "verify_cmd_resolved": _clean_text(verify.get("verify_cmd_resolved")),
        "target_source": _clean_text(verify.get("verify_target_source")),
        "targets": [str(item) for item in list(verify.get("verify_targets") or []) if str(item).strip()],
        "instinct_reason": _clean_text(verify.get("instinct_reason")),
        "instinct_patterns": [str(item) for item in list(verify.get("instinct_patterns") or []) if str(item).strip()],
        "command_executed": bool(verify.get("command_executed")),
        "returncode": verify.get("returncode"),
    }


def _build_gate_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    gate = _dict_or_empty(payload.get("gate_check"))
    return {
        "status": _clean_text(gate.get("total_status")),
        "passed": bool(gate.get("passed")),
        "scope": _clean_text(gate.get("scope")),
        "profile": _clean_text(_dict_or_empty(gate.get("profile")).get("name")),
        "blocking_violations": int(gate.get("blocking_violations", 0) or 0),
        "total_violations": int(gate.get("total_violations", 0) or 0),
    }


def _build_compact_runtime_semantics(
    payload: dict[str, Any],
    *,
    merged_absorption_status: dict[str, str],
) -> dict[str, Any]:
    runtime_compact = _dict_or_empty(payload.get("context_compact_runtime"))
    return {
        "available": bool(runtime_compact),
        "status": _clean_text(runtime_compact.get("status")),
        "mode": _clean_text(runtime_compact.get("mode")),
        "trigger_event": _clean_text(runtime_compact.get("trigger_event")),
        "artifact_state": _clean_text(runtime_compact.get("artifact_state")),
        "include_instincts_requested": bool(runtime_compact.get("include_instincts_requested")),
        "instincts_loaded": bool(runtime_compact.get("instincts_loaded")),
        "instincts_status": _clean_text(runtime_compact.get("instincts_status")),
        "instinct_hints_count": int(runtime_compact.get("instinct_hints_count", 0) or 0),
        "merged_absorption_status": dict(merged_absorption_status),
    }


def _build_semantic_compact_runtime_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = _dict_or_empty(payload.get("semantic_compact_runtime"))
    return {
        "available": bool(runtime),
        "status": _clean_text(runtime.get("status")),
        "mode": _clean_text(runtime.get("mode")),
        "trigger_event": _clean_text(runtime.get("trigger_event")),
        "artifacts": _dict_or_empty(runtime.get("artifacts")),
    }


def _build_self_review_semantics_lazy(payload: dict[str, Any]) -> Any:
    from kodawari.autopilot.review.self_review_semantics import build_self_review_runtime_semantics  # noqa: PLC0415
    return build_self_review_runtime_semantics(payload)


def _apply_runtime_semantics(payload: dict[str, Any]) -> None:
    merged_absorption_status = _resolve_merged_absorption_status(payload)
    payload["merged_absorption_status"] = dict(merged_absorption_status)
    payload["loop_outcome"] = _build_loop_outcome(payload)
    payload["runtime_semantics"] = {
        "peer_review": _build_peer_review_semantics(payload),
        "self_review": _build_self_review_semantics_lazy(payload),
        "verify": _build_verify_semantics(payload),
        "gate": _build_gate_semantics(payload),
        "compact_runtime": _build_compact_runtime_semantics(
            payload,
            merged_absorption_status=merged_absorption_status,
        ),
        "semantic_compact": _build_semantic_compact_runtime_semantics(payload),
    }


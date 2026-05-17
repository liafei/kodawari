"""Review, verify, and approval helpers for workflow-chain runtime."""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.execution.execution_backend import execution_backend_descriptor
from kodawari.autopilot.review_runtime_policy import classify_review_runtime


def verify_payload_from_autopilot(autopilot_payload: dict[str, Any]) -> dict[str, Any]:
    verify = _verify_check_payload(autopilot_payload.get("verify_check"))
    if verify:
        return verify
    return _compat_verify_payload(autopilot_payload.get("post_execution_qa"))


def approval_summary(
    payload: dict[str, Any],
    *,
    peer_review_enabled: bool,
    gate: dict[str, Any],
    verify: dict[str, Any],
) -> dict[str, Any]:
    peer_review_passed = _peer_review_passed(payload, peer_review_enabled)
    self_review_required = _self_review_required(payload)
    self_review_passed = _self_review_passed(
        dict(payload.get("self_review_summary") or {}),
        peer_review_enabled=peer_review_enabled,
        self_review_required=self_review_required,
    )
    verify_passed = _verify_passed(verify)
    gate_passed = _gate_passed(gate)
    return {
        "peer_review": peer_review_passed,
        "self_review": self_review_passed,
        "verify": verify_passed,
        "gate": gate_passed,
        "all_passed": peer_review_passed and self_review_passed and verify_passed and gate_passed,
    }


def peer_review_runtime(payload: dict[str, Any]) -> dict[str, Any]:
    semantics = _runtime_peer_review_semantics(payload)
    if semantics:
        return semantics
    return _summary_peer_review_semantics(payload)


def peer_review_runtime_blocking_reason(peer_review_runtime: dict[str, Any]) -> str:
    runtime = dict(peer_review_runtime or {})
    runtime_classification = classify_review_runtime(
        runtime,
        require_real_peer_review=bool(runtime.get("real_required")),
    )
    if not runtime_classification.real_requested:
        return ""
    if runtime_classification.is_real_review:
        return ""
    if runtime_classification.review_quality == "degraded" and not runtime_classification.real_required:
        return ""
    error = str(runtime.get("error") or "").strip()
    return error or "Real peer review did not complete"


def verify_blocking_reason(verify_payload: dict[str, Any]) -> str:
    if str(verify_payload.get("status") or "").upper() == "PASS":
        return ""
    for key in ("blocking_reason", "summary"):
        reason = str(verify_payload.get(key) or "").strip()
        if reason:
            return reason
    return "Verify blocked during task cycle"


def gate_blocking_reason(gate_summary: dict[str, Any]) -> str:
    if gate_summary["total_status"] == "BLOCKED":
        return "Gate blocked during task cycle"
    return ""


def loop_blocking_reason(autopilot_payload: dict[str, Any]) -> str:
    loop_outcome = autopilot_payload.get("loop_outcome")
    if not isinstance(loop_outcome, dict):
        return ""
    return str(loop_outcome.get("blocking_reason") or "").strip()


def approval_blocking_reason(approvals: dict[str, Any]) -> str:
    if bool(approvals.get("all_passed")):
        return ""
    if approvals.get("peer_review") is False:
        return "Peer review not approved"
    if approvals.get("self_review") is False:
        return "Self review not approved"
    if approvals.get("verify") is False:
        return "Verify not passed"
    if approvals.get("gate") is False:
        return "Rules gate not passed"
    return ""


def final_review_summary(
    *,
    upstream: dict[str, Any],
    task_cycle: dict[str, Any],
) -> dict[str, str]:
    for summary in (_upstream_review_summary(upstream), _task_cycle_review_summary(task_cycle)):
        if summary is not None:
            return summary
    return _pass_review_summary(task_cycle)


def task_cycle_blocked_reason(blocked_task: dict[str, Any] | None) -> str:
    if not isinstance(blocked_task, dict):
        return ""
    return str(blocked_task.get("blocking_reason") or "").strip()


def task_cycle_blocking_reason(task_cycle: dict[str, Any]) -> str:
    return str(task_cycle.get("blocked_reason") or task_cycle.get("blocked_task") or "").strip()


def _runtime_peer_review_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(payload.get("runtime_semantics") or {})
    peer = runtime.get("peer_review")
    if not isinstance(peer, dict):
        return {}
    runtime_classification = classify_review_runtime(
        peer,
        require_real_peer_review=bool(peer.get("real_required")),
    )
    return {
        "mode": str(peer.get("mode") or ""),
        "source": str(peer.get("source") or ""),
        "real_requested": bool(peer.get("real_requested")),
        "real_required": bool(peer.get("real_required")),
        "fallback_used": bool(peer.get("fallback_used")),
        "error": str(peer.get("error") or ""),
        "review_quality": str(peer.get("review_quality") or runtime_classification.review_quality),
        "semantic_review_performed": bool(
            peer.get("semantic_review_performed", runtime_classification.semantic_review_performed)
        ),
    }


def _summary_peer_review_semantics(payload: dict[str, Any]) -> dict[str, Any]:
    summary = dict(payload.get("peer_review_summary") or {})
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
        "mode": str(summary.get("latest_review_mode") or ""),
        "source": str(summary.get("latest_source") or ""),
        "real_requested": bool(summary.get("real_review_requested")),
        "real_required": bool(summary.get("real_review_required")),
        "fallback_used": bool(summary.get("real_review_fallback_used")),
        "error": str(summary.get("real_review_error") or ""),
        "review_quality": str(summary.get("review_quality") or runtime_classification.review_quality),
        "semantic_review_performed": bool(
            summary.get("semantic_review_performed", runtime_classification.semantic_review_performed)
        ),
    }


def _verify_check_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    verify = _verify_payload_base(payload)
    verify.update(_verify_execution_fields(payload))
    return verify


def _compat_verify_payload(payload: Any) -> dict[str, Any]:
    qa_dict = dict(payload) if isinstance(payload, dict) else {}
    status = str(qa_dict.get("status") or ("UNKNOWN" if not qa_dict else "PASS")).upper()
    blocking_reason = str(qa_dict.get("reason") or qa_dict.get("summary") or "")
    if status != "PASS" and not blocking_reason:
        blocking_reason = "verify_check unavailable; compatibility fallback had no canonical verify result"
    verify = {
        "status": status,
        "mode": "compat_post_execution_qa",
        "source": "post_execution_qa_compatibility_fallback",
        "verify_cmd": "",
        "verify_cmd_resolved": "",
        "verify_target_source": "default",
        "verify_targets": [],
        "artifacts": list(qa_dict.get("artifacts") or []),
        "summary": str(qa_dict.get("summary") or ""),
        "blocking_reason": blocking_reason,
    }
    verify.update(_verify_execution_fields({}))
    return verify


def _verify_execution_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "command_executed": bool(payload.get("command_executed")),
        "returncode": payload.get("returncode"),
    }


def _verify_payload_base(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": _payload_upper(payload, "status", default="PASS"),
        "mode": _payload_text(payload, "mode"),
        "source": _payload_text(payload, "source", default="verify_check"),
        "verify_cmd": _payload_text(payload, "verify_cmd"),
        "verify_cmd_resolved": _payload_text(payload, "verify_cmd_resolved"),
        "verify_target_source": _payload_text(payload, "verify_target_source", default="default"),
        "verify_targets": [str(item) for item in _payload_list(payload, "verify_targets") if str(item).strip()],
        "artifacts": _payload_list(payload, "artifacts"),
        "summary": _payload_text(payload, "summary"),
        "blocking_reason": _payload_text(payload, "blocking_reason"),
    }


def _payload_upper(payload: dict[str, Any], key: str, *, default: str) -> str:
    return _payload_text(payload, key, default=default).upper()


def _payload_text(payload: dict[str, Any], key: str, *, default: str = "") -> str:
    value = payload.get(key)
    if value:
        return str(value)
    return default


def _payload_list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if value is None:
        return []
    return list(value)


def _peer_review_passed(payload: dict[str, Any], peer_review_enabled: bool) -> bool:
    if not peer_review_enabled:
        return True
    peer_review = dict(payload.get("peer_review_summary") or {})
    return bool(peer_review.get("approved", False))


def _verify_passed(verify: dict[str, Any]) -> bool:
    return str(verify.get("status") or "").upper() == "PASS"


def _gate_passed(gate: dict[str, Any]) -> bool:
    return gate["total_status"] != "BLOCKED"


def _self_review_passed(
    self_review: dict[str, Any],
    *,
    peer_review_enabled: bool,
    self_review_required: bool,
) -> bool:
    if not peer_review_enabled or not self_review_required:
        return True
    review_count = int(self_review.get("review_count", 0) or 0)
    approved_count = int(self_review.get("approved_count", review_count) or 0)
    if review_count <= 0:
        return False
    return approved_count >= review_count


def _self_review_required(payload: dict[str, Any]) -> bool:
    context = dict(payload.get("collaboration_context") or {})
    if "self_review_required" in context:
        return bool(context.get("self_review_required"))
    capabilities = dict(payload.get("execution_backend_capabilities") or {})
    if "self_review_selectable" in capabilities:
        return bool(capabilities.get("self_review_selectable"))
    backend = str(payload.get("execution_backend") or "").strip().lower()
    if backend:
        return bool(execution_backend_descriptor(backend).self_review_selectable)
    return True


def _upstream_review_summary(upstream: dict[str, Any]) -> dict[str, str] | None:
    if bool(upstream.get("passed")):
        return None
    blocking_reason = (
        peer_review_runtime_blocking_reason(dict(upstream.get("peer_review_runtime") or {}))
        or str(dict(upstream.get("loop_outcome") or {}).get("blocking_reason") or "").strip()
        or str(upstream.get("reason") or "UPSTREAM_BLOCKED")
    )
    return _blocked_review_summary(
        summary="Upstream stage blocked before task auto cycle.",
        blocking_reason=blocking_reason,
    )


def _task_cycle_review_summary(task_cycle: dict[str, Any]) -> dict[str, str] | None:
    if not bool(task_cycle.get("blocked")):
        return None
    blocked_reason = task_cycle_blocking_reason(task_cycle)
    return _blocked_review_summary(
        summary="Task auto cycle stopped on a blocked task.",
        blocking_reason=blocked_reason or "TASK_BLOCKED",
    )


def _pass_review_summary(task_cycle: dict[str, Any]) -> dict[str, str]:
    completed = int(task_cycle.get("tasks_completed", 0) or 0)
    total = int(task_cycle.get("tasks_total", 0) or 0)
    return {
        "status": "PASS",
        "summary": f"Task auto cycle completed {completed}/{total} tasks.",
        "blocking_reason": "",
    }


def _blocked_review_summary(*, summary: str, blocking_reason: str) -> dict[str, str]:
    return {
        "status": "BLOCKED",
        "summary": summary,
        "blocking_reason": blocking_reason,
    }


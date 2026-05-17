"""Reviewer bridge helpers absorbed from workflow-claude modules."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kodawari.autopilot.collaboration import CollaborationRole, enforce_reviewer_boundary
from kodawari.autopilot.core.task_modes import is_verification_only_task
from kodawari.autopilot.execution.execution_artifacts import EXECUTION_RESULT_FILENAME, ExecutionArtifactError, load_execution_result
from kodawari.autopilot.review.review_contract import MISSING_PEER_REVIEW_ISSUE, MISSING_SELF_REVIEW_ISSUE
from kodawari.autopilot.review_runtime_policy import (
    REAL_REVIEW_MODES,
    classify_review_runtime,
    review_quality_grading_enabled,
)


_SEVERITY_ORDER = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
logger = logging.getLogger(__name__)
_EXECUTION_PASS_STATUSES = {"PASS", "DONE", "SUCCESS"}


def _max_severity(reviews: list[dict[str, Any]]) -> str:
    if not reviews:
        return "info"
    max_rank = -1
    resolved = "info"
    for item in reviews:
        value = str(item.get("severity") or "info").strip().lower()
        rank = _SEVERITY_ORDER.get(value, 0)
        if rank > max_rank:
            max_rank = rank
            resolved = value
    return resolved


def _approved_count(entries: list[dict[str, Any]]) -> int:
    return sum(1 for item in entries if bool(item.get("approved", False)))


def _blocking_items_total(entries: list[dict[str, Any]]) -> int:
    return sum(len(list(item.get("blocking_items") or [])) for item in entries)


def _last_gate_recommendation(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return ""
    return str(entries[-1].get("gate_recommendation") or "")


def _reviewers(entries: list[dict[str, Any]]) -> list[str]:
    values = {
        str(item.get("reviewer") or "").strip().lower()
        for item in entries
        if str(item.get("reviewer") or "").strip()
    }
    return sorted(values)


def _max_review_iteration(entries: list[dict[str, Any]]) -> int:
    values = [int(item.get("review_iteration") or 0) for item in entries]
    return max(values, default=0)


def _latest_review(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {}
    latest = entries[-1]
    return dict(latest) if isinstance(latest, dict) else {}


def _latest_review_runtime(entries: list[dict[str, Any]]) -> dict[str, Any]:
    runtime = _latest_review(entries).get("review_runtime")
    return dict(runtime) if isinstance(runtime, dict) else {}


def _latest_review_error_message(entries: list[dict[str, Any]]) -> str:
    runtime = _latest_review_runtime(entries)
    error = runtime.get("error")
    if not isinstance(error, dict):
        return ""
    return str(error.get("message") or "")


def _default_self_review_summary(changed_files: list[str]) -> str:
    if changed_files:
        return "Changed files recorded for review."
    return "No changed files recorded."


def _self_review_entries(reviews: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [dict(item) for item in list(reviews or []) if isinstance(item, dict)]


def summarize_self_review(feature: str, reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entries = _self_review_entries(reviews)
    approved_count = _approved_count(entries)
    latest = entries[-1] if entries else {}
    return {
        "feature": feature,
        "review_count": len(entries),
        "approved_count": approved_count,
        "rejected_count": len(entries) - approved_count,
        "reviewers": _reviewers(entries),
        "actor_boundary_enforced_count": sum(1 for item in entries if bool(item.get("actor_boundary_enforced"))),
        "latest_reviewer": str(latest.get("reviewer") or ""),
        "latest_summary": str(latest.get("summary") or ""),
    }


def run_codex_self_review(feature: str, content: str, reviewer: str = "codex") -> dict[str, Any]:
    summary = str(content or "").strip()
    return {
        "feature": feature,
        "reviewer": reviewer,
        "approved": bool(summary),
        "summary": (summary[:400] if summary else "No concrete changed files were supplied to self-review."),
    }


def summarize_peer_review(
    feature: str,
    reviews: list[dict[str, Any]] | None = None,
    *,
    require_real_peer_review: bool | None = None,
) -> dict[str, Any]:
    """Summarize peer-review entries for downstream consumers.

    ``require_real_peer_review`` is the authoritative policy knob from the
    engine config. When provided it overrides what individual reviewer
    adapters wrote into ``review_runtime.real_required`` — adapters may not
    know the engine's hard-requirement setting, so the engine is the
    source of truth.
    """
    entries = list(reviews or [])
    approved_count = _approved_count(entries)
    changes_requested_count = len(entries) - approved_count
    blocking_items_total = _blocking_items_total(entries)
    last_recommendation = _last_gate_recommendation(entries)
    latest = _latest_review(entries)
    latest_runtime = _latest_review_runtime(entries)
    effective_require_real = (
        bool(require_real_peer_review)
        if require_real_peer_review is not None
        else bool(latest_runtime.get("real_required"))
    )
    runtime_classification = classify_review_runtime(
        latest_runtime,
        require_real_peer_review=effective_require_real,
    )
    # No-fake-run policy Fix 4: empty entries means no peer review ran at
    # all (zero reviewer invocations). The previous default `True` claimed
    # approval without any signal, letting downstream gates accept a run
    # where the reviewer step was silently skipped. Flip to False with an
    # explicit reason so the audit trail records the gap and gates can
    # decide whether to require review evidence.
    return {
        "feature": feature,
        "review_count": len(entries),
        "approved": all(bool(item.get("approved", False)) for item in entries) if entries else False,
        "approved_reason": "" if entries else "no_peer_review_ran",
        "approved_count": approved_count,
        "changes_requested_count": changes_requested_count,
        "blocking_items_total": blocking_items_total,
        "max_severity": _max_severity(entries),
        "last_gate_recommendation": last_recommendation,
        "reviewers": _reviewers(entries),
        "max_review_iteration": _max_review_iteration(entries),
        "latest_source": str(latest.get("source") or ""),
        "latest_review_mode": str(latest_runtime.get("mode") or ""),
        "review_mode": str(latest_runtime.get("mode") or ""),
        "real_review_requested": runtime_classification.real_requested,
        "real_review_required": runtime_classification.real_required,
        "real_review_fallback_used": runtime_classification.fallback_used,
        "fallback_used": runtime_classification.fallback_used,
        "review_quality": runtime_classification.review_quality,
        "semantic_review_performed": runtime_classification.semantic_review_performed,
        "real_review_error": _latest_review_error_message(entries),
    }


def normalize_self_review_payload(
    *,
    payload: Any,
    feature: str,
    changed_files: list[str],
    reviewer: str = "codex",
) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    expected_reviewer = str(reviewer).strip().lower() or "codex"
    expected_role = (
        CollaborationRole.OPUS if expected_reviewer == CollaborationRole.OPUS.value else CollaborationRole.CODEX
    )
    normalized.setdefault("feature", feature)
    normalized = enforce_reviewer_boundary(normalized, expected_reviewer=expected_role)
    normalized.setdefault("approved", False)
    normalized.setdefault("summary", _default_self_review_summary(changed_files))
    status = str(normalized.get("status") or "").strip().upper()
    if status in {"BLOCKED", "FAIL"}:
        normalized["approved"] = False
        normalized.setdefault(
            "blocking_reason",
            str(normalized.get("summary") or "self-review backend blocked"),
        )
    return normalized


def _verification_only_noop(context: dict[str, Any]) -> bool:
    task_card = context.get("task_card")
    return is_verification_only_task(context, task_card if isinstance(task_card, dict) else None)


def _verify_passed(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "").strip().upper()
    if status and status != "PASS":
        return False
    if payload.get("passed") is False:
        return False
    returncode = payload.get("returncode")
    return returncode in (None, 0) and (status == "PASS" or payload.get("passed") is True)


def _verify_evidence_available(context: dict[str, Any], execution_payload: dict[str, Any]) -> bool:
    if _verify_passed(context.get("runtime_verify_check")):
        return True
    if _verify_passed(execution_payload.get("verify_summary")):
        return True
    return bool(str(context.get("verify_cmd") or "").strip())


def run_post_execution_qa(
    feature: str,
    artifacts: list[str] | None = None,
    context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    context_payload = dict(context or {})
    planning_dir = Path(str(context_payload.get("planning_dir") or "")).resolve() if str(context_payload.get("planning_dir") or "").strip() else None
    execution_payload: dict[str, Any] = {}
    execution_status = "MISSING"
    execution_source = "none"
    execution_backend = ""
    if planning_dir is not None:
        try:
            execution_payload = load_execution_result(planning_dir / EXECUTION_RESULT_FILENAME)
        except (ExecutionArtifactError, ValueError):
            logger.warning("post-execution QA could not load execution result artifact", exc_info=True)
            execution_payload = {}
        if execution_payload:
            execution_status = str(execution_payload.get("status") or "UNKNOWN").upper()
            execution_source = EXECUTION_RESULT_FILENAME
            execution_backend = str(execution_payload.get("backend") or "").strip()
    changed = [str(item) for item in list(artifacts or []) if str(item).strip()]
    verification_only_noop = _verification_only_noop(context_payload)
    verify_evidence_available = _verify_evidence_available(context_payload, execution_payload)
    changed_files_required = not verification_only_noop
    checks = {
        "changed_files_present": bool(changed),
        "changed_files_required": changed_files_required,
        "verification_only_noop": verification_only_noop,
        "execution_result_present": bool(execution_payload),
        "execution_status": execution_status,
        "execution_backend": execution_backend,
        "verify_command_configured": bool(str(context_payload.get("verify_cmd") or "").strip()),
        "verify_evidence_available": verify_evidence_available,
    }
    passed = execution_status in _EXECUTION_PASS_STATUSES and (
        bool(changed) or (verification_only_noop and verify_evidence_available)
    )
    reason = "" if passed else "execution evidence is incomplete or failed before verify"
    return {
        "feature": feature,
        "artifacts": changed,
        "status": "PASS" if passed else "FAIL",
        "reason": reason,
        "execution_source": execution_source,
        "execution_status": execution_status,
        "execution_backend": execution_backend,
        "checks": checks,
        "options": kwargs,
    }


def validate_dual_review_evidence(
    *,
    codex_self_reviews: list[dict[str, Any]] | None,
    peer_reviews: list[dict[str, Any]] | None,
    must_fix_items: list[str] | None = None,
    must_fix_remaining: int | None = None,
    require_real_peer_review: bool = False,
    require_self_review: bool = True,
    require_peer_review: bool = True,
) -> dict[str, Any]:
    self_entries = _self_review_entries(codex_self_reviews)
    peer_entries = [dict(item) for item in list(peer_reviews or []) if isinstance(item, dict)]
    issues: list[str] = []

    if require_self_review and not self_entries:
        issues.append(MISSING_SELF_REVIEW_ISSUE)
    if require_peer_review and not peer_entries:
        issues.append(MISSING_PEER_REVIEW_ISSUE)

    unresolved_must_fix = [str(item) for item in list(must_fix_items or []) if str(item).strip()]
    if not unresolved_must_fix and int(must_fix_remaining or 0) > 0:
        unresolved_must_fix = [f"{int(must_fix_remaining)} must-fix item(s) unresolved"]
    if unresolved_must_fix:
        issues.append("Must-fix items are still open.")

    latest_runtime = _latest_review_runtime(peer_entries)
    if not review_quality_grading_enabled():
        # Legacy fallback path (kept for emergency rollback via env flag).
        legacy_real_requested = bool(latest_runtime.get("real_requested"))
        legacy_mode = str(latest_runtime.get("mode") or "").strip()
        legacy_fallback_used = bool(latest_runtime.get("fallback_used"))
        legacy_is_real = legacy_mode in REAL_REVIEW_MODES
        if not legacy_is_real:
            if bool(require_real_peer_review):
                issues.append("Real peer review is required but not satisfied.")
            elif legacy_real_requested and not legacy_fallback_used:
                issues.append("Real peer review was requested but gateway mode is not a real review mode.")
    runtime_classification = classify_review_runtime(
        latest_runtime,
        require_real_peer_review=require_real_peer_review,
    )
    if review_quality_grading_enabled() and not runtime_classification.is_real_review:
        if runtime_classification.real_required:
            # Hard requirement — block regardless of fallback.
            issues.append("Real peer review is required but not satisfied.")
        elif runtime_classification.real_requested and runtime_classification.review_quality != "degraded":
            # Was explicitly requested but ran in a non-real mode without a
            # declared fallback (unexpected configuration mismatch).
            issues.append("Real peer review was requested but gateway mode is not a real review mode.")

    passed = not issues
    return {
        "status": "PASS" if passed else "FAIL",
        "blocking_reason": "" if passed else issues[0],
        "issues": issues,
        "checks": {
            "self_review_count": len(self_entries),
            "peer_review_count": len(peer_entries),
            "must_fix_remaining": len(unresolved_must_fix),
            "required_self_review": bool(require_self_review),
            "required_peer_review": bool(require_peer_review),
            "real_requested": runtime_classification.real_requested,
            "require_real_peer_review": bool(require_real_peer_review),
            "latest_review_mode": runtime_classification.mode,
            "review_quality": runtime_classification.review_quality,
            "semantic_review_performed": runtime_classification.semantic_review_performed,
        },
    }


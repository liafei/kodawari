"""Reviewer boundary and collaboration state transition helpers."""

from __future__ import annotations

import logging
from typing import Any, MutableMapping

from .collaboration_core import _normalize_opus_review_payload, normalize_reviewer_feedback
from .collaboration_flow import (
    apply_rules_gate_result,
    apply_self_review_result,
    apply_verify_result,
)
from .collaboration_types import (
    CollaborationAction,
    CollaborationContext,
    CollaborationRole,
    ReviewFeedback,
    _feedback_snapshot,
    _merge_unique_items,
    _utc_now_iso,
)

logger = logging.getLogger(__name__)


def enforce_reviewer_boundary(
    feedback: dict[str, Any],
    *,
    expected_reviewer: CollaborationRole,
) -> dict[str, Any]:
    normalized = dict(feedback)
    expected = expected_reviewer.value
    existing_original = str(normalized.get("original_reviewer") or "").strip().lower()
    provided_reviewer = str(normalized.get("reviewer") or "").strip().lower()
    if provided_reviewer and provided_reviewer != expected:
        normalized["actor_boundary_enforced"] = True
        normalized["original_reviewer"] = existing_original or provided_reviewer
    else:
        normalized["actor_boundary_enforced"] = bool(normalized.get("actor_boundary_enforced", False))
    normalized["reviewer"] = expected
    return normalized


def review_round_limit_reached(
    *,
    action: CollaborationAction,
    context: CollaborationContext,
    rounds_limit: int,
) -> bool:
    if action not in {CollaborationAction.PEER_REVIEW, CollaborationAction.OPUS_REVIEW}:
        return False
    if context.review_feedback.approved:
        return False
    review_round = int(context.review_feedback.review_iteration or 0)
    return review_round >= int(rounds_limit)


def record_opus_review(
    context: CollaborationContext,
    *,
    approved: bool,
    summary: str | None = None,
    must_fix: list[str] | None = None,
    should_fix: list[str] | None = None,
    blocking_items: list[str] | None = None,
    severity: str | None = None,
    score: int | None = None,
    target_score: int | None = None,
    min_dimension_score: int | None = None,
    dimension_scores: dict[str, int] | None = None,
    gate_recommendation: str | None = None,
    global_consistency_verdict: str | None = None,
    local_implementation_verdict: str | None = None,
    global_failure_attribution: str | None = None,
    deterministic_finding_responses: list[dict[str, Any]] | None = None,
    evidence_refs: list[dict[str, str]] | None = None,
    state_payload: MutableMapping[str, Any] | None = None,
) -> CollaborationContext:
    next_iteration = context.review_feedback.review_iteration + 1
    context.review_feedback = normalize_reviewer_feedback(
        _normalize_opus_review_payload(
            approved=approved,
            summary=summary,
            must_fix=must_fix,
            should_fix=should_fix,
            blocking_items=blocking_items,
            severity=severity,
            score=score,
            target_score=target_score,
            min_dimension_score=min_dimension_score,
            dimension_scores=dimension_scores,
            gate_recommendation=gate_recommendation,
            global_consistency_verdict=global_consistency_verdict,
            local_implementation_verdict=local_implementation_verdict,
            global_failure_attribution=global_failure_attribution,
            deterministic_finding_responses=deterministic_finding_responses,
            evidence_refs=evidence_refs,
            review_iteration=next_iteration,
        ),
        review_iteration=next_iteration,
    )
    if str(context.review_feedback.global_consistency_verdict or "").upper() == "FAIL":
        scope = str(getattr(context, "review_scope", "") or "single_task").strip().lower()
        if scope == "single_task":
            if _should_override_for_single_task(context):
                _apply_global_consistency_override(context, next_iteration)
            else:
                logger.debug(
                    "review_scope=single_task: global_consistency_verdict=FAIL ignored "
                    "(attribution=%s; not a local defect)",
                    context.review_feedback.global_failure_attribution or "missing",
                )
        else:
            _apply_global_consistency_override(context, next_iteration)
    context.review_history.append(_feedback_snapshot(context.review_feedback))
    context.assigned_role = CollaborationRole.CODEX
    context.self_review_completed = False
    context.self_review_approved = False
    context.updated_at = _utc_now_iso()
    if state_payload is not None:
        sync_collaboration_to_state(state_payload, context)
    return context


def request_executor_recovery(
    context: CollaborationContext,
    *,
    blocking_reason: str,
    summary: str,
    blocking_items: list[str] | None = None,
    gate_recommendation: str = "REVIEW_FIX_REQUIRED",
    source: str = "stall_recovery",
    state_payload: MutableMapping[str, Any] | None = None,
) -> CollaborationContext:
    """Route the next collaboration action to FIX_ROUND from an *internal* recovery path
    (executor stall, verify-setup retry) without consuming reviewer round budget.

    Unlike `record_opus_review`, this helper:
      - does NOT increment `review_feedback.review_iteration`
      - does NOT append to `review_history`
    so `review_round_limit_reached` keeps reflecting real external reviewer turns only.

    The next action still resolves to FIX_ROUND because `must_fix` is set to a single
    entry; `next_action()` already routes on `has_must_fix` regardless of source.
    """
    feedback = context.review_feedback
    feedback.approved = False
    feedback.must_fix = [str(blocking_reason)]
    feedback.should_fix = list(feedback.should_fix or [])
    feedback.blocking_items = list(blocking_items or [str(blocking_reason)])
    feedback.gate_recommendation = gate_recommendation
    feedback.summary = str(summary or "")
    # `source` is a free-form telemetry marker on the feedback (e.g. "stall_recovery")
    # so downstream consumers can filter synthetic entries from real reviewer turns.
    if hasattr(feedback, "source"):
        feedback.source = source
    context.assigned_role = CollaborationRole.CODEX
    context.self_review_completed = False
    context.self_review_approved = False
    context.updated_at = _utc_now_iso()
    if state_payload is not None:
        sync_collaboration_to_state(state_payload, context)
    return context


_LEGACY_INVARIANT_KEYWORDS = ("invariant", "forbidden_change", "layer boundary", "dependency violation")


def _should_override_for_single_task(context: CollaborationContext) -> bool:
    """Decide whether a single_task review should escalate global FAIL into a hard block.

    Structured rule (preferred): trust the reviewer-provided
    ``global_failure_attribution`` enum:
      - this_task -> override (real defect in this diff)
      - sibling_tasks -> do NOT override (gap is upstream/downstream)
      - unknown -> do NOT override (conservative; a real defect should be
        attributable; if the reviewer cannot attribute it, default to "let
        single_task pass" so sibling-gap noise does not deadlock the loop)

    Legacy fallback (transitional): when the reviewer did NOT emit the
    attribution field (older models / older prompts), fall back to the original
    blocking_items keyword scan and emit a deprecation warning. This keeps the
    pipeline working while we migrate all reviewers, but the substring rule is
    no longer the source of truth.
    """
    attribution = str(context.review_feedback.global_failure_attribution or "").strip().lower()
    if attribution:
        return attribution == "this_task"
    logger.warning(
        "reviewer did not emit global_failure_attribution while global_consistency_verdict=FAIL; "
        "falling back to legacy blocking_items keyword scan. Update the reviewer prompt to "
        "include the structured attribution field."
    )
    return any(
        any(kw in item.lower() for kw in _LEGACY_INVARIANT_KEYWORDS)
        for item in (context.review_feedback.blocking_items or [])
    )


def _apply_global_consistency_override(
    context: CollaborationContext, next_iteration: int
) -> None:
    override_reason = (
        f"global_consistency_verdict=FAIL overrides approved "
        f"(review_iteration={next_iteration})"
    )
    logger.warning("review_override: %s", override_reason)
    context.review_feedback.approved = False
    context.review_feedback.review_override_reason = override_reason
    _merge_unique_items(context.review_feedback.blocking_items, ["global consistency check failed"])
    _merge_unique_items(context.review_feedback.must_fix, ["Resolve global consistency conflicts before proceeding"])
    context.review_feedback.gate_recommendation = "REVIEW_FIX_REQUIRED"


def record_rules_gate_result(
    context: CollaborationContext,
    *,
    passed: bool,
    state_payload: MutableMapping[str, Any] | None = None,
) -> CollaborationContext:
    apply_rules_gate_result(
        context,
        passed=passed,
        opus_role=CollaborationRole.OPUS,
        system_role=CollaborationRole.SYSTEM,
        codex_role=CollaborationRole.CODEX,
    )
    context.updated_at = _utc_now_iso()
    if state_payload is not None:
        sync_collaboration_to_state(state_payload, context)
    return context


def record_verify_result(
    context: CollaborationContext,
    *,
    passed: bool,
    state_payload: MutableMapping[str, Any] | None = None,
) -> CollaborationContext:
    apply_verify_result(
        context,
        passed=passed,
        system_role=CollaborationRole.SYSTEM,
        codex_role=CollaborationRole.CODEX,
    )
    context.updated_at = _utc_now_iso()
    if state_payload is not None:
        sync_collaboration_to_state(state_payload, context)
    return context


def record_codex_self_review_result(
    context: CollaborationContext,
    *,
    approved: bool,
    summary: str | None = None,
    state_payload: MutableMapping[str, Any] | None = None,
) -> CollaborationContext:
    apply_self_review_result(
        context,
        approved=approved,
        summary=summary,
        system_role=CollaborationRole.SYSTEM,
        codex_role=CollaborationRole.CODEX,
    )
    context.updated_at = _utc_now_iso()
    if state_payload is not None:
        sync_collaboration_to_state(state_payload, context)
    return context


def mark_codex_fix_applied(
    context: CollaborationContext,
    *,
    resolution_summary: str | None = None,
    state_payload: MutableMapping[str, Any] | None = None,
) -> CollaborationContext:
    resolved_at = _utc_now_iso()
    context.fix_history.append(
        {
            "review_iteration": int(context.review_feedback.review_iteration),
            "actor": CollaborationRole.CODEX.value,
            "resolved_must_fix": list(context.review_feedback.must_fix),
            "resolution_summary": resolution_summary,
            "resolved_at": resolved_at,
        }
    )
    context.review_feedback = ReviewFeedback(
        approved=False,
        reviewer=CollaborationRole.CODEX,
        summary=resolution_summary,
        must_fix=[],
        should_fix=list(context.review_feedback.should_fix),
        blocking_items=[],
        severity="info",
        score=context.review_feedback.score,
        target_score=context.review_feedback.target_score,
        min_dimension_score=context.review_feedback.min_dimension_score,
        dimension_scores=dict(context.review_feedback.dimension_scores),
        gate_recommendation="REVIEW_PENDING",
        source=context.review_feedback.source,
        reviewed_at=resolved_at,
        review_iteration=context.review_feedback.review_iteration,
    )
    context.assigned_role = CollaborationRole.OPUS
    context.verify_passed = False
    context.rules_gate_passed = False
    context.self_review_completed = False
    context.self_review_approved = False
    context.updated_at = resolved_at
    if state_payload is not None:
        sync_collaboration_to_state(state_payload, context)
    return context


def sync_collaboration_to_state(
    state_payload: MutableMapping[str, Any],
    context: CollaborationContext,
) -> MutableMapping[str, Any]:
    """Persist collaboration data onto a generic state payload.

    This helper intentionally accepts a plain mutable mapping so it can
    integrate with either legacy StateManager payloads or newer
    AutopilotState-style payloads without changing state.py.
    """

    state_payload["collaboration_context"] = context.to_dict()
    state_payload["architecture_decisions"] = [item.to_dict() for item in context.architecture_decisions]
    return state_payload


__all__ = [
    "enforce_reviewer_boundary",
    "mark_codex_fix_applied",
    "record_codex_self_review_result",
    "record_opus_review",
    "record_rules_gate_result",
    "record_verify_result",
    "request_executor_recovery",
    "review_round_limit_reached",
    "sync_collaboration_to_state",
]

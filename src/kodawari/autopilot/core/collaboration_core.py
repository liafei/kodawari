"""Collaboration builders and review normalization helpers."""

from __future__ import annotations

import re
from typing import Any

from .collaboration_types import (
    ArchitectureDecision,
    CollaborationAction,
    CollaborationContext,
    CollaborationRole,
    ReviewFeedback,
    TaskRouter,
    _build_context_constraints,
    _clean_text,
    _deterministic_finding_responses,
    _evidence_refs,
    _merge_unique_items,
    _optional_int,
    _string_int_map,
    _string_list,
    _normalize_verdict,
    _normalize_attribution,
    _utc_now_iso,
)

_BLOCKING_RE = re.compile(r"(must fix|blocking|critical|blocker)", re.IGNORECASE)
_NON_BLOCKING_RE = re.compile(r"\bnon[- ]?blocking\b", re.IGNORECASE)


def _non_empty_stripped(values: list[str]) -> list[str]:
    cleaned: list[str] = []
    for item in values:
        normalized = str(item).strip()
        if normalized:
            cleaned.append(normalized)
    return cleaned


def _collect_blocking_candidates(review_text: str, must_fix: list[str] | None) -> list[str]:
    lines = [line.strip("- ").strip() for line in str(review_text or "").splitlines()]
    candidates = _non_empty_stripped(lines)
    if must_fix:
        candidates.extend(_non_empty_stripped([str(item) for item in must_fix]))
    return candidates


def _extract_review_lists(data: dict[str, Any]) -> tuple[str, list[str], list[str], list[str]]:
    summary = _clean_text(data.get("summary"))
    must_fix = [item for item in _string_list(data.get("must_fix")) if item.strip()]
    should_fix = [item for item in _string_list(data.get("should_fix")) if item.strip()]
    blocking_items = [item for item in _string_list(data.get("blocking_items")) if item.strip()]
    if blocking_items:
        return summary, must_fix, should_fix, blocking_items
    return summary, must_fix, should_fix, extract_blocking_items(summary, must_fix=must_fix)


def _resolve_review_approved(
    data: dict[str, Any],
    *,
    must_fix: list[str],
    blocking_items: list[str],
) -> bool:
    if must_fix or blocking_items:
        return False
    return bool(data.get("approved", False))


def _resolve_review_severity(
    data: dict[str, Any],
    *,
    must_fix: list[str],
    should_fix: list[str],
    blocking_items: list[str],
) -> str:
    severity = _clean_text(data.get("severity")).lower()
    if severity:
        return severity
    if blocking_items:
        return "critical"
    if must_fix:
        return "high"
    if should_fix:
        return "medium"
    return "info"


def _resolve_gate_recommendation(data: dict[str, Any], *, approved: bool) -> str:
    recommendation = _clean_text(data.get("gate_recommendation")).upper()
    if recommendation:
        return recommendation
    return "PROCEED_TO_GATE" if approved else "REVIEW_FIX_REQUIRED"


def _resolve_reviewer(
    data: dict[str, Any],
    *,
    default_reviewer: CollaborationRole,
) -> CollaborationRole:
    reviewer_value = _clean_text(data.get("reviewer"), default=default_reviewer.value)
    try:
        return CollaborationRole(reviewer_value)
    except ValueError:
        return default_reviewer


def _threshold_blocking_items(
    *,
    score: int | None,
    target_score: int | None,
    min_dimension_score: int | None,
    dimension_scores: dict[str, int],
) -> list[str]:
    return _score_threshold_items(score=score, target_score=target_score) + _dimension_threshold_items(
        min_dimension_score=min_dimension_score,
        dimension_scores=dimension_scores,
    )


def _score_threshold_items(*, score: int | None, target_score: int | None) -> list[str]:
    if score is None or target_score is None or score >= target_score:
        return []
    return [f"Score {score} below target {target_score}"]


def _dimension_threshold_items(
    *,
    min_dimension_score: int | None,
    dimension_scores: dict[str, int],
) -> list[str]:
    if min_dimension_score is None:
        return []
    return [
        f"Dimension {name} score {value} below minimum {min_dimension_score}"
        for name, value in dimension_scores.items()
        if value < min_dimension_score
    ]


def _apply_quality_thresholds(
    *,
    data: dict[str, Any],
    must_fix: list[str],
    blocking_items: list[str],
) -> tuple[int | None, int | None, int | None, dict[str, int], list[str]]:
    score = _optional_int(data.get("score"))
    target_score = _optional_int(data.get("target_score"))
    min_dimension_score = _optional_int(data.get("min_dimension_score"))
    dimension_scores = _string_int_map(data.get("dimension_scores"))
    quality_issues = _threshold_blocking_items(
        score=score,
        target_score=target_score,
        min_dimension_score=min_dimension_score,
        dimension_scores=dimension_scores,
    )
    if quality_issues:
        _merge_unique_items(must_fix, quality_issues)
        _merge_unique_items(blocking_items, quality_issues)
    return score, target_score, min_dimension_score, dimension_scores, quality_issues


def _normalize_opus_review_payload(
    *,
    approved: bool,
    summary: str | None,
    must_fix: list[str] | None,
    should_fix: list[str] | None,
    blocking_items: list[str] | None,
    severity: str | None,
    score: int | None,
    target_score: int | None,
    min_dimension_score: int | None,
    dimension_scores: dict[str, int] | None,
    gate_recommendation: str | None,
    global_consistency_verdict: str | None = None,
    local_implementation_verdict: str | None = None,
    global_failure_attribution: str | None = None,
    deterministic_finding_responses: list[dict[str, Any]] | None = None,
    evidence_refs: list[dict[str, str]] | None = None,
    review_iteration: int = 0,
) -> dict[str, Any]:
    return {
        "approved": approved,
        "reviewer": CollaborationRole.OPUS.value,
        "summary": summary,
        "must_fix": _string_list(must_fix or []),
        "should_fix": _string_list(should_fix or []),
        "blocking_items": _string_list(blocking_items or []),
        "severity": severity,
        "score": score,
        "target_score": target_score,
        "min_dimension_score": min_dimension_score,
        "dimension_scores": _string_int_map(dimension_scores or {}),
        "gate_recommendation": gate_recommendation,
        "global_consistency_verdict": _normalize_verdict(
            global_consistency_verdict,
            allowed={"PASS", "FAIL", "INSUFFICIENT_CONTEXT"},
        ),
        "local_implementation_verdict": _normalize_verdict(
            local_implementation_verdict,
            allowed={"PASS", "FAIL"},
        ),
        "global_failure_attribution": _normalize_attribution(
            global_failure_attribution,
            allowed={"this_task", "sibling_tasks", "unknown"},
        ),
        "deterministic_finding_responses": _deterministic_finding_responses(deterministic_finding_responses or []),
        "evidence_refs": _evidence_refs(evidence_refs or []),
        "reviewed_at": _utc_now_iso(),
        "review_iteration": review_iteration,
    }


def build_collaboration_context(
    task_id: str,
    task_label: str,
    *,
    task_scope: str | None = None,
    architecture_decisions: list[ArchitectureDecision] | None = None,
    implementation_constraints: dict[str, list[str]] | None = None,
    self_review_required: bool = True,
    contract_source: str = "phase1",
) -> CollaborationContext:
    decisions = list(architecture_decisions or [])
    assigned_role = CollaborationRole.CODEX if decisions else TaskRouter.route(task_label, task_scope)
    return CollaborationContext(
        task_id=_clean_text(task_id),
        task_label=_clean_text(task_label),
        assigned_role=assigned_role,
        architecture_decisions=decisions,
        implementation_constraints=_build_context_constraints(implementation_constraints),
        self_review_required=bool(self_review_required),
        contract_source=contract_source,
        updated_at=_utc_now_iso(),
    )


def build_peer_review_policy(**overrides: Any) -> dict[str, Any]:
    """Normalized peer-review policy absorbed from workflow-claude semantics."""

    policy = {
        "target_score": 95,
        "min_dimension_score": 80,
        "max_rounds": 5,
    }
    policy.update(overrides)
    return policy


def build_round_record(
    *,
    round_index: int,
    cycle: int,
    task_id: str,
    task_label: str,
    action: CollaborationAction,
    actor: CollaborationRole,
    context: CollaborationContext,
) -> dict[str, Any]:
    return {
        "round": int(round_index),
        "round_id": f"R{int(round_index):03d}",
        "cycle": int(cycle),
        "task_id": task_id,
        "task_label": task_label,
        "stage": action.value.upper(),
        "action": action.value,
        "actor": actor.value,
        "review_round": int(context.review_feedback.review_iteration),
        "assigned_role_before": context.assigned_role.value,
        "architecture_decisions_total": len(context.architecture_decisions),
        "must_fix_open": len(context.review_feedback.must_fix),
        "details": {},
    }


def extract_blocking_items(review_text: str, *, must_fix: list[str] | None = None) -> list[str]:
    candidates = _collect_blocking_candidates(review_text, must_fix)
    seen: set[str] = set()
    blocking: list[str] = []
    for item in candidates:
        normalized = item.lower()
        is_new = normalized not in seen
        if is_new and _BLOCKING_RE.search(item) and not _NON_BLOCKING_RE.search(item):
            seen.add(normalized)
            blocking.append(item)
    return blocking


def normalize_reviewer_feedback(
    payload: dict[str, Any] | None,
    *,
    default_reviewer: CollaborationRole = CollaborationRole.OPUS,
    review_iteration: int = 0,
    default_source: str = "kodawari",
) -> ReviewFeedback:
    data = dict(payload or {})
    summary, must_fix, should_fix, blocking_items = _extract_review_lists(data)
    score, target_score, min_dimension_score, dimension_scores, quality_issues = (
        _apply_quality_thresholds(
            data=data,
            must_fix=must_fix,
            blocking_items=blocking_items,
        )
    )
    approved = _resolve_review_approved(data, must_fix=must_fix, blocking_items=blocking_items)
    if quality_issues:
        approved = False
    severity = _resolve_review_severity(
        data,
        must_fix=must_fix,
        should_fix=should_fix,
        blocking_items=blocking_items,
    )
    gate_recommendation = _resolve_gate_recommendation(data, approved=approved)
    reviewer = _resolve_reviewer(data, default_reviewer=default_reviewer)
    parsed_iteration = _optional_int(data.get("review_iteration"))

    return ReviewFeedback(
        approved=approved,
        reviewer=reviewer,
        summary=summary or None,
        must_fix=must_fix,
        should_fix=should_fix,
        blocking_items=blocking_items,
        severity=severity,
        score=score,
        target_score=target_score,
        min_dimension_score=min_dimension_score,
        dimension_scores=dimension_scores,
        gate_recommendation=gate_recommendation,
        global_consistency_verdict=_normalize_verdict(
            data.get("global_consistency_verdict"),
            allowed={"PASS", "FAIL", "INSUFFICIENT_CONTEXT"},
        ),
        local_implementation_verdict=_normalize_verdict(
            data.get("local_implementation_verdict"),
            allowed={"PASS", "FAIL"},
        ),
        global_failure_attribution=_normalize_attribution(
            data.get("global_failure_attribution"),
            allowed={"this_task", "sibling_tasks", "unknown"},
        ),
        deterministic_finding_responses=_deterministic_finding_responses(data.get("deterministic_finding_responses")),
        evidence_refs=_evidence_refs(data.get("evidence_refs")),
        source=_clean_text(data.get("source"), default=default_source),
        reviewed_at=_clean_text(data.get("reviewed_at"), default=_utc_now_iso()),
        review_iteration=parsed_iteration or max(0, int(review_iteration)),
    )


__all__ = [
    "build_collaboration_context",
    "build_peer_review_policy",
    "build_round_record",
    "extract_blocking_items",
    "normalize_reviewer_feedback",
]

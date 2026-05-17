"""Canonical collaboration API for the autopilot loop."""

from __future__ import annotations

from kodawari.autopilot.core.collaboration_core import (
    build_collaboration_context,
    build_peer_review_policy,
    build_round_record,
    extract_blocking_items,
    normalize_reviewer_feedback,
)
from kodawari.autopilot.core.collaboration_runtime import (
    merge_loop_result_optionals,
    update_round_record_outcome,
)
from kodawari.autopilot.core.collaboration_types import (
    ArchitectureDecision,
    CollaborationAction,
    CollaborationContext,
    CollaborationRole,
    ReviewFeedback,
    TaskRouter,
)
from kodawari.autopilot.core.reviewer_boundary import (
    enforce_reviewer_boundary,
    mark_codex_fix_applied,
    record_codex_self_review_result,
    record_opus_review,
    record_rules_gate_result,
    record_verify_result,
    request_executor_recovery,
    review_round_limit_reached,
    sync_collaboration_to_state,
)

__all__ = [
    "ArchitectureDecision",
    "CollaborationAction",
    "CollaborationContext",
    "CollaborationRole",
    "ReviewFeedback",
    "TaskRouter",
    "build_collaboration_context",
    "build_peer_review_policy",
    "build_round_record",
    "enforce_reviewer_boundary",
    "extract_blocking_items",
    "mark_codex_fix_applied",
    "merge_loop_result_optionals",
    "normalize_reviewer_feedback",
    "record_codex_self_review_result",
    "record_opus_review",
    "record_rules_gate_result",
    "record_verify_result",
    "request_executor_recovery",
    "review_round_limit_reached",
    "sync_collaboration_to_state",
    "update_round_record_outcome",
]

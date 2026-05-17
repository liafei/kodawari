"""Small state-machine helpers for collaboration flow decisions."""

from __future__ import annotations

from typing import Any


def resolve_next_action(
    *,
    verify_passed: bool,
    rules_gate_passed: bool,
    has_must_fix: bool,
    design_pending: bool,
    implementation_started: bool,
    assigned_role: str,
    peer_review_enabled: bool,
    review_approved: bool,
    self_review_required: bool,
    self_review_completed: bool,
    self_review_approved: bool,
) -> str:
    proceed = _ready_to_proceed(
        rules_gate_passed=rules_gate_passed,
        has_must_fix=has_must_fix,
        peer_review_enabled=peer_review_enabled,
        review_approved=review_approved,
        self_review_required=self_review_required,
        self_review_approved=self_review_approved,
    )
    review_followup = _review_followup_action(
        implementation_started=implementation_started,
        verify_passed=verify_passed,
        rules_gate_passed=rules_gate_passed,
        assigned_role=assigned_role,
        peer_review_enabled=peer_review_enabled,
        review_approved=review_approved,
        self_review_required=self_review_required,
        self_review_completed=self_review_completed,
    )
    return (
        _initial_action(has_must_fix=has_must_fix, design_pending=design_pending, proceed=proceed)
        or review_followup
        or "implement"
    )


def apply_rules_gate_result(
    context: Any,
    *,
    passed: bool,
    opus_role: str,
    system_role: str,
    codex_role: str,
) -> None:
    context.rules_gate_passed = bool(passed)
    if passed:
        context.assigned_role = opus_role if context.peer_review_enabled else system_role
        context.self_review_completed = False
        context.self_review_approved = False
        return
    context.assigned_role = codex_role


def apply_verify_result(
    context: Any,
    *,
    passed: bool,
    system_role: str,
    codex_role: str,
) -> None:
    context.verify_passed = bool(passed)
    context.rules_gate_passed = False
    context.assigned_role = system_role if passed else codex_role


def apply_self_review_result(
    context: Any,
    *,
    approved: bool,
    summary: str | None,
    system_role: str,
    codex_role: str,
) -> None:
    context.self_review_completed = True
    context.self_review_approved = bool(approved)
    if approved:
        context.assigned_role = system_role
        return
    message = summary or "Codex self review requested follow-up changes"
    context.review_feedback.approved = False
    context.review_feedback.must_fix = [message]
    context.review_feedback.blocking_items = [message]
    context.review_feedback.gate_recommendation = "REVIEW_FIX_REQUIRED"
    context.assigned_role = codex_role


def _ready_to_proceed(
    *,
    rules_gate_passed: bool,
    has_must_fix: bool,
    peer_review_enabled: bool,
    review_approved: bool,
    self_review_required: bool,
    self_review_approved: bool,
) -> bool:
    if not rules_gate_passed or has_must_fix:
        return False
    if not peer_review_enabled:
        return True
    if not review_approved:
        return False
    return (not self_review_required) or self_review_approved


def _initial_action(*, has_must_fix: bool, design_pending: bool, proceed: bool) -> str:
    if proceed:
        return "proceed_to_gate"
    if has_must_fix:
        return "fix_round"
    if design_pending:
        return "design"
    return ""


def _review_followup_action(
    *,
    implementation_started: bool,
    verify_passed: bool,
    rules_gate_passed: bool,
    assigned_role: str,
    peer_review_enabled: bool,
    review_approved: bool,
    self_review_required: bool,
    self_review_completed: bool,
) -> str:
    return (
        _verification_followup(
            implementation_started=implementation_started,
            verify_passed=verify_passed,
            rules_gate_passed=rules_gate_passed,
        )
        or _review_followup(assigned_role)
        or _self_review_followup(
            peer_review_enabled=peer_review_enabled,
            review_approved=review_approved,
            self_review_required=self_review_required,
            self_review_completed=self_review_completed,
        )
    )


def _verification_followup(
    *,
    implementation_started: bool,
    verify_passed: bool,
    rules_gate_passed: bool,
) -> str:
    if implementation_started and not verify_passed:
        return "verify"
    if implementation_started and not rules_gate_passed:
        return "rules_gate"
    return ""


def _review_followup(assigned_role: str) -> str:
    return "peer_review" if assigned_role == "opus" else ""


def _self_review_followup(
    *,
    peer_review_enabled: bool,
    review_approved: bool,
    self_review_required: bool,
    self_review_completed: bool,
) -> str:
    if _requires_self_review(
        peer_review_enabled=peer_review_enabled,
        review_approved=review_approved,
        self_review_required=self_review_required,
        self_review_completed=self_review_completed,
    ):
        return "self_review"
    return ""


def _requires_self_review(
    *,
    peer_review_enabled: bool,
    review_approved: bool,
    self_review_required: bool,
    self_review_completed: bool,
) -> bool:
    return peer_review_enabled and review_approved and self_review_required and not self_review_completed

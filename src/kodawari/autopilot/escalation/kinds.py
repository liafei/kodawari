"""EscalationKind enum + classifier.

Maps raw ``FailureEvent`` (executor) / ``planning_diagnostics`` (planner) /
``gate_check`` (gate) signals into one of 12 escalation kinds. Each kind
has an associated Planner prompt template (see planner_prompts.py) and
resume action (see handler.py / engine_session_mixin).
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class EscalationKind(str, Enum):
    """Categories of escalatable workflow failures."""

    # Executor (IMPLEMENT phase)
    EXECUTOR_STUCK = "EXECUTOR_STUCK"
    EXECUTOR_PATCH_BROKEN = "EXECUTOR_PATCH_BROKEN"
    EXECUTOR_PRECONDITION_MISSING = "EXECUTOR_PRECONDITION_MISSING"
    EXECUTOR_MODEL_INCAPABLE = "EXECUTOR_MODEL_INCAPABLE"

    # Gate
    GATE_REFACTOR_NEEDED = "GATE_REFACTOR_NEEDED"
    GATE_FILE_SPLIT_NEEDED = "GATE_FILE_SPLIT_NEEDED"
    GATE_TASK_CARD_DESIGN_BUG = "GATE_TASK_CARD_DESIGN_BUG"
    COMPLIANCE_BLOCK = "COMPLIANCE_BLOCK"

    # Planning
    PLANNING_APPROVAL_REQUIRED = "PLANNING_APPROVAL_REQUIRED"
    PLANNING_DEADLOCK = "PLANNING_DEADLOCK"
    PLANNING_PREREQ_MISSING = "PLANNING_PREREQ_MISSING"
    PLANNING_ENV_FAIL = "PLANNING_ENV_FAIL"

    # Infrastructure
    INFRA_INTERRUPTION = "INFRA_INTERRUPTION"


# --- error_code → kind lookup tables --------------------------------------

_EXECUTOR_STUCK_CODES = {
    "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
    "EXECUTOR_STALLED_REDUNDANT_READS",
    "EXECUTOR_STALLED_FRAGMENTED_READS",
    "EXECUTOR_STALLED_REPEATED_SEARCH",
    "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
    "EXECUTOR_STALLED_BUDGET_PRESSURE",
    "MAX_TOOL_ITERATIONS",
    "MAX_SAME_TOOL_CALLS_PER_PATH",
    "MAX_TOOL_CALLS_PER_RESPONSE",
    "NO_PROGRESS_ABORTED",
    "INVALID_TOOL_CALL",
}

_EXECUTOR_PATCH_BROKEN_CODES = {
    "EXECUTOR_STALLED_PATCH_FAILURES",
    "PATCH_PLAN_MISSING",
    "PATCH_TARGET_MISSING",
    "PATCH_PRECONDITION_MISMATCH",
    "PATCH_OCCURRENCE_MISMATCH",
    "PATCH_PLAN_APPLY_FAILED",
}

_EXECUTOR_PRECONDITION_CODES = {
    "TASK_BLOCKED_BY_PRECONDITION",
}

_GATE_REFACTOR_DETECTORS = {
    "gate_complexity",
    "gate_nesting",
    "duplication",
    "semantics",
}

_GATE_FILE_SPLIT_DETECTORS = {
    "gate_file_length",
    "gate_file_complexity_sum",
}

_GATE_TASK_CARD_DESIGN_DETECTORS = {
    "scope_contract",
    "import_rules",
}

_PLANNING_DEADLOCK_REASONS = {
    "stubborn_round_limit",
    "escalation_required",
    "planner_reviewer_deadlock",
}

_PLANNING_ENV_REASONS = {
    "planner_environment_error:timeout",
    "planner_environment_error:planner_output_truncated_empty",
    "planner_environment_error:planner_http_error",
    "planner_environment_error:rate_limit",
    "planner_environment_error:planning_context_oversize",
}


def classify(
    *,
    failure_event: Any = None,
    planning_diagnostics: dict[str, Any] | None = None,
    gate_check: dict[str, Any] | None = None,
    phase: str = "",
) -> EscalationKind | None:
    """Classify a failure into an EscalationKind.

    Returns ``None`` for non-escalatable failures (config errors / fail-fast).

    Inputs are exclusive: pass exactly one of ``failure_event``,
    ``planning_diagnostics``, or ``gate_check`` based on the workflow phase.
    """
    phase_norm = (phase or "").strip().lower()

    # ---- Planning phase ----
    if planning_diagnostics is not None and phase_norm == "planning":
        run_reason = str(planning_diagnostics.get("run_reason") or "").strip().lower()
        root_cause = str(planning_diagnostics.get("root_cause") or "").strip().lower()
        history = list(planning_diagnostics.get("blocking_findings_history") or [])
        # Plan converged (0 blocking on last round) but config requires approval
        # → distinct kind: PLANNING_APPROVAL_REQUIRED (let user accept as-is,
        # not "split into sub-features" like a true deadlock).
        last_blocking = int(history[-1]) if history else -1
        if root_cause == "approval_required" or run_reason == "approval_required":
            if last_blocking == 0:
                return EscalationKind.PLANNING_APPROVAL_REQUIRED
        # Env-fail codes carry "planner_environment_error:..." prefix
        if run_reason.startswith("planner_environment_error"):
            return EscalationKind.PLANNING_ENV_FAIL
        if root_cause in {"planner_transport_or_output_failure"}:
            return EscalationKind.PLANNING_ENV_FAIL
        if run_reason == "task_input_infeasible" or root_cause == "task_input_infeasible":
            return EscalationKind.PLANNING_PREREQ_MISSING
        # True deadlock: still has blocking findings or stubborn rounds
        if run_reason in _PLANNING_DEADLOCK_REASONS or root_cause == "semantic_closure_failure":
            return EscalationKind.PLANNING_DEADLOCK
        return None

    # ---- Gate phase ----
    if gate_check is not None and phase_norm == "gate":
        items = list(gate_check.get("items") or [])
        # Aggregate first detector hint with violation
        for item in items:
            if not isinstance(item, dict):
                continue
            violations = list(item.get("violations") or [])
            if not violations:
                continue
            checker = str(item.get("checker") or "").strip().lower()
            # Detector hint maps:
            if checker in _GATE_FILE_SPLIT_DETECTORS or "file_length" in checker:
                return EscalationKind.GATE_FILE_SPLIT_NEEDED
            if checker in _GATE_TASK_CARD_DESIGN_DETECTORS:
                return EscalationKind.GATE_TASK_CARD_DESIGN_BUG
            if checker == "compliance":
                return EscalationKind.COMPLIANCE_BLOCK
            if checker in _GATE_REFACTOR_DETECTORS or checker == "function_metrics":
                return EscalationKind.GATE_REFACTOR_NEEDED
        return None

    # ---- Executor phase ----
    if failure_event is not None and phase_norm in {"executor", "implement", ""}:
        error_code = str(getattr(failure_event, "error_code", "") or "").strip().upper()
        detector_hint = str(getattr(failure_event, "detector_hint", "") or "").strip().lower()
        # GATE_BLOCKED from inside executor recovery → route to gate kinds
        if error_code == "GATE_BLOCKED":
            if detector_hint in _GATE_FILE_SPLIT_DETECTORS or detector_hint == "gate_file_length":
                return EscalationKind.GATE_FILE_SPLIT_NEEDED
            if detector_hint in _GATE_TASK_CARD_DESIGN_DETECTORS:
                return EscalationKind.GATE_TASK_CARD_DESIGN_BUG
            if detector_hint == "compliance":
                return EscalationKind.COMPLIANCE_BLOCK
            # default gate → refactor (covers gate_complexity, nesting, etc.)
            return EscalationKind.GATE_REFACTOR_NEEDED
        if error_code in _EXECUTOR_PATCH_BROKEN_CODES:
            return EscalationKind.EXECUTOR_PATCH_BROKEN
        if error_code in _EXECUTOR_PRECONDITION_CODES:
            return EscalationKind.EXECUTOR_PRECONDITION_MISSING
        if error_code in _EXECUTOR_STUCK_CODES:
            return EscalationKind.EXECUTOR_STUCK
        return None

    return None


def is_escalatable(kind: EscalationKind | None) -> bool:
    """Whether a kind allows automatic decide flow (vs. mandatory fail-fast)."""
    return kind is not None


def allows_skip(kind: EscalationKind) -> bool:
    """Whether the 'skip' action is valid for this kind.

    COMPLIANCE_BLOCK and EXECUTOR_PRECONDITION_MISSING require resolution,
    not skipping.
    """
    return kind not in {
        EscalationKind.COMPLIANCE_BLOCK,
        EscalationKind.EXECUTOR_PRECONDITION_MISSING,
    }


__all__ = [
    "EscalationKind",
    "allows_skip",
    "classify",
    "is_escalatable",
]

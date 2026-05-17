"""Deterministic executor recovery detector registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kodawari.autopilot.recovery.failure_event import FailureEvent
from kodawari.autopilot.recovery.gate_recovery import build_gate_complexity_recovery
from kodawari.autopilot.recovery.pytest_recovery import (
    build_pytest_collection_nameerror_recovery,
    build_pytest_verify_failure_recovery,
)
from kodawari.autopilot.recovery.stall_recovery import (
    build_no_write_stall_recovery,
    build_scope_drift_recovery,
    build_tool_call_limit_recovery,
)


@dataclass(frozen=True)
class RecoveryContext:
    project_root: Path
    original_card: dict[str, Any]
    task_id: str
    must_fix: list[str]
    event: FailureEvent


@dataclass(frozen=True)
class DetectorMatch:
    name: str
    priority: int
    decision: dict[str, Any]
    card: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)


Detector = Callable[[RecoveryContext], DetectorMatch | None]
_DETECTORS: list[tuple[int, str, Detector]] = []


def register_detector(*, priority: int, name: str) -> Callable[[Detector], Detector]:
    def decorator(func: Detector) -> Detector:
        _DETECTORS.append((int(priority), str(name), func))
        _DETECTORS.sort(key=lambda item: (item[0], item[1]))
        return func

    return decorator


def iter_detector_matches(context: RecoveryContext):
    for priority, name, detector in list(_DETECTORS):
        match = detector(context)
        if match is not None:
            yield match


def route_deterministic_recovery(context: RecoveryContext) -> DetectorMatch | None:
    for match in iter_detector_matches(context):
        return match
    return None


@register_detector(priority=5, name="task_infeasibility")
def _detect_task_infeasibility(context: RecoveryContext) -> DetectorMatch | None:
    """Route a TASK_BLOCKED_BY_PRECONDITION declaration to a stop-and-replan
    decision. The executor itself signals infeasibility via the
    declare_task_infeasible tool — we do NOT generate a retry card here.
    Instead the decision tells the engine to finish the loop and surface the
    structured missing_preconditions to the planner so the next plan can
    insert the prerequisite work."""

    if context.event.error_code != "TASK_BLOCKED_BY_PRECONDITION":
        return None
    evidence = context.event.evidence or ""
    decision = {
        "schema_version": "execution.recovery_decision.v1",
        "action": "task_blocked_by_precondition",
        "reason": "executor declared task infeasible; missing structural preconditions",
        "source": "kodawari.task_infeasibility_recovery",
        "must_fix": list(context.must_fix),
        "missing_preconditions": list(context.event.affected_paths),
        "evidence": evidence[:1000] if evidence else "",
    }
    # No recovery card — there is nothing the executor can retry that would
    # make the missing column / module / API appear. Returning ``card={}``
    # signals "decision recorded, no retry" so the engine flow can route to
    # finish_loop with stop_reason=STUCK + precondition_blocked telemetry.
    return DetectorMatch(
        name="task_infeasibility",
        priority=5,
        decision=decision,
        card={},
        evidence={
            "error_code": context.event.error_code,
            "missing_preconditions": list(context.event.affected_paths),
            "detector_hint": "task_blocked_by_precondition",
        },
    )


@register_detector(priority=8, name="pytest_verify_failure")
def _detect_pytest_verify_failure(context: RecoveryContext) -> DetectorMatch | None:
    if context.event.error_code not in {"VERIFY_FAILED", "VERIFY_FAILED_RETRYABLE"}:
        return None
    result = build_pytest_verify_failure_recovery(
        project_root=context.project_root,
        original_card=context.original_card,
        task_id=context.task_id,
        must_fix=context.must_fix,
        execution_result=context.event.execution_result,
        verify_check=context.event.verify_check,
        collection_errors=[item.to_dict() for item in context.event.collection_errors],
    )
    return _match("pytest_verify_failure", 8, result, context.event)


@register_detector(priority=10, name="no_write_stall")
def _detect_no_write_stall(context: RecoveryContext) -> DetectorMatch | None:
    if context.event.error_code not in {
        "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED",
        "EXECUTOR_STALLED_FRAGMENTED_READS",
    }:
        return None
    result = build_no_write_stall_recovery(
        project_root=context.project_root,
        original_card=context.original_card,
        task_id=context.task_id,
        must_fix=context.must_fix,
        stall_report=context.event.stall_report,
        execution_result=context.event.execution_result,
    )
    return _match("no_write_stall", 10, result, context.event)


@register_detector(priority=15, name="scope_drift")
def _detect_scope_drift(context: RecoveryContext) -> DetectorMatch | None:
    result = build_scope_drift_recovery(
        project_root=context.project_root,
        original_card=context.original_card,
        task_id=context.task_id,
        must_fix=context.must_fix,
        affected_paths=list(context.event.affected_paths),
    )
    return _match("scope_drift", 15, result, context.event)


@register_detector(priority=20, name="same_path_tool_limit")
def _detect_same_path_tool_limit(context: RecoveryContext) -> DetectorMatch | None:
    if context.event.error_code != "MAX_SAME_TOOL_CALLS_PER_PATH":
        return None
    result = build_tool_call_limit_recovery(
        project_root=context.project_root,
        original_card=context.original_card,
        task_id=context.task_id,
        must_fix=context.must_fix,
        stall_report=context.event.stall_report,
        execution_result=context.event.execution_result,
    )
    return _match("same_path_tool_limit", 20, result, context.event)


@register_detector(priority=30, name="pytest_collection_nameerror")
def _detect_pytest_collection_nameerror(context: RecoveryContext) -> DetectorMatch | None:
    result = build_pytest_collection_nameerror_recovery(
        project_root=context.project_root,
        original_card=context.original_card,
        task_id=context.task_id,
        must_fix=context.must_fix,
        execution_result=context.event.execution_result,
        collection_errors=[item.to_dict() for item in context.event.collection_errors],
    )
    return _match("pytest_collection_nameerror", 30, result, context.event)


@register_detector(priority=40, name="gate_complexity")
def _detect_gate_complexity(context: RecoveryContext) -> DetectorMatch | None:
    if not context.event.verify_passed:
        return None
    result = build_gate_complexity_recovery(
        project_root=context.project_root,
        gate_check=context.event.gate_check,
        original_card=context.original_card,
        task_id=context.task_id,
        must_fix=context.must_fix,
    )
    return _match("gate_complexity", 40, result, context.event)


def _match(
    name: str,
    priority: int,
    result: tuple[dict[str, Any], dict[str, Any]] | None,
    event: FailureEvent,
) -> DetectorMatch | None:
    if result is None:
        return None
    decision, card = result
    evidence = {
        "error_code": event.error_code,
        "affected_paths": list(event.affected_paths),
        "detector_hint": event.detector_hint,
    }
    if event.tool_call_limit is not None:
        evidence["tool_call_limit"] = event.tool_call_limit.to_dict()
    if event.collection_errors:
        evidence["collection_errors"] = [item.to_dict() for item in event.collection_errors]
    return DetectorMatch(name=name, priority=priority, decision=decision, card=card, evidence=evidence)


__all__ = [
    "DetectorMatch",
    "RecoveryContext",
    "iter_detector_matches",
    "register_detector",
    "route_deterministic_recovery",
]

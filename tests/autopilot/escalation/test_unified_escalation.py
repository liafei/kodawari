"""Unit tests for unified escalation system (kinds.classify + handler write/read)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from kodawari.autopilot.escalation import (
    EscalationKind,
    classify,
    escalation_count,
    maybe_escalate,
    read_decision_response,
    write_decision_response,
)
from kodawari.autopilot.escalation.handler import (
    DecisionResponse,
    find_pending_request,
)
from kodawari.autopilot.escalation.kinds import allows_skip, is_escalatable
from kodawari.autopilot.recovery.failure_event import FailureEvent


# ---------------------------------------------------------------------------
# classify() — parametrized across 4 phases × multiple failure types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure_code,detector_hint,expected_kind",
    [
        ("EXECUTOR_STALLED_NO_WRITE_PROGRESS", "no_write_stall", EscalationKind.EXECUTOR_STUCK),
        ("EXECUTOR_STALLED_REDUNDANT_READS", "", EscalationKind.EXECUTOR_STUCK),
        ("MAX_TOOL_ITERATIONS", "", EscalationKind.EXECUTOR_STUCK),
        ("INVALID_TOOL_CALL", "", EscalationKind.EXECUTOR_STUCK),
        ("EXECUTOR_STALLED_PATCH_FAILURES", "", EscalationKind.EXECUTOR_PATCH_BROKEN),
        ("PATCH_PLAN_MISSING", "", EscalationKind.EXECUTOR_PATCH_BROKEN),
        ("TASK_BLOCKED_BY_PRECONDITION", "", EscalationKind.EXECUTOR_PRECONDITION_MISSING),
        ("GATE_BLOCKED", "gate_complexity", EscalationKind.GATE_REFACTOR_NEEDED),
        ("GATE_BLOCKED", "gate_nesting", EscalationKind.GATE_REFACTOR_NEEDED),
        ("GATE_BLOCKED", "gate_file_length", EscalationKind.GATE_FILE_SPLIT_NEEDED),
        ("GATE_BLOCKED", "scope_contract", EscalationKind.GATE_TASK_CARD_DESIGN_BUG),
        ("GATE_BLOCKED", "import_rules", EscalationKind.GATE_TASK_CARD_DESIGN_BUG),
        ("GATE_BLOCKED", "compliance", EscalationKind.COMPLIANCE_BLOCK),
        ("UNKNOWN_CODE", "", None),  # not escalatable
    ],
)
def test_classify_executor(failure_code, detector_hint, expected_kind):
    event = FailureEvent(
        phase="executor",
        error_code=failure_code,
        detector_hint=detector_hint,
    )
    kind = classify(failure_event=event, phase="executor")
    assert kind == expected_kind, f"{failure_code}/{detector_hint} -> {kind} != {expected_kind}"


@pytest.mark.parametrize(
    "run_reason,root_cause,history,expected_kind",
    [
        ("stubborn_round_limit", "", [3, 2, 4, 3], EscalationKind.PLANNING_DEADLOCK),
        ("escalation_required", "semantic_closure_failure", [3, 2, 4, 3], EscalationKind.PLANNING_DEADLOCK),
        # NEW: approval_required + 0 blocking on last round → APPROVAL_REQUIRED, not deadlock
        ("escalation_required", "approval_required", [3, 2, 1, 0], EscalationKind.PLANNING_APPROVAL_REQUIRED),
        ("approval_required", "", [0], EscalationKind.PLANNING_APPROVAL_REQUIRED),
        # approval_required but still has blocking → treat as deadlock (rare)
        ("escalation_required", "approval_required", [3, 2, 1], EscalationKind.PLANNING_DEADLOCK),
        ("planner_environment_error:timeout", "", [], EscalationKind.PLANNING_ENV_FAIL),
        ("planner_environment_error:planner_output_truncated_empty", "", [], EscalationKind.PLANNING_ENV_FAIL),
        ("task_input_infeasible", "", [], EscalationKind.PLANNING_PREREQ_MISSING),
        ("approved", "", [0], None),  # success, not escalatable
    ],
)
def test_classify_planning(run_reason, root_cause, history, expected_kind):
    diagnostics = {
        "run_reason": run_reason,
        "root_cause": root_cause,
        "blocking_findings_history": history,
    }
    kind = classify(planning_diagnostics=diagnostics, phase="planning")
    assert kind == expected_kind, f"{run_reason!r}/{root_cause!r}/{history} -> {kind} != {expected_kind}"


def test_classify_gate_file_length():
    gate_check = {"items": [{"checker": "file_length", "violations": [{"path": "x.py"}]}]}
    assert classify(gate_check=gate_check, phase="gate") == EscalationKind.GATE_FILE_SPLIT_NEEDED


def test_classify_gate_complexity():
    gate_check = {"items": [{"checker": "function_metrics", "violations": [{"path": "x.py"}]}]}
    assert classify(gate_check=gate_check, phase="gate") == EscalationKind.GATE_REFACTOR_NEEDED


def test_classify_gate_compliance():
    gate_check = {"items": [{"checker": "compliance", "violations": [{"msg": "x"}]}]}
    assert classify(gate_check=gate_check, phase="gate") == EscalationKind.COMPLIANCE_BLOCK


# ---------------------------------------------------------------------------
# is_escalatable / allows_skip
# ---------------------------------------------------------------------------


def test_is_escalatable():
    assert is_escalatable(EscalationKind.EXECUTOR_STUCK) is True
    assert is_escalatable(EscalationKind.COMPLIANCE_BLOCK) is True
    assert is_escalatable(None) is False


def test_allows_skip():
    assert allows_skip(EscalationKind.EXECUTOR_STUCK) is True
    assert allows_skip(EscalationKind.COMPLIANCE_BLOCK) is False
    assert allows_skip(EscalationKind.EXECUTOR_PRECONDITION_MISSING) is False
    assert allows_skip(EscalationKind.PLANNING_DEADLOCK) is True


# ---------------------------------------------------------------------------
# maybe_escalate write/read round-trip + count enforcement
# ---------------------------------------------------------------------------


def test_maybe_escalate_writes_request():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        event = FailureEvent(
            phase="executor",
            error_code="EXECUTOR_STALLED_NO_WRITE_PROGRESS",
            evidence="executor stalled",
        )
        ok, kind = maybe_escalate(
            planning_dir=tmp,
            phase="executor",
            failure_event=event,
            feature="test_feature",
            task_id="T2",
            failure_summary="test summary",
        )
        assert ok is True
        assert kind == EscalationKind.EXECUTOR_STUCK
        req_path = tmp / ".executor_decision_request.json"
        assert req_path.exists()
        data = json.loads(req_path.read_text(encoding="utf-8"))
        assert data["escalation_kind"] == "EXECUTOR_STUCK"
        assert data["escalation_count"] == 1


def test_maybe_escalate_count_caps_at_max():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        event = FailureEvent(
            phase="executor",
            error_code="EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        )
        # 1st call → escalates (count=1)
        ok1, _ = maybe_escalate(planning_dir=tmp, phase="executor", failure_event=event)
        # 2nd call → escalates (count=2)
        ok2, _ = maybe_escalate(planning_dir=tmp, phase="executor", failure_event=event)
        # 3rd call → cap reached, no escalation
        ok3, kind3 = maybe_escalate(planning_dir=tmp, phase="executor", failure_event=event)
        assert ok1 is True
        assert ok2 is True
        assert ok3 is False
        assert kind3 == EscalationKind.EXECUTOR_STUCK  # kind detected but cap reached
        assert escalation_count(tmp, "executor") == 2


def test_maybe_escalate_non_escalatable_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        event = FailureEvent(phase="executor", error_code="UNKNOWN_CODE")
        ok, kind = maybe_escalate(planning_dir=tmp, phase="executor", failure_event=event)
        assert ok is False
        assert kind is None


def test_write_and_read_decision_response_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        resp = DecisionResponse(
            phase="planning",
            escalation_kind="PLANNING_DEADLOCK",
            action="accept",
            option_index=0,
            option={"title": "Split", "description": "..."},
        )
        write_decision_response(tmp, "planning", resp)
        loaded = read_decision_response(tmp, "planning")
        assert loaded is not None
        assert loaded.action == "accept"
        assert loaded.escalation_kind == "PLANNING_DEADLOCK"
        assert loaded.option == {"title": "Split", "description": "..."}


def test_legacy_compat_mirrors_gate_refactor_to_old_filename():
    """When phase=executor + GATE_REFACTOR_NEEDED, both new and legacy files exist."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        event = FailureEvent(
            phase="executor",
            error_code="GATE_BLOCKED",
            detector_hint="gate_complexity",
            evidence="complexity 11 > 10",
        )
        ok, _ = maybe_escalate(
            planning_dir=tmp,
            phase="executor",
            failure_event=event,
            feature="x",
            task_id="T2",
        )
        assert ok is True
        # New unified file
        assert (tmp / ".executor_decision_request.json").exists()
        # Legacy file also written for back-compat
        assert (tmp / ".executor_redesign_request.json").exists()


def test_find_pending_request_priority_order():
    """planning > executor > gate priority."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Write all three
        maybe_escalate(
            planning_dir=tmp, phase="executor",
            failure_event=FailureEvent(phase="executor", error_code="EXECUTOR_STALLED_NO_WRITE_PROGRESS"),
        )
        maybe_escalate(
            planning_dir=tmp, phase="planning",
            planning_diagnostics={"run_reason": "stubborn_round_limit"},
        )
        maybe_escalate(
            planning_dir=tmp, phase="gate",
            gate_check={"items": [{"checker": "function_metrics", "violations": [{}]}]},
        )
        pending = find_pending_request(tmp)
        assert pending is not None
        phase, req = pending
        assert phase == "planning"  # priority winner
        assert req.escalation_kind == "PLANNING_DEADLOCK"

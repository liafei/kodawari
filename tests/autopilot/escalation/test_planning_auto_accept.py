"""Tests for try_auto_accept_planning_approval.

GPT v6 plan (two sub-agents both approved). Covers:
  - happy path: clean [3,1,0] → response written + request consumed + audit fields
  - find_pending_request / detect_pending_resume do not surface the planning gate after
  - selected plan is taken from the LAST clean round in conversation.rounds
  - all 6 no-op cases listed in the spec
  - legacy executor redesign requests are not affected
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from kodawari.autopilot.escalation.handler import find_pending_request
from kodawari.autopilot.escalation.planning_auto_accept import (
    AutoAcceptResult,
    try_auto_accept_planning_approval,
)
from kodawari.autopilot.escalation.resume import detect_pending_resume
from kodawari.autopilot.planning.planning_orchestrator import STRUCTURAL_CHECK_NAMES


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _clean_checks(**overrides) -> dict:
    """All 17 structural checks PASS + score 9 / score_gap_ok."""
    base = {name: True for name in STRUCTURAL_CHECK_NAMES}
    base["planner_score"] = 9.0
    base["reviewer_score"] = 9.0
    base["score_gap_ok"] = True
    base.update(overrides)
    return base


def _clean_task(**overrides) -> dict:
    t = {
        "task_id": "T1",
        "task_name": "Implement clean unit",
        "files_to_change": ["backend/foo.py"],
        "invariants": ["public signature stable"],
        "forbidden_changes": [],
        "path_type": "write",
    }
    t.update(overrides)
    return t


def _clean_conversation(*, history=(3, 1, 0), tasks=None, decision="auto_approve") -> dict:
    if tasks is None:
        tasks = [_clean_task()]
    return {
        "status": "escalation_required",
        "escalation": {
            "gate_reason": "approval_required",
            "round_count": len(history),
            "blocking_findings_history": list(history),
        },
        "approval": {
            "decision": decision,
            "reason": "all_structural_checks_passed",
            "checks": _clean_checks(),
        },
        "rounds": [
            {"round_number": i + 1, "blocking_findings_count": n}
            for i, n in enumerate(history[:-1])
        ] + [
            {
                "round_number": len(history),
                "blocking_findings_count": 0,
                "plan_payload": {"tasks": list(tasks)},
            },
        ],
        "final_plan": {"tasks": list(tasks)},
    }


def _seed_request(planning_dir: Path) -> None:
    """Drop a .planning_decision_request.json so we can verify it's removed."""
    (planning_dir / ".planning_decision_request.json").write_text(
        json.dumps({
            "schema_version": "workflow.decision_request.v1",
            "escalation_kind": "PLANNING_APPROVAL_REQUIRED",
            "phase": "planning",
            "failure_code": "escalation_required",
            "context": {"root_cause": "approval_required"},
            "escalation_count": 1,
            "max_escalations": 2,
            "issued_at": "2026-05-16T00:00:00+00:00",
        }),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_3_1_0_auto_accepts_and_consumes_request() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        _seed_request(pdir)
        payload = _clean_conversation(history=(3, 1, 0))

        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=payload,
        )

        assert result.applied is True
        assert result.reason == "auto_accepted"
        assert result.task_count == 1
        assert result.selected_round_number == 3

        # Response written with audit
        resp_path = pdir / ".planning_decision_response.json"
        assert resp_path.exists()
        resp = json.loads(resp_path.read_text(encoding="utf-8"))
        assert resp["phase"] == "planning"
        assert resp["action"] == "accept"
        assert resp["escalation_kind"] == "PLANNING_APPROVAL_REQUIRED"
        assert resp["applied_inline_via"] == "model_bootstrap"
        assert "applied_at" in resp
        assert resp["auto_accept_audit"]["selected_round_number"] == 3

        # Request consumed
        assert not (pdir / ".planning_decision_request.json").exists()


def test_find_pending_request_does_not_surface_after_accept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        _seed_request(pdir)
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=_clean_conversation(),
        )
        assert result.applied is True
        assert find_pending_request(pdir) is None


def test_detect_pending_resume_does_not_surface_after_accept() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        _seed_request(pdir)
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=_clean_conversation(),
        )
        assert result.applied is True
        # detect_pending_resume should NOT pick this up because the response
        # has been tagged with applied_at.
        assert detect_pending_resume(pdir) is None


def test_selected_plan_is_last_clean_round() -> None:
    """When multiple rounds have blocking=0, take the LAST one (latest)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        payload = {
            "status": "escalation_required",
            "escalation": {
                "gate_reason": "approval_required",
                "round_count": 3,
                "blocking_findings_history": [3, 0, 0],
            },
            "approval": {"decision": "auto_approve", "checks": _clean_checks()},
            "rounds": [
                {"round_number": 1, "blocking_findings_count": 3, "plan_payload": {"tasks": [_clean_task(task_id="OLD_TASK")]}},
                {"round_number": 2, "blocking_findings_count": 0, "plan_payload": {"tasks": [_clean_task(task_id="MID_TASK")]}},
                {"round_number": 3, "blocking_findings_count": 0, "plan_payload": {"tasks": [_clean_task(task_id="LATEST_TASK")]}},
            ],
            "final_plan": {"tasks": [_clean_task(task_id="LATEST_TASK")]},
        }
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=payload,
        )
        assert result.applied is True
        assert result.selected_round_number == 3
        assert result.selected_plan["tasks"][0]["task_id"] == "LATEST_TASK"


# ---------------------------------------------------------------------------
# Safety predicate no-ops
# ---------------------------------------------------------------------------


def _expect_noop(payload: dict, expected_reason_prefix: str) -> AutoAcceptResult:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        _seed_request(pdir)
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=payload,
        )
        assert result.applied is False, f"unexpected accept; reason={result.reason}"
        assert result.reason.startswith(expected_reason_prefix), result.reason
        # Request must remain — manual approval path will pick it up.
        assert (pdir / ".planning_decision_request.json").exists()
        # No response file should be written.
        assert not (pdir / ".planning_decision_response.json").exists()
    return result


def test_noop_low_planner_score() -> None:
    payload = _clean_conversation()
    payload["approval"]["checks"]["planner_score"] = 7.0
    _expect_noop(payload, "planner_score_below_threshold")


def test_noop_low_reviewer_score() -> None:
    payload = _clean_conversation()
    payload["approval"]["checks"]["reviewer_score"] = 7.0
    _expect_noop(payload, "reviewer_score_below_threshold")


def test_noop_score_gap_not_ok() -> None:
    payload = _clean_conversation()
    payload["approval"]["checks"]["score_gap_ok"] = False
    _expect_noop(payload, "score_gap_too_large")


def test_noop_structural_check_fails() -> None:
    payload = _clean_conversation()
    payload["approval"]["checks"]["dependency_graph_acyclic"] = False
    result = _expect_noop(payload, "structural_checks_failed")
    assert "dependency_graph_acyclic" in result.audit.get("failing_checks", [])


def test_noop_history_bounces() -> None:
    payload = _clean_conversation(history=(3, 1, 2, 0))  # 1→2 is an increase
    _expect_noop(payload, "history_not_monotonic_non_increasing")


def test_noop_history_last_not_zero() -> None:
    payload = _clean_conversation(history=(3, 2, 1))
    _expect_noop(payload, "history_last_not_zero")


def test_noop_round_count_above_threshold() -> None:
    payload = _clean_conversation(history=(5, 4, 3, 2, 1, 0))
    payload["escalation"]["round_count"] = 6
    _expect_noop(payload, "round_count_above_threshold")


def test_noop_empty_tasks() -> None:
    payload = _clean_conversation()
    payload["rounds"][-1]["plan_payload"]["tasks"] = []
    payload["final_plan"]["tasks"] = []
    # _select_clean_plan returns None when last round has no tasks AND
    # final_plan has no tasks.
    _expect_noop(payload, "no_clean_plan_in_rounds")


def test_noop_decision_not_auto_approve() -> None:
    payload = _clean_conversation(decision="human_required")
    _expect_noop(payload, "approval_decision_not_auto_approve")


def test_noop_files_to_change_above_threshold() -> None:
    payload = _clean_conversation(tasks=[
        _clean_task(files_to_change=["a.py", "b.py", "c.py", "d.py"]),  # 4 > 3
    ])
    result = _expect_noop(payload, "files_to_change_above_threshold")
    assert result.audit.get("files_count") == 4


def test_noop_write_task_missing_invariants() -> None:
    payload = _clean_conversation(tasks=[_clean_task(invariants=[])])
    _expect_noop(payload, "write_task_missing_invariants")


def test_noop_forbidden_changes_wrong_type() -> None:
    payload = _clean_conversation(tasks=[_clean_task(forbidden_changes="not_a_list")])
    _expect_noop(payload, "forbidden_changes_not_list")


def test_noop_status_not_escalation_required() -> None:
    payload = _clean_conversation()
    payload["status"] = "approved"
    _expect_noop(payload, "status_not_escalation_required")


def test_noop_gate_reason_not_approval_required() -> None:
    payload = _clean_conversation()
    payload["escalation"]["gate_reason"] = "stubborn_round_limit"
    _expect_noop(payload, "gate_reason_not_approval_required")


# ---------------------------------------------------------------------------
# Splitter-skip / files-cap interaction
# ---------------------------------------------------------------------------


def test_files_to_change_exactly_3_allowed() -> None:
    """3 files is the boundary — still accept."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        payload = _clean_conversation(tasks=[
            _clean_task(files_to_change=["a.py", "b.py", "c.py"]),
        ])
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=payload,
        )
        assert result.applied is True


def test_read_only_task_can_omit_invariants() -> None:
    """A read-only / planning-meta task (path_type=read) may have no
    invariants since it doesn't change code."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        payload = _clean_conversation(tasks=[
            _clean_task(invariants=[], path_type="read"),
        ])
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=payload,
        )
        assert result.applied is True


# ---------------------------------------------------------------------------
# Off-path safety
# ---------------------------------------------------------------------------


def test_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_PLANNING_AUTO_ACCEPT", "0")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        _seed_request(pdir)
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=_clean_conversation(),
        )
        assert result.applied is False
        assert result.reason == "disabled_via_env"
        # request still pending
        assert (pdir / ".planning_decision_request.json").exists()


def test_legacy_executor_redesign_request_not_touched() -> None:
    """The helper writes ONLY .planning_decision_response.json. Any legacy
    .executor_redesign_request.json sitting in the same dir is unaffected."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        legacy = pdir / ".executor_redesign_request.json"
        legacy.write_text('{"task_id": "T9", "failure_summary": "executor"}', encoding="utf-8")
        try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=_clean_conversation(),
        )
        # Legacy executor file should still exist.
        assert legacy.exists()
        # Helper should have written planning response only.
        assert (pdir / ".planning_decision_response.json").exists()


def test_invalid_arguments_returns_noop() -> None:
    result = try_auto_accept_planning_approval(
        project_root="not_a_path",  # type: ignore[arg-type]
        planning_dir="not_a_path",  # type: ignore[arg-type]
        conversation_payload={},
    )
    assert result.applied is False
    assert result.reason == "invalid_arguments"


def test_missing_conversation_payload_returns_noop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        # No PLANNING_CONVERSATION.json on disk and no payload arg
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=None,
        )
        assert result.applied is False
        assert result.reason == "conversation_payload_missing"


def test_loads_conversation_from_disk_when_no_payload_arg() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdir = root / "planning" / "demo"
        pdir.mkdir(parents=True)
        (pdir / "PLANNING_CONVERSATION.json").write_text(
            json.dumps(_clean_conversation()),
            encoding="utf-8",
        )
        result = try_auto_accept_planning_approval(
            project_root=root,
            planning_dir=pdir,
            conversation_payload=None,
        )
        assert result.applied is True


# ---------------------------------------------------------------------------
# A3: relaxed-score auto-approve path
# ---------------------------------------------------------------------------


class TestA3RelaxedScorePath:
    """Approval.reason endswith '_relaxed_score' lowers the score floor to 7.5.

    Guardrail: the relaxed path REQUIRES all structural checks + score_gap_ok +
    no_blocking_findings + reviewer_approved_effective. So 7.5 is safe — it can
    only fire when the plan is otherwise clean.
    """

    def test_relaxed_reason_accepts_75_planner_75_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "planning" / "demo"
            pdir.mkdir(parents=True)
            _seed_request(pdir)
            payload = _clean_conversation()
            payload["approval"]["reason"] = "all_structural_checks_passed_relaxed_score"
            payload["approval"]["checks"]["planner_score"] = 7.5
            payload["approval"]["checks"]["reviewer_score"] = 7.5

            result = try_auto_accept_planning_approval(
                project_root=root,
                planning_dir=pdir,
                conversation_payload=payload,
            )
            assert result.applied is True
            assert result.reason == "auto_accepted"

    def test_relaxed_reason_rejects_below_75(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "planning" / "demo"
            pdir.mkdir(parents=True)
            _seed_request(pdir)
            payload = _clean_conversation()
            payload["approval"]["reason"] = "all_structural_checks_passed_relaxed_score"
            payload["approval"]["checks"]["planner_score"] = 7.4
            payload["approval"]["checks"]["reviewer_score"] = 7.5

            result = try_auto_accept_planning_approval(
                project_root=root,
                planning_dir=pdir,
                conversation_payload=payload,
            )
            assert result.applied is False
            assert result.reason == "planner_score_below_threshold"
            assert result.audit.get("relaxed") is True
            assert result.audit.get("threshold") == 7.5

    def test_strict_reason_rejects_75_score(self) -> None:
        """Without the relaxed suffix, the strict 8.0 floor still applies."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "planning" / "demo"
            pdir.mkdir(parents=True)
            _seed_request(pdir)
            payload = _clean_conversation()
            # strict reason — default already strict but make it explicit
            payload["approval"]["reason"] = "all_structural_checks_passed"
            payload["approval"]["checks"]["planner_score"] = 7.9
            payload["approval"]["checks"]["reviewer_score"] = 9.0

            result = try_auto_accept_planning_approval(
                project_root=root,
                planning_dir=pdir,
                conversation_payload=payload,
            )
            assert result.applied is False
            assert result.reason == "planner_score_below_threshold"
            assert result.audit.get("relaxed") is False
            assert result.audit.get("threshold") == 8.0

    def test_relaxed_path_still_enforces_structural_checks(self) -> None:
        """Relaxed score does NOT lift the 17 structural checks guard."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "planning" / "demo"
            pdir.mkdir(parents=True)
            _seed_request(pdir)
            payload = _clean_conversation()
            payload["approval"]["reason"] = "all_structural_checks_passed_relaxed_score"
            payload["approval"]["checks"]["planner_score"] = 7.5
            payload["approval"]["checks"]["reviewer_score"] = 7.5
            # Break one structural check.
            payload["approval"]["checks"][STRUCTURAL_CHECK_NAMES[0]] = False

            result = try_auto_accept_planning_approval(
                project_root=root,
                planning_dir=pdir,
                conversation_payload=payload,
            )
            assert result.applied is False
            assert result.reason == "structural_checks_failed"

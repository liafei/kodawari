from __future__ import annotations

import json
from pathlib import Path

from kodawari.autopilot.core.delivery_report import generate_delivery_report
from kodawari.cli.evidence.artifact_truth import (
    RUN_TRUTH_SCHEMA_VERSION,
    build_run_truth,
    load_run_truth,
    write_run_truth,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_build_run_truth_splits_runtime_rounds_from_effective_rounds(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    planning = project / "planning" / "feature"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_t107.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    _write_json(planning / "PLANNING_CONVERSATION.json", {"rounds": [{"round_number": 1}]})
    _write_json(planning / ".execution_result.json", {"changed_files": ["tests/test_t107.py"]})
    rounds = [
        {"stage": "IMPLEMENT"},
        {"stage": "VERIFY"},
        {"stage": "RULES_GATE"},
        {"stage": "PEER_REVIEW", "review_round": 1, "details": {"must_fix": ["a", "b"]}},
        {"stage": "IMPLEMENT"},
        {"stage": "VERIFY"},
        {"stage": "RULES_GATE"},
        {"stage": "PEER_REVIEW", "review_round": 2, "details": {"must_fix": []}},
        {"stage": "PROCEED_TO_GATE"},
    ]
    run_result = {
        "reason": "PROCEED_TO_GATE",
        "peer_review_summary": {"approved": True, "review_round": 2},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS"},
        "recovery_decisions": [{"role": "deterministic_recovery"}],
    }

    truth = build_run_truth(
        project_root=project,
        planning_dir=planning,
        feature="feature",
        payload={},
        run_result=run_result,
        rounds=rounds,
        state_payload={},
        reliable_changed_files=("tests/test_t107.py",),
        changed_files_source="execution_result_changed_files:existing",
    )

    assert truth["schema_version"] == RUN_TRUTH_SCHEMA_VERSION
    assert truth["planning_rounds"] == 1
    assert truth["runtime_rounds"] == 9
    assert truth["executor_attempts"] == 2
    assert truth["review_rounds"] == 2
    assert truth["review_must_fix_max"] == 2
    assert truth["deterministic_recovery_hits"] == 1
    assert truth["verify_status"] == "PASS"
    assert truth["gate_status"] == "PASS"


def test_build_run_truth_counts_planning_failure_rounds_without_conversation(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    planning = project / "planning" / "feature"
    _write_json(
        planning / ".planning_failure.json",
        {
            "round_count": 3,
            "rounds": [
                {"round_number": 1},
                {"round_number": 2},
                {"round_number": 3},
            ],
        },
    )

    truth = build_run_truth(
        project_root=project,
        planning_dir=planning,
        feature="feature",
        payload={"unified_status": {"final_status": "BLOCKED"}},
        run_result={"reason": "planner_reviewer_deadlock", "blocking_reason": "planner_reviewer_deadlock"},
        rounds=[],
        state_payload={},
    )

    assert truth["planning_rounds"] == 3
    assert truth["run_reason"] == "planner_reviewer_deadlock"


def test_write_load_run_truth_and_delivery_report_use_truth_only(tmp_path: Path) -> None:
    planning = tmp_path / "planning"
    truth = {
        "schema_version": RUN_TRUTH_SCHEMA_VERSION,
        "feature": "feature",
        "final_status": "ready_for_gate",
        "run_reason": "PROCEED_TO_GATE",
        "blocking_reason": "",
        "planning_rounds": 1,
        "runtime_rounds": 9,
        "executor_attempts": 2,
        "review_rounds": 2,
        "review_must_fix_max": 2,
        "recovery_pressure": 0,
        "deterministic_recovery_hits": 0,
        "synthesizer_calls": 0,
        "verify_status": "PASS",
        "gate_status": "PASS",
        "review_approved": True,
        "changed_files": ["tests/test_t107.py"],
        "changed_files_source": "execution_result_changed_files:existing",
        "truth_sources": {"review": ".review_evidence.json", "verify": "none"},
        "stale_artifacts": {"review_result": [], "verify_report": ["verify_changed_files_mismatch"]},
    }

    write_run_truth(planning, truth)

    assert load_run_truth(planning)["runtime_rounds"] == 9
    report = generate_delivery_report(planning_dir=planning, feature="feature")
    assert "review_rounds: 2" in report
    assert "verify_status: PASS" in report
    assert "gate_status: PASS" in report
    assert "stale_verify_report: ['verify_changed_files_mismatch']" in report


def test_unexecuted_task_ids_reports_skipped_tasks_when_task_cycle_off(tmp_path: Path) -> None:
    """TASK_GRAPH has T1+T2, only T1 completed -> T2 appears in unexecuted_task_ids."""
    project = tmp_path / "repo"
    planning = project / "planning" / "feature"
    _write_json(
        planning / "TASK_GRAPH.json",
        {
            "tasks": [
                {"task_id": "t1_migration", "task_name": "Add migration"},
                {"task_id": "t2_helper", "task_name": "Add helper"},
            ]
        },
    )
    truth = build_run_truth(
        project_root=project,
        planning_dir=planning,
        feature="feature",
        payload={},
        run_result={"reason": "PROCEED_TO_GATE"},
        rounds=[],
        state_payload={"completed_tasks": ["T1_MIGRATION: Add migration"]},
        reliable_changed_files=(),
        changed_files_source="",
    )
    assert truth["unexecuted_task_ids"] == ["T2_HELPER"]


def test_unexecuted_task_ids_empty_when_all_completed(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    planning = project / "planning" / "feature"
    _write_json(
        planning / "TASK_GRAPH.json",
        {"tasks": [{"task_id": "t1", "task_name": "only"}]},
    )
    truth = build_run_truth(
        project_root=project,
        planning_dir=planning,
        feature="feature",
        payload={},
        run_result={"reason": "PROCEED_TO_GATE"},
        rounds=[],
        state_payload={"completed_tasks": ["T1: only"]},
        reliable_changed_files=(),
        changed_files_source="",
    )
    assert truth["unexecuted_task_ids"] == []


def test_executor_attempts_counts_fix_round_and_codex_fix(tmp_path: Path) -> None:
    """executor_attempts must include FIX_ROUND and CODEX_FIX retries, not just IMPLEMENT.

    Pre-fix this run reported executor_attempts=1 even though the executor ran 4 times.
    """
    project = tmp_path / "repo"
    planning = project / "planning" / "feature"
    rounds = [
        {"stage": "IMPLEMENT"},
        {"stage": "PEER_REVIEW", "review_round": 1, "details": {"must_fix": ["x"]}},
        {"stage": "FIX_ROUND"},
        {"stage": "FIX_ROUND"},
        {"stage": "CODEX_FIX"},
        {"stage": "VERIFY"},
        {"stage": "PEER_REVIEW", "review_round": 2, "details": {"must_fix": []}},
    ]
    # run_result deliberately omits executor_attempts so we exercise the fallback in artifact_truth
    truth = build_run_truth(
        project_root=project,
        planning_dir=planning,
        feature="feature",
        payload={},
        run_result={"reason": "PROCEED_TO_GATE"},
        rounds=rounds,
        state_payload={},
        reliable_changed_files=(),
        changed_files_source="",
    )
    # 1 IMPLEMENT + 2 FIX_ROUND + 1 CODEX_FIX = 4
    assert truth["executor_attempts"] == 4
    assert truth["execution_rounds"] == 4


def test_unexecuted_task_ids_empty_when_task_graph_absent(tmp_path: Path) -> None:
    """No TASK_GRAPH -> field is [] (not present-but-everything-pending)."""
    project = tmp_path / "repo"
    planning = project / "planning" / "feature"
    planning.mkdir(parents=True)
    truth = build_run_truth(
        project_root=project,
        planning_dir=planning,
        feature="feature",
        payload={},
        run_result={"reason": "PROCEED_TO_GATE"},
        rounds=[],
        state_payload={"completed_tasks": []},
        reliable_changed_files=(),
        changed_files_source="",
    )
    assert truth["unexecuted_task_ids"] == []

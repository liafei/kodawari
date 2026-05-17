from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.contract.next_task_selector import select_next_task


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _graph() -> dict:
    return {
        "schema_version": "contract_first.task_graph.v1",
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "first",
                "depends_on": [],
                "core_files": ["src/a.py"],
                "layer_owner": "service",
                "invariants": ["keep a"],
                "test_proof": "pytest",
                "executability": {"status": "PASS", "issues": []},
            },
            {
                "task_id": "T2",
                "task_name": "second",
                "depends_on": ["T1"],
                "core_files": ["src/b.py"],
                "layer_owner": "service",
                "invariants": ["keep b"],
                "test_proof": "pytest",
                "executability": {"status": "PASS", "issues": []},
            },
        ],
        "executability": {"status": "PASS", "issues": []},
    }


def test_select_next_task_respects_completed_dependencies(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feature"
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "completed_tasks": ["T1: first"],
        },
    )

    selection = select_next_task(_graph(), planning_dir=planning_dir)

    assert selection.action == "take_task"
    assert selection.task_id == "T2"
    assert selection.completed_task_ids == frozenset({"T1"})


def test_select_next_task_skips_unsatisfied_dependencies(tmp_path: Path) -> None:
    selection = select_next_task(_graph(), planning_dir=tmp_path / "planning" / "feature")

    assert selection.action == "take_task"
    assert selection.task_id == "T1"
    assert not selection.skipped_tasks


def test_select_next_task_routes_latest_review_block_to_recovery(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feature"
    _write_json(
        planning_dir / ".task_run_result.json",
        {
            "status": "FAIL",
            "task_id": "T1",
            "reason": "OPUS_REVIEW_BLOCKED",
        },
    )

    selection = select_next_task(_graph(), planning_dir=planning_dir)

    assert selection.action == "review_fix_required"
    assert selection.task_id == "T1"
    assert selection.stage_profile == "recovery"


def _failure_chain_graph() -> dict:
    """T1 FAIL -> T2 depends_on T1 -> T3 depends_on T2."""
    return {
        "schema_version": "contract_first.task_graph.v1",
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "first",
                "depends_on": [],
                "core_files": ["src/a.py"],
                "layer_owner": "service",
                "invariants": ["keep a"],
                "test_proof": "pytest",
                "executability": {"status": "FAIL", "issues": ["core file does not exist"]},
            },
            {
                "task_id": "T2",
                "task_name": "second",
                "depends_on": ["T1"],
                "core_files": ["src/b.py"],
                "layer_owner": "service",
                "invariants": ["keep b"],
                "test_proof": "pytest",
                "executability": {"status": "PASS", "issues": []},
            },
            {
                "task_id": "T3",
                "task_name": "third",
                "depends_on": ["T2"],
                "core_files": ["src/c.py"],
                "layer_owner": "service",
                "invariants": ["keep c"],
                "test_proof": "pytest",
                "executability": {"status": "PASS", "issues": []},
            },
        ],
        "executability": {"status": "PASS", "issues": []},
    }


def test_skipped_task_records_failed_ancestor_in_blocked_by(tmp_path: Path) -> None:
    """B1: when T1 FAILs, T3 (which depends on T2 which depends on T1) reports
    blocked_by=[T1], not just blocked_by=[T2]. The user sees the actual root
    cause without having to trace the chain manually."""
    planning_dir = tmp_path / "planning" / "chain"
    planning_dir.mkdir(parents=True, exist_ok=True)

    selection = select_next_task(_failure_chain_graph(), planning_dir=planning_dir)
    skipped_by_id = {item.task_id: item for item in selection.skipped_tasks}

    assert "T1" in skipped_by_id, "T1 itself should be in skipped (executability_failed)"
    assert skipped_by_id["T1"].reason == "executability_failed"
    assert list(skipped_by_id["T1"].blocked_by) == ["T1"]

    assert "T2" in skipped_by_id, "T2 should be skipped (depends on failed T1)"
    assert skipped_by_id["T2"].reason == "dependencies_unsatisfied"
    assert list(skipped_by_id["T2"].blocked_by) == ["T1"], (
        "T2's blocked_by must trace to the failed ancestor T1, not just the "
        "immediate missing dep — that's the whole point of the closure trace"
    )

    assert "T3" in skipped_by_id, "T3 should be skipped (transitive dep on T1)"
    assert skipped_by_id["T3"].reason == "dependencies_unsatisfied"
    assert list(skipped_by_id["T3"].blocked_by) == ["T1"], (
        "T3 must trace through T2 to the actual failed root cause T1"
    )


def test_skipped_task_serializes_blocked_by_field(tmp_path: Path) -> None:
    """B1: to_dict surfaces blocked_by so downstream artifacts (CYCLE summary,
    status output) can read it without import-time coupling to SkippedTask."""
    planning_dir = tmp_path / "planning" / "ser"
    planning_dir.mkdir(parents=True, exist_ok=True)

    selection = select_next_task(_failure_chain_graph(), planning_dir=planning_dir)
    payload = selection.to_dict()

    skipped_entries = {item["task_id"]: item for item in payload["skipped_tasks"]}
    assert "blocked_by" in skipped_entries["T3"]
    assert skipped_entries["T3"]["blocked_by"] == ["T1"]


def test_select_next_task_reports_all_complete(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feature"
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "completed_tasks": ["T1: first", "T2: second"],
        },
    )

    selection = select_next_task(_graph(), planning_dir=planning_dir)

    assert selection.action == "all_tasks_complete"
    assert not selection.selected

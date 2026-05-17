from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.parallel_coordinator import (
    PARALLEL_COORDINATOR_VERSION,
    PARALLEL_RUNTIME_VERSION,
    build_parallel_coordinator_plan,
    build_parallel_runtime_snapshot,
)
from kodawari.autopilot.state import AutopilotState, SubtaskCheckpoint, SubtaskStatus


def test_parallel_coordinator_plan_builds_worker_assignments(tmp_path: Path) -> None:
    state = AutopilotState(feature="demo", project_root=tmp_path, active_task="T100: parallel")
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T100.1",
            title="Implement API",
            parent_task_id="T100",
            status=SubtaskStatus.PENDING,
            changed_files=["src/api.py"],
        )
    )
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T100.2",
            title="Write tests",
            parent_task_id="T100",
            status=SubtaskStatus.PENDING,
            changed_files=["tests/test_api.py"],
        )
    )

    payload = build_parallel_coordinator_plan(
        state=state,
        feature="demo",
        planning_dir=tmp_path / "planning" / "demo",
        max_workers=2,
    )

    assert payload["schema_version"] == PARALLEL_COORDINATOR_VERSION
    assert len(payload["workers"]) == 2
    assert len(payload["assignments"]) == 2
    assert payload["assignments"][0]["worker_id"] == "worker-1"
    assert payload["assignments"][1]["worker_id"] == "worker-2"


def test_parallel_runtime_snapshot_reports_worker_merge_status(tmp_path: Path) -> None:
    state = AutopilotState(feature="demo", project_root=tmp_path, active_task="T200: runtime")
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T200.1",
            title="Task one",
            parent_task_id="T200",
            status=SubtaskStatus.DONE,
            changed_files=["src/a.py"],
        )
    )
    state.add_subtask(
        SubtaskCheckpoint(
            subtask_id="T200.2",
            title="Task two",
            parent_task_id="T200",
            status=SubtaskStatus.FAILED,
            changed_files=["src/b.py"],
        )
    )
    plan = build_parallel_coordinator_plan(
        state=state,
        feature="demo",
        planning_dir=tmp_path / "planning" / "demo",
        max_workers=2,
    )
    worktree_snapshot = {
        "schema_version": "parallel.worktree_manager.v1",
        "mode": "directory_isolation",
        "allocations": [
            {"worker_id": "worker-1", "worktree_id": "wt-worker-1", "mode": "directory_isolation", "path": str(tmp_path / "w1")},
            {"worker_id": "worker-2", "worktree_id": "wt-worker-2", "mode": "directory_isolation", "path": str(tmp_path / "w2")},
        ],
    }

    runtime = build_parallel_runtime_snapshot(
        plan=plan,
        state=state,
        worktree_snapshot=worktree_snapshot,
    )

    assert runtime["schema_version"] == PARALLEL_RUNTIME_VERSION
    assert runtime["merge_status"] == "BLOCKED"
    assert len(runtime["worker_statuses"]) == 2
    assert runtime["totals"]["failed"] == 1

from __future__ import annotations

from pathlib import Path

from kodawari.autopilot.worktree_manager import (
    WORKTREE_MANAGER_VERSION,
    allocate_worker_worktrees,
    inject_worktree_paths,
)


def test_allocate_worker_worktrees_creates_isolated_directories(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "demo"
    workers = [{"worker_id": "worker-1"}, {"worker_id": "worker-2"}]

    snapshot = allocate_worker_worktrees(
        planning_dir=planning_dir,
        workers=workers,
        mode="directory_isolation",
    )

    assert snapshot["schema_version"] == WORKTREE_MANAGER_VERSION
    assert snapshot["mode"] == "directory_isolation"
    allocations = list(snapshot["allocations"])
    assert len(allocations) == 2
    for row in allocations:
        assert Path(row["path"]).exists()


def test_inject_worktree_paths_enriches_assignments(tmp_path: Path) -> None:
    assignments = [
        {"subtask_id": "T1.1", "worker_id": "worker-1", "role": "implementer"},
        {"subtask_id": "T1.2", "worker_id": "worker-2", "role": "reviewer"},
    ]
    worktree_snapshot = {
        "schema_version": WORKTREE_MANAGER_VERSION,
        "mode": "directory_isolation",
        "allocations": [
            {
                "worker_id": "worker-1",
                "worktree_id": "wt-worker-1",
                "mode": "directory_isolation",
                "path": str(tmp_path / "w1"),
            },
            {
                "worker_id": "worker-2",
                "worktree_id": "wt-worker-2",
                "mode": "directory_isolation",
                "path": str(tmp_path / "w2"),
            },
        ],
    }

    enriched = inject_worktree_paths(
        assignments=assignments,
        worktree_snapshot=worktree_snapshot,
    )

    assert enriched[0]["worktree_id"] == "wt-worker-1"
    assert enriched[1]["worktree_id"] == "wt-worker-2"
    assert enriched[0]["worktree_path"].endswith("w1")

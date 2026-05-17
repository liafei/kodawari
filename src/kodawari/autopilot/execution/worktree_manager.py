"""Worktree/directory isolation helpers for parallel workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKTREE_MANAGER_VERSION = "parallel.worktree_manager.v1"


@dataclass(frozen=True)
class WorktreeAllocation:
    worker_id: str
    worktree_id: str
    mode: str
    path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "worktree_id": self.worktree_id,
            "mode": self.mode,
            "path": self.path,
        }


def allocate_worker_worktrees(
    *,
    planning_dir: Path,
    workers: list[dict[str, Any]],
    mode: str = "directory_isolation",
) -> dict[str, Any]:
    base_dir = (planning_dir / ".parallel_workers").resolve()
    allocations: list[WorktreeAllocation] = []
    if mode != "directory_isolation":
        return {
            "schema_version": WORKTREE_MANAGER_VERSION,
            "mode": "disabled",
            "base_dir": str(base_dir),
            "allocations": [],
        }

    base_dir.mkdir(parents=True, exist_ok=True)
    for index, worker in enumerate(workers, start=1):
        worker_id = str(worker.get("worker_id") or f"worker-{index}").strip()
        if not worker_id:
            worker_id = f"worker-{index}"
        worktree_id = f"wt-{worker_id}"
        path = (base_dir / worker_id).resolve()
        path.mkdir(parents=True, exist_ok=True)
        allocations.append(
            WorktreeAllocation(
                worker_id=worker_id,
                worktree_id=worktree_id,
                mode="directory_isolation",
                path=str(path),
            )
        )
    return {
        "schema_version": WORKTREE_MANAGER_VERSION,
        "mode": "directory_isolation",
        "base_dir": str(base_dir),
        "allocations": [item.to_dict() for item in allocations],
    }


def inject_worktree_paths(
    *,
    assignments: list[dict[str, Any]],
    worktree_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    allocation_map: dict[str, dict[str, Any]] = {}
    for row in list(worktree_snapshot.get("allocations") or []):
        if not isinstance(row, dict):
            continue
        worker_id = str(row.get("worker_id") or "").strip()
        if worker_id:
            allocation_map[worker_id] = row
    enriched: list[dict[str, Any]] = []
    for assignment in assignments:
        row = dict(assignment)
        worker_id = str(row.get("worker_id") or "").strip()
        alloc = allocation_map.get(worker_id, {})
        row["worktree_id"] = str(alloc.get("worktree_id") or row.get("worktree_id") or "")
        row["worktree_path"] = str(alloc.get("path") or row.get("worktree_path") or "")
        row["worktree_mode"] = str(alloc.get("mode") or row.get("worktree_mode") or "")
        enriched.append(row)
    return enriched


__all__ = [
    "WORKTREE_MANAGER_VERSION",
    "WorktreeAllocation",
    "allocate_worker_worktrees",
    "inject_worktree_paths",
]

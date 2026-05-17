"""Minimal parallel coordinator contract for worker assignment runtime."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from kodawari.autopilot.execution.merge_semantics import (
    MERGE_SEMANTICS_VERSION,
    normalize_worker_role,
    ownership_files_from_subtask,
    summarize_worker_statuses,
)
from kodawari.autopilot.execution.worktree_manager import inject_worktree_paths


PARALLEL_COORDINATOR_VERSION = "parallel.coordinator.v1"
PARALLEL_RUNTIME_VERSION = "parallel.runtime.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _state_subtasks(state: Any) -> list[Any]:
    raw = getattr(state, "subtasks", {}) if state is not None else {}
    if isinstance(raw, dict):
        return list(raw.values())
    return []


def _subtask_id(subtask: Any, *, index: int) -> str:
    return str(getattr(subtask, "subtask_id", "") or f"TASK-{index:03d}").strip()


def _subtask_status(subtask: Any) -> str:
    raw = getattr(subtask, "status", None)
    if hasattr(raw, "value"):
        raw = raw.value
    text = str(raw or "").strip().upper()
    if text in {"DONE", "FAILED", "RUNNING", "PENDING"}:
        return text
    return "PENDING"


def _worker_specs(max_workers: int) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for index in range(max_workers):
        worker_id = f"worker-{index + 1}"
        specs.append(
            {
                "worker_id": worker_id,
                "role": normalize_worker_role("", worker_index=index),
            }
        )
    return specs


def build_parallel_coordinator_plan(
    *,
    state: Any,
    feature: str,
    planning_dir: Any,
    max_workers: int = 2,
    session_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    resolved_workers = _worker_specs(_safe_int(max_workers, 2))
    assignments: list[dict[str, Any]] = []
    subtasks = _state_subtasks(state)
    for index, subtask in enumerate(subtasks):
        worker = resolved_workers[index % len(resolved_workers)]
        assignments.append(
            {
                "subtask_id": _subtask_id(subtask, index=index + 1),
                "status": _subtask_status(subtask),
                "worker_id": str(worker["worker_id"]),
                "role": str(worker["role"]),
                "ownership_files": ownership_files_from_subtask(subtask),
                "worktree_id": "",
                "worktree_path": "",
                "worktree_mode": "",
            }
        )
    if not assignments:
        assignments.append(
            {
                "subtask_id": "T001.1",
                "status": "PENDING",
                "worker_id": str(resolved_workers[0]["worker_id"]),
                "role": str(resolved_workers[0]["role"]),
                "ownership_files": [],
                "worktree_id": "",
                "worktree_path": "",
                "worktree_mode": "",
            }
        )

    return {
        "schema_version": PARALLEL_COORDINATOR_VERSION,
        "feature": str(feature or ""),
        "planning_dir": str(planning_dir),
        "session_id": str(session_id or f"sess-{uuid4().hex[:10]}"),
        "run_id": str(run_id or f"run-{uuid4().hex[:10]}"),
        "strategy": "round_robin",
        "workers": resolved_workers,
        "assignments": assignments,
        "generated_at": _utc_now_iso(),
    }


def build_parallel_runtime_snapshot(
    *,
    plan: dict[str, Any],
    state: Any,
    worktree_snapshot: dict[str, Any],
) -> dict[str, Any]:
    assignments = inject_worktree_paths(
        assignments=list(plan.get("assignments") or []),
        worktree_snapshot=worktree_snapshot,
    )
    lookup = dict(getattr(state, "subtasks", {}) or {})
    worker_statuses, totals, merge_status = summarize_worker_statuses(
        assignments=assignments,
        subtask_lookup=lookup,
    )
    return {
        "schema_version": PARALLEL_RUNTIME_VERSION,
        "coordinator_version": PARALLEL_COORDINATOR_VERSION,
        "merge_semantics_version": MERGE_SEMANTICS_VERSION,
        "session_id": str(plan.get("session_id") or ""),
        "run_id": str(plan.get("run_id") or ""),
        "strategy": str(plan.get("strategy") or "round_robin"),
        "workers": list(plan.get("workers") or []),
        "assignments": assignments,
        "worktree": dict(worktree_snapshot),
        "worker_statuses": worker_statuses,
        "totals": totals,
        "merge_status": merge_status,
        "generated_at": _utc_now_iso(),
    }


__all__ = [
    "PARALLEL_COORDINATOR_VERSION",
    "PARALLEL_RUNTIME_VERSION",
    "build_parallel_coordinator_plan",
    "build_parallel_runtime_snapshot",
]


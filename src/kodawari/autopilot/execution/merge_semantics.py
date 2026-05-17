"""Worker-role and merge-status helpers for parallel execution runtime."""

from __future__ import annotations

from typing import Any


MERGE_SEMANTICS_VERSION = "parallel.merge_semantics.v1"


def normalize_worker_role(role: Any, *, worker_index: int = 0) -> str:
    normalized = str(role or "").strip().lower()
    if normalized in {"implementer", "reviewer", "verifier", "planner"}:
        return normalized
    if worker_index == 0:
        return "implementer"
    if worker_index == 1:
        return "reviewer"
    return "implementer"


def ownership_files_from_subtask(subtask: Any) -> list[str]:
    raw = getattr(subtask, "changed_files", None)
    if not isinstance(raw, list):
        return []
    deduped: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip().replace("\\", "/")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def merge_status_from_counts(*, done: int, failed: int, running: int, pending: int) -> str:
    if failed > 0:
        return "BLOCKED"
    if pending == 0 and running == 0 and done > 0:
        return "PASS"
    return "IN_PROGRESS"


def summarize_worker_statuses(
    *,
    assignments: list[dict[str, Any]],
    subtask_lookup: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    workers: dict[str, dict[str, Any]] = {}
    for assignment in assignments:
        worker_id = str(assignment.get("worker_id") or "").strip()
        if not worker_id:
            continue
        row = workers.setdefault(
            worker_id,
            {
                "worker_id": worker_id,
                "role": str(assignment.get("role") or ""),
                "worktree_path": str(assignment.get("worktree_path") or ""),
                "assigned_subtasks": [],
                "ownership_files": [],
                "done": 0,
                "failed": 0,
                "running": 0,
                "pending": 0,
            },
        )
        subtask_id = str(assignment.get("subtask_id") or "").strip()
        if subtask_id:
            row["assigned_subtasks"].append(subtask_id)
        for path in list(assignment.get("ownership_files") or []):
            normalized = str(path or "").strip().replace("\\", "/")
            if normalized and normalized not in row["ownership_files"]:
                row["ownership_files"].append(normalized)
        status = _subtask_status(subtask_lookup.get(subtask_id))
        if status == "DONE":
            row["done"] += 1
        elif status == "FAILED":
            row["failed"] += 1
        elif status == "RUNNING":
            row["running"] += 1
        else:
            row["pending"] += 1

    totals = {"done": 0, "failed": 0, "running": 0, "pending": 0}
    ordered = sorted(workers.values(), key=lambda item: str(item.get("worker_id") or ""))
    for row in ordered:
        totals["done"] += int(row["done"])
        totals["failed"] += int(row["failed"])
        totals["running"] += int(row["running"])
        totals["pending"] += int(row["pending"])
    status = merge_status_from_counts(
        done=totals["done"],
        failed=totals["failed"],
        running=totals["running"],
        pending=totals["pending"],
    )
    return ordered, totals, status


def _subtask_status(subtask: Any) -> str:
    if subtask is None:
        return "PENDING"
    status = getattr(subtask, "status", None)
    if hasattr(status, "value"):
        status = status.value
    normalized = str(status or "").strip().upper()
    if normalized in {"DONE", "FAILED", "RUNNING", "PENDING"}:
        return normalized
    return "PENDING"


__all__ = [
    "MERGE_SEMANTICS_VERSION",
    "merge_status_from_counts",
    "normalize_worker_role",
    "ownership_files_from_subtask",
    "summarize_worker_statuses",
]

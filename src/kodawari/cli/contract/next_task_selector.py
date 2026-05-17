"""Deterministic next-task selection for existing contract-first task graphs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import re
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.stage_profiles import RECOVERY, TAKE_TASK
from kodawari.cli.contract.contract_first_backlog import ordered_task_graph_tasks
from kodawari.cli.runtime.task_run_manifest import read_task_run_manifest


_PASS_REASONS = frozenset({"PROCEED_TO_GATE", "PIPELINE_FINISH", "PASS"})
_REVIEW_FIX_REASONS = frozenset({
    "OPUS_REVIEW_BLOCKED",
    "SELF_REVIEW_BLOCKED",
    "GATE_BLOCKED",
    "SCOPE_DRIFT_BLOCKED",
})
_RECOVERY_REASONS = frozenset({
    "VERIFY_BLOCKED",
    "EXECUTION_BACKEND_BLOCKED",
    "IMPLEMENTATION_ERROR",
    "MAX_CYCLES_REACHED",
    "MAX_CYCLES",
    "COLLABORATION_ROUND_LIMIT",
})
_TASK_ID_RE = re.compile(r"\bT[0-9]+[A-Z]?\b", re.IGNORECASE)


class PlanningAction(str, Enum):
    TAKE_TASK = "take_task"
    REVISE_TASK = "revise_task"
    INSERT_FOLLOWUP = "insert_followup"
    ESCALATE_TO_EPIC_REPLAN = "escalate_to_epic_replan"
    REVIEW_FIX_REQUIRED = "review_fix_required"
    RESUME_CURRENT = "resume_current"
    ALL_TASKS_COMPLETE = "all_tasks_complete"
    NO_EXECUTABLE_TASK = "no_executable_task"


@dataclass(frozen=True)
class SkippedTask:
    task_id: str
    reason: str
    details: str = ""
    blocked_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "reason": self.reason,
            "details": self.details,
            "blocked_by": list(self.blocked_by),
        }


@dataclass(frozen=True)
class TaskSelection:
    action: PlanningAction | str
    task_id: str = ""
    reason: str = ""
    stage_profile: str = TAKE_TASK.profile_id
    completed_task_ids: frozenset[str] = frozenset()
    skipped_tasks: tuple[SkippedTask, ...] = field(default_factory=tuple)
    latest_task_result: dict[str, Any] = field(default_factory=dict)

    @property
    def selected(self) -> bool:
        return bool(self.task_id)

    @property
    def action_value(self) -> str:
        return str(self.action.value if isinstance(self.action, PlanningAction) else self.action)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action_value,
            "task_id": self.task_id,
            "reason": self.reason,
            "stage_profile": self.stage_profile,
            "completed_task_ids": sorted(self.completed_task_ids),
            "skipped_tasks": [item.to_dict() for item in self.skipped_tasks],
            "latest_task_result": dict(self.latest_task_result),
        }


def select_next_task(task_graph: dict[str, Any], *, planning_dir: Path) -> TaskSelection:
    """Return the next task to run without calling a planner model."""
    tasks = ordered_task_graph_tasks(task_graph)
    task_ids = {_normalize_task_id(item.get("task_id")) for item in tasks}
    task_ids.discard("")
    if not tasks:
        return TaskSelection(action=PlanningAction.NO_EXECUTABLE_TASK, reason="TASK_GRAPH.json contains no tasks")

    completed = _completed_task_ids(planning_dir, known_task_ids=task_ids)
    latest = _latest_task_result(planning_dir)
    latest_selection = _selection_from_latest_result(latest, completed=completed, known_task_ids=task_ids)
    if latest_selection is not None:
        return latest_selection

    failed_or_blocked = _failed_or_blocked_task_ids(tasks, completed=completed)
    dep_index = _dep_index(tasks)

    skipped: list[SkippedTask] = []
    for task in tasks:
        task_id = _normalize_task_id(task.get("task_id"))
        if not task_id:
            skipped.append(SkippedTask(task_id="", reason="task_id_missing"))
            continue
        if task_id in completed:
            continue
        executability = str(dict(task.get("executability") or {}).get("status") or "").strip().upper()
        if executability == "FAIL":
            skipped.append(
                SkippedTask(
                    task_id=task_id,
                    reason="executability_failed",
                    details="; ".join(str(item) for item in list(dict(task.get("executability") or {}).get("issues") or [])),
                    blocked_by=(task_id,),
                )
            )
            continue
        deps = {
            _normalize_task_id(item)
            for item in list(task.get("depends_on") or [])
            if _normalize_task_id(item)
        }
        missing = sorted(dep for dep in deps if dep not in completed)
        if missing:
            # B1: trace the closure — the failing ancestor (executability_failed
            # or upstream of the missing chain) is more informative than just
            # naming the immediate missing dep. A user looking at the skip
            # report should see "T5 blocked because T1 failed" not just
            # "T5 blocked because T2 unsatisfied (and T2 because T1...)".
            ancestors = _trace_blocking_ancestors(
                task_id=task_id,
                dep_index=dep_index,
                failed_or_blocked=failed_or_blocked,
                completed=completed,
            )
            skipped.append(
                SkippedTask(
                    task_id=task_id,
                    reason="dependencies_unsatisfied",
                    details=", ".join(missing),
                    blocked_by=tuple(ancestors) if ancestors else tuple(missing),
                )
            )
            continue
        return TaskSelection(
            action=PlanningAction.TAKE_TASK,
            task_id=task_id,
            reason="first dependency-satisfied task not marked complete",
            stage_profile=TAKE_TASK.profile_id,
            completed_task_ids=frozenset(completed),
            skipped_tasks=tuple(skipped),
        )

    if completed.issuperset(task_ids):
        return TaskSelection(
            action=PlanningAction.ALL_TASKS_COMPLETE,
            reason="all task graph tasks are marked complete",
            completed_task_ids=frozenset(completed),
            skipped_tasks=tuple(skipped),
        )
    return TaskSelection(
        action=PlanningAction.NO_EXECUTABLE_TASK,
        reason="no incomplete task has satisfied dependencies and executable scope",
        completed_task_ids=frozenset(completed),
        skipped_tasks=tuple(skipped),
    )


def _failed_or_blocked_task_ids(tasks: list[dict[str, Any]], *, completed: set[str]) -> set[str]:
    """Identify root-cause failures (executability FAIL on a task that is NOT
    already completed). These are the ancestors a downstream skip should cite."""
    failed: set[str] = set()
    for task in tasks:
        task_id = _normalize_task_id(task.get("task_id"))
        if not task_id or task_id in completed:
            continue
        executability = str(dict(task.get("executability") or {}).get("status") or "").strip().upper()
        if executability == "FAIL":
            failed.add(task_id)
    return failed


def _dep_index(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    """task_id -> set of direct depends_on task_ids."""
    index: dict[str, set[str]] = {}
    for task in tasks:
        task_id = _normalize_task_id(task.get("task_id"))
        if not task_id:
            continue
        deps: set[str] = set()
        for item in list(task.get("depends_on") or []):
            normalized = _normalize_task_id(item)
            if normalized:
                deps.add(normalized)
        index[task_id] = deps
    return index


def _trace_blocking_ancestors(
    *,
    task_id: str,
    dep_index: dict[str, set[str]],
    failed_or_blocked: set[str],
    completed: set[str],
) -> list[str]:
    """Walk the depends_on chain from ``task_id`` and return ancestors that are
    failed (executability FAIL) — the actual root causes. Falls back to the
    immediate not-completed deps when no failure is on the path (the chain is
    just running serially)."""
    visited: set[str] = set()
    found: list[str] = []
    stack = list(dep_index.get(task_id, ()))
    while stack:
        current = stack.pop(0)
        if current in visited:
            continue
        visited.add(current)
        if current in failed_or_blocked:
            if current not in found:
                found.append(current)
            # Keep walking — multiple independent failed ancestors are possible.
        if current not in completed:
            stack.extend(item for item in dep_index.get(current, ()) if item not in visited)
    return sorted(found)


def _selection_from_latest_result(
    latest: dict[str, Any],
    *,
    completed: set[str],
    known_task_ids: set[str],
) -> TaskSelection | None:
    if not latest:
        return None
    task_id = _normalize_task_id(latest.get("task_id"))
    if not task_id:
        task_id = _task_id_from_text(str(latest.get("task") or latest.get("task_label") or ""))
    if not task_id or task_id not in known_task_ids or task_id in completed:
        return None
    reason = str(latest.get("reason") or "").strip().upper()
    status = str(latest.get("status") or "").strip().upper()
    if reason in _PASS_REASONS or status == "PASS":
        return None
    if reason in _REVIEW_FIX_REASONS:
        return TaskSelection(
            action=PlanningAction.REVIEW_FIX_REQUIRED,
            task_id=task_id,
            reason=f"latest task-run blocked by {reason}",
            stage_profile=RECOVERY.profile_id,
            completed_task_ids=frozenset(completed),
            latest_task_result=dict(latest),
        )
    if reason in _RECOVERY_REASONS or status in {"FAIL", "BLOCKED"}:
        return TaskSelection(
            action=PlanningAction.RESUME_CURRENT,
            task_id=task_id,
            reason=f"latest task-run did not pass ({reason or status})",
            stage_profile=RECOVERY.profile_id,
            completed_task_ids=frozenset(completed),
            latest_task_result=dict(latest),
        )
    return None


def _completed_task_ids(planning_dir: Path, *, known_task_ids: set[str]) -> set[str]:
    completed: set[str] = set()
    completed.update(_completed_from_tasks_markdown(planning_dir / "TASKS.md", known_task_ids=known_task_ids))
    completed.update(_completed_from_state(planning_dir / ".autopilot_state.json", known_task_ids=known_task_ids))
    completed.update(_completed_from_manifest(planning_dir, known_task_ids=known_task_ids))
    completed.update(_completed_from_latest_result(planning_dir / ".task_run_result.json", known_task_ids=known_task_ids))
    return completed


def _completed_from_tasks_markdown(path: Path, *, known_task_ids: set[str]) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    completed: set[str] = set()
    for line in text.splitlines():
        if not re.search(r"-\s*\[[xX]\]", line):
            continue
        task_id = _task_id_from_text(line)
        if task_id in known_task_ids:
            completed.add(task_id)
    return completed


def _completed_from_state(path: Path, *, known_task_ids: set[str]) -> set[str]:
    payload = _load_json(path)
    if not payload:
        return set()
    completed: set[str] = set()
    for item in list(payload.get("completed_tasks") or []):
        task_id = _task_id_from_text(str(item))
        if task_id in known_task_ids:
            completed.add(task_id)
    return completed


def _completed_from_manifest(planning_dir: Path, *, known_task_ids: set[str]) -> set[str]:
    manifest = read_task_run_manifest(planning_dir)
    if not manifest:
        return set()
    task_id = _normalize_task_id(manifest.get("task_id"))
    status = str(manifest.get("status") or "").strip().upper()
    if task_id in known_task_ids and status == "PASS":
        return {task_id}
    return set()


def _completed_from_latest_result(path: Path, *, known_task_ids: set[str]) -> set[str]:
    payload = _load_json(path)
    if not payload:
        return set()
    task_id = _normalize_task_id(payload.get("task_id")) or _task_id_from_text(str(payload.get("task") or ""))
    reason = str(payload.get("reason") or "").strip().upper()
    status = str(payload.get("status") or "").strip().upper()
    if task_id in known_task_ids and (status == "PASS" or reason in _PASS_REASONS):
        return {task_id}
    return set()


def _latest_task_result(planning_dir: Path) -> dict[str, Any]:
    payload = _load_json(planning_dir / ".task_run_result.json")
    if payload:
        return payload
    manifest = read_task_run_manifest(planning_dir)
    return dict(manifest or {})


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _task_id_from_text(text: str) -> str:
    match = _TASK_ID_RE.search(str(text or ""))
    return _normalize_task_id(match.group(0)) if match else ""


def _normalize_task_id(value: Any) -> str:
    return str(value or "").strip().upper()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "PlanningAction",
    "SkippedTask",
    "TaskSelection",
    "select_next_task",
]

"""Active-task scope derivation for planning review.

Background
----------
A planner may emit a multi-task epic plan, but the autopilot engine only
executes ONE task per run (the "active" one identified by ``TASK_CARD_ACTIVE.json``).
Reviewer feedback today applies to the whole epic — including tasks that
won't execute this run — so reviewer findings about *future* tasks were able
to block *current* execution. That mismatch let lite-lane runs auto-skip
plans whose active task was actually clean while spending planning rounds
re-litigating future-task structure.

This module gives the orchestrator a way to ask:
  * Which tasks should reviewer findings be allowed to block this run?
  * Given a finding, does it concern the active scope or a future task?

The "active scope" is the union of:
  1. the active task (selected by the engine for this run)
  2. its ``depends_on`` closure (upstream tasks the active task references)

Single-leaf plans collapse to a single-task scope. Multi-leaf plans use a
conservative union — any leaf in the plan counts as in-scope, so we never
silently demote a finding about a parallel task.

Active task selection priority:
  1. Hint from the caller (``hint_task_id``) — used when the engine already
     selected an active task before re-planning.
  2. Existing ``TASK_CARD_ACTIVE.json`` in the planning directory (reuse path).
  3. Topological first leaf — task with all ``depends_on`` satisfied within
     the plan's own task set, taking the planner's emission order as a tie
     break. With multiple leaves this returns the union of leaves (caller
     handles "conservative scope").
  4. Empty — no active scope, downstream uses ``unscoped`` semantics.

Notes:
  * We do NOT consult external state (no .task_run_result.json, no completed
    sets) here. This module answers "what is in scope for THIS round's
    reviewer findings?", not "what task should the engine run next?". Engine
    selection lives in cli/contract/next_task_selector.py and consults more
    sources.
  * No model calls. Pure structural derivation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_task_id(value: Any) -> str:
    return _clean_text(value)


def _plan_tasks(plan_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    payload = plan_payload or {}
    raw = payload.get("tasks") or []
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _all_task_ids(plan_payload: dict[str, Any] | None) -> list[str]:
    """Return ordered task ids from the plan, deduped, original emission order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for task in _plan_tasks(plan_payload):
        task_id = _normalize_task_id(task.get("task_id"))
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        ordered.append(task_id)
    return ordered


def _depends_on(task: dict[str, Any], known_ids: set[str]) -> set[str]:
    raw = task.get("depends_on")
    if not isinstance(raw, list):
        return set()
    return {
        _normalize_task_id(item)
        for item in raw
        if _normalize_task_id(item) and _normalize_task_id(item) in known_ids
    }


def topological_leaves(plan_payload: dict[str, Any] | None) -> list[str]:
    """Return tasks with no unfulfilled ``depends_on`` within the plan.

    "Leaf" here is the topology-input sense: a task that can run *first*
    because it has no prerequisites within the plan. Original emission order
    breaks ties (first in plan.tasks → first in result). Returns ``[]`` for
    an empty plan.
    """
    tasks = _plan_tasks(plan_payload)
    known_ids = {_normalize_task_id(task.get("task_id")) for task in tasks}
    known_ids.discard("")
    leaves: list[str] = []
    for task in tasks:
        task_id = _normalize_task_id(task.get("task_id"))
        if not task_id:
            continue
        if not _depends_on(task, known_ids):
            leaves.append(task_id)
    return leaves


def _read_task_card_active(planning_dir: Path | None) -> str:
    if planning_dir is None:
        return ""
    path = Path(planning_dir) / "TASK_CARD_ACTIVE.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    return _normalize_task_id(payload.get("task_id"))


def _depends_on_closure(task_id: str, plan_payload: dict[str, Any] | None) -> set[str]:
    """Return the transitive ``depends_on`` set for ``task_id``."""
    tasks_by_id = {
        _normalize_task_id(task.get("task_id")): task for task in _plan_tasks(plan_payload)
    }
    tasks_by_id.pop("", None)
    closure: set[str] = set()
    frontier = [task_id] if task_id in tasks_by_id else []
    while frontier:
        current = frontier.pop()
        task = tasks_by_id.get(current)
        if task is None:
            continue
        for dep in _depends_on(task, set(tasks_by_id)):
            if dep in closure or dep == task_id:
                continue
            closure.add(dep)
            frontier.append(dep)
    return closure


def derive_active_scope(
    *,
    plan_payload: dict[str, Any] | None,
    planning_dir: Path | None = None,
    hint_task_id: str = "",
) -> dict[str, Any]:
    """Return ``{active_task_ids: list[str], scope_task_ids: set[str], source: str}``.

    ``active_task_ids`` is the explicit selection (1-element list for a real
    active task, multi-element for the multi-leaf conservative case, empty
    when nothing can be derived). ``scope_task_ids`` is the union of every
    active task and its ``depends_on`` closure — the set within which a
    blocking finding should still block. ``source`` records how the active
    task was chosen, useful for audit and tests.
    """
    known_ids = set(_all_task_ids(plan_payload))

    hint = _normalize_task_id(hint_task_id)
    if hint and hint in known_ids:
        scope = {hint, *_depends_on_closure(hint, plan_payload)}
        return {
            "active_task_ids": [hint],
            "scope_task_ids": scope,
            "source": "hint",
        }

    card_active = _read_task_card_active(planning_dir)
    if card_active and card_active in known_ids:
        scope = {card_active, *_depends_on_closure(card_active, plan_payload)}
        return {
            "active_task_ids": [card_active],
            "scope_task_ids": scope,
            "source": "task_card_active",
        }

    leaves = topological_leaves(plan_payload)
    if len(leaves) == 1:
        leaf = leaves[0]
        scope = {leaf, *_depends_on_closure(leaf, plan_payload)}
        return {
            "active_task_ids": [leaf],
            "scope_task_ids": scope,
            "source": "topological_first_leaf",
        }
    if leaves:
        scope: set[str] = set()
        for leaf in leaves:
            scope.add(leaf)
            scope |= _depends_on_closure(leaf, plan_payload)
        return {
            "active_task_ids": list(leaves),
            "scope_task_ids": scope,
            "source": "topological_multi_leaf",
        }

    return {
        "active_task_ids": [],
        "scope_task_ids": set(),
        "source": "unscoped",
    }


__all__ = [
    "derive_active_scope",
    "topological_leaves",
]

"""Helpers that bridge contract-first task graphs into legacy task-cycle runtime."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.task_card import build_task_card, validate_task_card
from kodawari.cli.contract.contract_first_schema import (
    ContractFirstSchemaValidationError,
    load_contract_first_artifact,
    validate_contract_first_payload,
    write_contract_first_artifact,
)
from kodawari.cli.io_atomic import CorruptArtifactError


logger = logging.getLogger(__name__)


def load_task_graph_payload(planning_dir: Path) -> dict[str, Any] | None:
    path = planning_dir / "TASK_GRAPH.json"
    if not path.exists():
        return None
    try:
        payload = load_contract_first_artifact(path, schema_name="task_graph")
    except (ContractFirstSchemaValidationError, CorruptArtifactError, ValueError):
        logger.warning("failed to load contract-first task graph for task-cycle runtime", exc_info=True)
        return None
    return dict(payload) if isinstance(payload, dict) else None


def task_graph_backlog_entries(
    planning_dir: Path,
    *,
    exclude_task_ids: set[str] | None = None,
    completed_task_ids: set[str] | None = None,
    include_completed: bool = False,
) -> list[dict[str, Any]]:
    task_graph = load_task_graph_payload(planning_dir)
    if task_graph is None:
        return []
    excluded = {str(item).strip().upper() for item in set(exclude_task_ids or set()) if str(item).strip()}
    completed = {str(item).strip().upper() for item in set(completed_task_ids or set()) if str(item).strip()}
    backlog: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in ordered_task_graph_tasks(task_graph):
        task_id = str(task.get("task_id") or "").strip().upper()
        if not task_id or task_id in seen or task_id in excluded:
            continue
        task_name = str(task.get("task_name") or task_id).strip() or task_id
        is_completed = task_id in completed
        if is_completed and not include_completed:
            continue
        backlog.append(
            {
                "task_id": task_id,
                "label": f"{task_id}: {task_name}",
                "scope": task_name,
                "completed": is_completed,
                "implicit": False,
            }
        )
        seen.add(task_id)
    return backlog


def render_task_graph_tasks_markdown(
    planning_dir: Path,
    *,
    feature: str,
    completed_task_ids: set[str] | None = None,
) -> str | None:
    tasks = task_graph_backlog_entries(
        planning_dir,
        completed_task_ids=completed_task_ids,
        include_completed=True,
    )
    if not tasks:
        return None
    lines = [f"# TASKS ({feature})", ""]
    for task in tasks:
        mark = "x" if bool(task.get("completed")) else " "
        lines.append(f"- [{mark}] {task['task_id']}: {task['scope']}")
    return "\n".join(lines) + "\n"


def activate_task_card(planning_dir: Path, task_id: str) -> dict[str, Any] | None:
    normalized_id = str(task_id or "").strip().upper()
    if not normalized_id:
        return None
    named_path = planning_dir / f"TASK_CARD_{normalized_id}.json"
    payload = _load_task_card_payload(named_path)
    if payload is None:
        task_graph = load_task_graph_payload(planning_dir)
        if task_graph is None:
            return None
        try:
            payload = build_task_card(task_graph, normalized_id)
        except ValueError:
            logger.warning("failed to materialize task card for %s", normalized_id, exc_info=True)
            return None
        errors = validate_task_card(
            payload,
            planning_mode=str(task_graph.get("planning_mode") or "existing"),
        )
        if errors:
            logger.warning("task card validation failed for %s: %s", normalized_id, "; ".join(errors))
            return None
        if not _write_task_card_payload(named_path, payload):
            return None
    active_path = planning_dir / "TASK_CARD_ACTIVE.json"
    if not _write_task_card_payload(active_path, payload):
        return None
    return dict(payload)


def ordered_task_graph_tasks(task_graph: dict[str, Any]) -> list[dict[str, Any]]:
    raw_tasks = [dict(item) for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)]
    if not raw_tasks:
        return []
    task_by_id: dict[str, dict[str, Any]] = {}
    original_order: list[str] = []
    for task in raw_tasks:
        task_id = str(task.get("task_id") or "").strip().upper()
        if not task_id or task_id in task_by_id:
            continue
        task_by_id[task_id] = task
        original_order.append(task_id)

    pending: dict[str, set[str]] = {}
    dependents: dict[str, list[str]] = {task_id: [] for task_id in original_order}
    for task_id in original_order:
        deps = {
            str(dep).strip().upper()
            for dep in list(task_by_id[task_id].get("depends_on") or [])
            if str(dep).strip().upper() in task_by_id
        }
        pending[task_id] = deps
        for dep in deps:
            dependents.setdefault(dep, []).append(task_id)

    ready = [task_id for task_id in original_order if not pending[task_id]]
    ordered: list[dict[str, Any]] = []
    emitted: set[str] = set()
    while ready:
        task_id = ready.pop(0)
        if task_id in emitted:
            continue
        ordered.append(task_by_id[task_id])
        emitted.add(task_id)
        for dependent in dependents.get(task_id, []):
            remaining = pending.get(dependent)
            if remaining is None:
                continue
            remaining.discard(task_id)
            if not remaining and dependent not in emitted and dependent not in ready:
                ready.append(dependent)

    for task_id in original_order:
        if task_id not in emitted:
            ordered.append(task_by_id[task_id])
    return ordered


def _load_task_card_payload(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = load_contract_first_artifact(path, schema_name="task_card")
    except (ContractFirstSchemaValidationError, CorruptArtifactError, ValueError):
        logger.warning("failed to load contract-first task card from %s", path, exc_info=True)
        return None
    return dict(payload) if isinstance(payload, dict) else None


def _write_task_card_payload(path: Path, payload: dict[str, Any]) -> bool:
    try:
        validate_contract_first_payload("task_card", payload)
        write_contract_first_artifact(path, payload, schema_name="task_card")
    except (ContractFirstSchemaValidationError, CorruptArtifactError, ValueError):
        logger.warning("failed to write contract-first task card to %s", path, exc_info=True)
        return False
    return True



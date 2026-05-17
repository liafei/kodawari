"""Task selection and active-card activation helpers for the bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from kodawari.autopilot.planning.task_card import build_task_card
from kodawari.cli.contract.bridge_types import AutopilotPlanningBridgeError
from kodawari.cli.contract.next_task_selector import PlanningAction, TaskSelection, select_next_task


def select_task_or_raise(planning_dir: Path, task_graph: dict[str, Any]) -> TaskSelection:
    selection = select_next_task(task_graph, planning_dir=planning_dir)
    if selection.selected:
        return selection
    if selection.action_value == PlanningAction.ALL_TASKS_COMPLETE.value:
        raise AutopilotPlanningBridgeError(
            error_code="task_graph_complete",
            message="All tasks in TASK_GRAPH.json are already marked complete.",
            remediation=[
                "Close this feature, or run with --replan/updated PRD input to extend the task graph.",
            ],
            details={"selection": selection.to_dict()},
        )
    raise AutopilotPlanningBridgeError(
        error_code="task_graph_no_ready_task",
        message=selection.reason or "No executable task is ready from TASK_GRAPH.json.",
        remediation=[
            "Complete prerequisite tasks, fix task executability, or regenerate TASK_GRAPH.json with --replan.",
        ],
        details={"selection": selection.to_dict()},
    )


def activate_selected_task_card(
    planning_dir: Path,
    *,
    task_graph: dict[str, Any],
    task_id: str,
    steps_run: list[str],
    artifacts: dict[str, str],
    load_active_card: Callable[[Path], dict[str, Any] | None],
    write_task_card: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    active_path = planning_dir / "TASK_CARD_ACTIVE.json"
    payload = load_active_card(active_path)
    try:
        desired = build_task_card(task_graph, task_id)
    except ValueError as exc:
        raise AutopilotPlanningBridgeError(
            error_code="task_card_invalid",
            message=str(exc),
            remediation=["Fix task executability or narrow the task scope before rerunning autopilot."],
            details={"task_id": task_id},
        ) from exc
    if (
        payload is None
        or str(payload.get("task_id") or "").strip().upper() != task_id
        or task_card_semantic_payload(payload) != task_card_semantic_payload(desired)
    ):
        payload = write_task_card(task_id)
        steps_run.append("task-prepare")
    artifacts[active_path.name] = str(active_path.resolve())
    named_path = planning_dir / f"TASK_CARD_{task_id}.json"
    if named_path.exists():
        artifacts[named_path.name] = str(named_path.resolve())
    return payload


def task_card_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ignored = {"generated_at"}
    return {
        str(key): value
        for key, value in sorted(dict(payload or {}).items(), key=lambda item: str(item[0]))
        if str(key) not in ignored
    }


def select_primary_task_id(task_graph: dict[str, Any]) -> str:
    tasks = [dict(item) for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)]
    if not tasks:
        raise AutopilotPlanningBridgeError(
            error_code="task_graph_empty",
            message="TASK_GRAPH.json does not contain any executable tasks.",
            remediation=["Refine the PRD or architecture plan so task-plan can produce at least one task."],
        )
    for task in tasks:
        task_id = str(task.get("task_id") or "").strip().upper()
        executability = str(dict(task.get("executability") or {}).get("status") or "").upper()
        if task_id and executability != "FAIL":
            return task_id
    fallback = str(tasks[0].get("task_id") or "").strip().upper()
    if fallback:
        return fallback
    raise AutopilotPlanningBridgeError(
        error_code="task_id_missing",
        message="Unable to resolve a primary task id from TASK_GRAPH.json.",
        remediation=["Fix TASK_GRAPH.json so each task has a valid task_id."],
    )


def task_runtime(task_card: dict[str, Any]) -> tuple[str, str]:
    task_id = str(task_card.get("task_id") or "TASK").strip().upper()
    task_name = str(task_card.get("task_name") or "Contract-first task").strip()
    files = [str(item) for item in list(task_card.get("files_to_change") or []) if str(item).strip()]
    task_label = f"{task_id}: {task_name}"
    task_scope = f"files_to_change={files}; test_plan={str(task_card.get('test_plan') or '').strip()}"
    return task_label, task_scope


__all__ = [
    "activate_selected_task_card",
    "select_primary_task_id",
    "select_task_or_raise",
    "task_card_semantic_payload",
    "task_runtime",
]

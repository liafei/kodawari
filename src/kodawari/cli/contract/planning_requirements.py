"""Shared planning strictness helpers for generic contract-first flows."""

from __future__ import annotations

from typing import Any


CONTRACT_FIRST_BASE_ARTIFACTS = (
    "PRD_INTAKE.json",
    "REPO_INVENTORY.json",
    "TASK_GRAPH.json",
    "TASK_CARD_ACTIVE.json",
)


class PlanningStrictnessError(ValueError):
    """Raised when generic contract-first planning prerequisites are missing."""

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        requirements: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.error_code = str(error_code)
        self.requirements = dict(requirements)


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _surface_count(repo_inventory: dict[str, Any] | None) -> int:
    surfaces = list((repo_inventory or {}).get("surfaces") or [])
    return sum(1 for item in surfaces if isinstance(item, dict))


def _planning_mode(
    *,
    repo_inventory: dict[str, Any] | None,
    architecture_plan: dict[str, Any] | None,
    default_mode: str = "existing",
) -> str:
    architecture_mode = _clean_text((architecture_plan or {}).get("planning_mode"))
    if architecture_mode:
        return architecture_mode
    inventory_mode = _clean_text((repo_inventory or {}).get("mode"))
    if inventory_mode:
        return inventory_mode
    return _clean_text(default_mode, default="existing")


def contract_first_planning_requirements(
    *,
    repo_inventory: dict[str, Any] | None,
    architecture_plan: dict[str, Any] | None,
    planning_conversation: dict[str, Any] | None = None,
    default_mode: str = "existing",
) -> dict[str, Any]:
    if planning_conversation is not None:
        planning_mode = _planning_mode(
            repo_inventory=repo_inventory,
            architecture_plan=architecture_plan,
            default_mode=default_mode,
        )
        return {
            "mode": "contract_first",
            "planning_mode": planning_mode,
            "surface_count": _surface_count(repo_inventory),
            "requires_architecture_plan": False,
            "required_artifacts": [
                "PLANNING_CONVERSATION.json",
                "REPO_INVENTORY.json",
                "TASK_GRAPH.json",
                "TASK_CARD_ACTIVE.json",
            ],
        }
    planning_mode = _planning_mode(
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        default_mode=default_mode,
    )
    surface_count = _surface_count(repo_inventory)
    requires_architecture = planning_mode == "greenfield" or surface_count > 1
    required = list(CONTRACT_FIRST_BASE_ARTIFACTS)
    if requires_architecture:
        required.append("ARCHITECTURE_PLAN.json")
    return {
        "mode": "contract_first",
        "planning_mode": planning_mode,
        "surface_count": surface_count,
        "requires_architecture_plan": requires_architecture,
        "required_artifacts": required,
    }


def planning_truth_source(
    *,
    repo_inventory: dict[str, Any] | None,
    architecture_plan: dict[str, Any] | None,
    planning_conversation: dict[str, Any] | None = None,
) -> str:
    if planning_conversation is not None:
        return "PLANNING_CONVERSATION.json+REPO_INVENTORY.json+TASK_GRAPH.json+TASK_CARD_ACTIVE.json"
    if architecture_plan and repo_inventory:
        return "PRD_INTAKE.json+REPO_INVENTORY.json+ARCHITECTURE_PLAN.json+TASK_GRAPH.json+TASK_CARD_ACTIVE.json"
    if repo_inventory:
        return "PRD_INTAKE.json+REPO_INVENTORY.json+TASK_GRAPH.json+TASK_CARD_ACTIVE.json"
    return "PRD_INTAKE.json+TASK_GRAPH.json+TASK_CARD_ACTIVE.json"


def validate_task_plan_requirements(
    *,
    repo_inventory: dict[str, Any] | None,
    architecture_plan: dict[str, Any] | None,
    planning_conversation: dict[str, Any] | None = None,
    default_mode: str,
) -> dict[str, Any]:
    requirements = contract_first_planning_requirements(
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        planning_conversation=planning_conversation,
        default_mode=default_mode,
    )
    if planning_conversation is not None:
        return requirements
    if architecture_plan is not None:
        return requirements
    if not requirements["requires_architecture_plan"]:
        return requirements
    raise PlanningStrictnessError(
        error_code="architecture_plan_required",
        message=(
            "ARCHITECTURE_PLAN.json is required before task-plan for greenfield "
            "or multi-surface repositories."
        ),
        requirements=requirements,
    )


__all__ = [
    "CONTRACT_FIRST_BASE_ARTIFACTS",
    "PlanningStrictnessError",
    "contract_first_planning_requirements",
    "planning_truth_source",
    "validate_task_plan_requirements",
]

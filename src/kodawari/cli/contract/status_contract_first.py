"""Contract-first status helpers for generic planning truth."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.cli.contract.contract_first_schema import load_contract_first_artifact
from kodawari.cli.contract.planning_requirements import (
    contract_first_planning_requirements,
    planning_truth_source,
)


_STATUS_CONTRACT_ARTIFACTS = (
    "PLANNING_CONVERSATION.json",
    "PRD_INTAKE.json",
    "REPO_INVENTORY.json",
    "ARCHITECTURE_PLAN.json",
    "TASK_GRAPH.json",
    "TASK_CARD_ACTIVE.json",
)


def _load_optional_contract_artifact(
    planning_dir: Path,
    name: str,
    *,
    schema_name: str | None,
) -> tuple[dict[str, Any] | None, bool]:
    path = (planning_dir / name).resolve()
    if not path.exists():
        return None, False
    try:
        payload = load_contract_first_artifact(path, schema_name=schema_name)
    except ValueError:
        return None, True
    return payload, False


def detect_status_planning_mode(planning_dir: Path) -> str:
    return "contract_first" if any((planning_dir / name).exists() for name in _STATUS_CONTRACT_ARTIFACTS) else "legacy"


def build_contract_first_planning_status(planning_dir: Path) -> dict[str, Any]:
    planning_conversation, planning_conversation_invalid = _load_optional_contract_artifact(
        planning_dir,
        "PLANNING_CONVERSATION.json",
        schema_name="planning_conversation",
    )
    repo_inventory, repo_invalid = _load_optional_contract_artifact(
        planning_dir,
        "REPO_INVENTORY.json",
        schema_name="repo_inventory",
    )
    architecture_plan, architecture_invalid = _load_optional_contract_artifact(
        planning_dir,
        "ARCHITECTURE_PLAN.json",
        schema_name="architecture_plan",
    )
    requirements = contract_first_planning_requirements(
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        planning_conversation=planning_conversation,
    )
    missing, invalid, present = _artifact_presence(
        planning_dir=planning_dir,
        required=requirements["required_artifacts"],
    )
    if repo_invalid and "REPO_INVENTORY.json" not in invalid:
        invalid.append("REPO_INVENTORY.json")
    if architecture_invalid and "ARCHITECTURE_PLAN.json" not in invalid:
        invalid.append("ARCHITECTURE_PLAN.json")
    if planning_conversation_invalid and "PLANNING_CONVERSATION.json" not in invalid:
        invalid.append("PLANNING_CONVERSATION.json")
    return {
        "planning_mode": "contract_first",
        "planning_conversation_present": planning_conversation is not None,
        "repo_inventory_present": repo_inventory is not None,
        "architecture_plan_present": architecture_plan is not None,
        "planning_requirements": requirements,
        "planning_complete": not missing and not invalid,
        "planning_truth_source": planning_truth_source(
            repo_inventory=repo_inventory,
            architecture_plan=architecture_plan,
            planning_conversation=planning_conversation,
        ),
        "required_artifacts": list(requirements["required_artifacts"]),
        "present_artifacts": present,
        "missing_artifacts": missing,
        "invalid_artifacts": invalid,
    }


def _artifact_presence(
    *,
    planning_dir: Path,
    required: list[str],
) -> tuple[list[str], list[str], list[str]]:
    missing: list[str] = []
    invalid: list[str] = []
    present: list[str] = []
    schema_names = {
        "PLANNING_CONVERSATION.json": "planning_conversation",
        "PRD_INTAKE.json": "prd_intake",
        "REPO_INVENTORY.json": "repo_inventory",
        "ARCHITECTURE_PLAN.json": "architecture_plan",
        "TASK_GRAPH.json": "task_graph",
        "TASK_CARD_ACTIVE.json": "task_card",
    }
    for name in required:
        payload, is_invalid = _load_optional_contract_artifact(
            planning_dir,
            name,
            schema_name=schema_names.get(name),
        )
        if is_invalid:
            invalid.append(name)
            continue
        if payload is None:
            missing.append(name)
            continue
        present.append(name)
    return missing, invalid, present


__all__ = [
    "build_contract_first_planning_status",
    "detect_status_planning_mode",
]


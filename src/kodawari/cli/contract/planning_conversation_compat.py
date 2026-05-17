"""Compatibility helpers for model-driven planning artifacts.

REMOVE_AFTER: 2026-08-01
REMOVAL_PLAN: Remove once PLANNING_CONVERSATION.json v1 migration is complete across all active planning dirs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.cli.contract.contract_first_schema import load_contract_first_artifact


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _load_optional_contract_artifact(path: Path, *, schema_name: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = load_contract_first_artifact(path, schema_name=schema_name)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def load_planning_conversation(planning_dir: Path) -> dict[str, Any] | None:
    return _load_optional_contract_artifact(
        (planning_dir / "PLANNING_CONVERSATION.json").resolve(),
        schema_name="planning_conversation",
    )


def load_repo_inventory(planning_dir: Path) -> dict[str, Any] | None:
    return _load_optional_contract_artifact(
        (planning_dir / "REPO_INVENTORY.json").resolve(),
        schema_name="repo_inventory",
    )


def load_architecture_plan(planning_dir: Path) -> dict[str, Any] | None:
    return _load_optional_contract_artifact(
        (planning_dir / "ARCHITECTURE_PLAN.json").resolve(),
        schema_name="architecture_plan",
    )


def load_prd_intake_compatible(planning_dir: Path) -> dict[str, Any] | None:
    intake = _load_optional_contract_artifact(
        (planning_dir / "PRD_INTAKE.json").resolve(),
        schema_name="prd_intake",
    )
    if intake is not None:
        return intake
    conversation = load_planning_conversation(planning_dir)
    if conversation is None:
        return None
    source_of_truth = _string_list(conversation.get("source_of_truth"))
    source_of_truth_canonical = _string_list(conversation.get("source_of_truth_canonical")) or list(source_of_truth)
    return {
        "schema_version": "planning.conversation.compat.prd_intake.v1",
        "business_outcome": _clean_text(conversation.get("business_outcome")),
        "out_of_scope": _string_list(conversation.get("out_of_scope")),
        "source_of_truth": source_of_truth,
        "source_of_truth_canonical": source_of_truth_canonical,
        "confidence": _clean_text(conversation.get("confidence")),
        "confidence_issues": _string_list(conversation.get("confidence_issues")),
    }


def load_architecture_plan_compatible(planning_dir: Path) -> dict[str, Any] | None:
    architecture = load_architecture_plan(planning_dir)
    if architecture is not None:
        return architecture
    conversation = load_planning_conversation(planning_dir)
    if conversation is None:
        return None
    execution_constraints = conversation.get("execution_constraints")
    return {
        "schema_version": "planning.conversation.compat.architecture_plan.v1",
        "archetype": _clean_text(conversation.get("archetype")),
        "capabilities": _string_list(conversation.get("capabilities")),
        "module_boundaries": _dict_list(conversation.get("module_boundaries")),
        "verify_recipes": _dict_list(conversation.get("verify_recipes")),
        "approval_points": _dict_list(conversation.get("approval_points")),
        "execution_constraints": dict(execution_constraints or {}) if isinstance(execution_constraints, dict) else {},
        "confidence": _clean_text(conversation.get("confidence")),
        "confidence_issues": _string_list(conversation.get("confidence_issues")),
    }


__all__ = [
    "load_architecture_plan",
    "load_architecture_plan_compatible",
    "load_planning_conversation",
    "load_prd_intake_compatible",
    "load_repo_inventory",
]


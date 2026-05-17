"""Canonical architecture-plan helpers for generic workflow planning."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.prd_contract import PRD_INTAKE_SCHEMA_VERSION
from kodawari.autopilot.planning.repo_inventory import build_repo_inventory
from kodawari.project_model import default_verify_command_for_surface, derive_task_layers
from kodawari.source_of_truth import build_contract_coverage_hints


SCHEMA_VERSION = "contract_first.architecture_plan.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _module_layers(surface_name: str, recommended_layers: list[str]) -> list[str]:
    if surface_name == "backend":
        layers = [item for item in recommended_layers if item in {"schema", "repository", "service", "route", "model", "util"}]
        return layers or ["service", "route"]
    if surface_name == "frontend":
        return ["frontend", "util"]
    if surface_name == "mobile_wrapper":
        return ["frontend"]
    return ["util"]


def _module_boundaries(*, surfaces: list[dict[str, Any]], recommended_layers: list[str]) -> list[dict[str, Any]]:
    boundaries: list[dict[str, Any]] = []
    for surface in surfaces:
        name = _clean_text(surface.get("name"))
        if not name:
            continue
        boundaries.append(
            {
                "name": f"{name}_surface",
                "surface": name,
                "roots": _string_list(surface.get("roots")),
                "layers": _module_layers(name, recommended_layers),
            }
        )
    return boundaries


def _verify_recipes(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    recipes: list[dict[str, Any]] = []
    for surface in surfaces:
        name = _clean_text(surface.get("name"))
        if not name:
            continue
        command = default_verify_command_for_surface(surface)
        recipes.append(
            {
                "surface": name,
                "command": command,
                "required": bool(command),
                "roots": _string_list(surface.get("roots")),
            }
        )
    return recipes


def _approval_points(*, planning_mode: str, surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    points = [
        {
            "name": "architecture_freeze",
            "required": True,
            "reason": "Confirm the project archetype, capabilities, and boundaries before task generation.",
        }
    ]
    if planning_mode == "greenfield" or len(surfaces) > 2:
        points.append(
            {
                "name": "execution_plan",
                "required": True,
                "reason": "Review multi-surface task ordering before implementation starts.",
            }
        )
    points.append(
        {
            "name": "release",
            "required": True,
            "reason": "Human approval remains required before shipping.",
        }
    )
    return points


def _confidence_issues(*, prd_intake: dict[str, Any]) -> list[str]:
    issues = [str(item) for item in list(prd_intake.get("confidence_issues") or []) if str(item).strip()]
    deduped: list[str] = []
    for item in issues:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _resolve_architecture_inputs(
    *,
    project_root: Path,
    prd_intake: dict[str, Any],
    repo_inventory: dict[str, Any] | None,
    archetype: str,
    capabilities: list[str] | None,
    planning_mode: str,
) -> tuple[dict[str, Any], str, list[str], list[dict[str, Any]], list[str], list[str], str]:
    inventory = dict(
        repo_inventory
        or build_repo_inventory(
            project_root=project_root,
            archetype=archetype,
            capabilities=capabilities,
            mode=planning_mode,
        )
    )
    # A2: in greenfield mode, fall back to "auto" instead of silently coercing
    # the project into a FastAPI shape. The other three call sites
    # (project_model.py:405/408, task_graph.py:757) keep the legacy
    # "fastapi_api" default to preserve back-compat for existing projects.
    archetype_default = "auto" if str(planning_mode or "").strip().lower() == "greenfield" else "fastapi_api"
    resolved_archetype = _clean_text(inventory.get("archetype"), default=archetype_default)
    resolved_capabilities = _string_list(inventory.get("capabilities"))
    surfaces = [dict(item) for item in list(inventory.get("surfaces") or []) if isinstance(item, dict)]
    source_of_truth = _string_list(prd_intake.get("source_of_truth"))
    source_of_truth_canonical = _string_list(prd_intake.get("source_of_truth_canonical")) or source_of_truth
    path_type = _clean_text(prd_intake.get("path_type"), default="read").lower()
    return (
        inventory,
        resolved_archetype,
        resolved_capabilities,
        surfaces,
        source_of_truth,
        source_of_truth_canonical,
        path_type,
    )


def _recommended_layers(
    *,
    resolved_archetype: str,
    resolved_capabilities: list[str],
    prd_intake: dict[str, Any],
) -> list[str]:
    return derive_task_layers(
        archetype=resolved_archetype,
        capabilities=resolved_capabilities,
        prd_layers=_string_list(prd_intake.get("layers")),
    )


def _plan_payload(
    *,
    prd_intake: dict[str, Any],
    planning_mode: str,
    resolved_archetype: str,
    resolved_capabilities: list[str],
    source_of_truth: list[str],
    source_of_truth_canonical: list[str],
    path_type: str,
    recommended_layers: list[str],
    surfaces: list[dict[str, Any]],
    verify_recipes: list[dict[str, Any]],
    confidence_issues: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "feature": _clean_text(prd_intake.get("feature")),
        "planning_mode": _clean_text(planning_mode, default="existing"),
        "semantic_source": "project_model+repo_inventory",
        "business_outcome": _clean_text(prd_intake.get("business_outcome")),
        "archetype": resolved_archetype,
        "capabilities": resolved_capabilities,
        "source_of_truth": source_of_truth,
        "source_of_truth_canonical": source_of_truth_canonical,
        "path_type": path_type,
        "coverage_hints": build_contract_coverage_hints(
            layers=recommended_layers,
            path_type=path_type,
            source_of_truth_canonical=source_of_truth_canonical,
        ),
        "recommended_layers": recommended_layers,
        "surfaces": surfaces,
        "module_boundaries": _module_boundaries(
            surfaces=surfaces,
            recommended_layers=recommended_layers,
        ),
        "verify_recipes": verify_recipes,
        "approval_points": _approval_points(
            planning_mode=_clean_text(planning_mode, default="existing"),
            surfaces=surfaces,
        ),
        "execution_constraints": {
            "native_executor_required": True,
            "review_required": True,
            "max_core_files_per_task": 3,
        },
        "confidence": "low" if confidence_issues else "high",
        "confidence_issues": confidence_issues,
    }


def build_architecture_plan(
    *,
    project_root: Path,
    prd_intake: dict[str, Any] | None = None,
    repo_inventory: dict[str, Any] | None = None,
    archetype: str = "auto",
    capabilities: list[str] | None = None,
    planning_mode: str = "existing",
) -> dict[str, Any]:
    intake = dict(prd_intake or {})
    (
        _inventory,
        resolved_archetype,
        resolved_capabilities,
        surfaces,
        source_of_truth,
        source_of_truth_canonical,
        path_type,
    ) = _resolve_architecture_inputs(
        project_root=project_root,
        prd_intake=intake,
        repo_inventory=repo_inventory,
        archetype=archetype,
        capabilities=capabilities,
        planning_mode=planning_mode,
    )
    recommended_layers = _recommended_layers(
        resolved_archetype=resolved_archetype,
        resolved_capabilities=resolved_capabilities,
        prd_intake=intake,
    )
    verify_recipes = _verify_recipes(surfaces)
    confidence_issues = _confidence_issues(prd_intake=intake)
    return _plan_payload(
        prd_intake=intake,
        planning_mode=planning_mode,
        resolved_archetype=resolved_archetype,
        resolved_capabilities=resolved_capabilities,
        source_of_truth=source_of_truth,
        source_of_truth_canonical=source_of_truth_canonical,
        path_type=path_type,
        recommended_layers=recommended_layers,
        surfaces=surfaces,
        verify_recipes=verify_recipes,
        confidence_issues=confidence_issues,
    )


def architecture_plan_to_prd_intake(payload: dict[str, Any]) -> dict[str, Any]:
    recommended_layers = _string_list(payload.get("recommended_layers"))
    source_of_truth_canonical = _string_list(payload.get("source_of_truth_canonical"))
    return {
        "schema_version": PRD_INTAKE_SCHEMA_VERSION,
        "generated_at": _clean_text(payload.get("generated_at")),
        "feature": _clean_text(payload.get("feature")),
        "business_outcome": _clean_text(payload.get("business_outcome")),
        "source_of_truth": _string_list(payload.get("source_of_truth")),
        "source_of_truth_canonical": source_of_truth_canonical,
        "path_type": _clean_text(payload.get("path_type"), default="read"),
        "layers": recommended_layers,
        "coverage_hints": _string_list(payload.get("coverage_hints"))
        or build_contract_coverage_hints(
            layers=recommended_layers,
            path_type=_clean_text(payload.get("path_type"), default="read"),
            source_of_truth_canonical=source_of_truth_canonical,
        ),
        "out_of_scope": [],
        "confidence": _clean_text(payload.get("confidence"), default="high"),
        "confidence_issues": _string_list(payload.get("confidence_issues")),
    }


def render_architecture_plan_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Architecture Plan",
        "",
        f"- archetype: {_clean_text(payload.get('archetype'))}",
        f"- capabilities: {', '.join(_string_list(payload.get('capabilities')))}",
        f"- planning_mode: {_clean_text(payload.get('planning_mode'))}",
        f"- confidence: {_clean_text(payload.get('confidence'), default='high')}",
        "",
        "## Surfaces",
    ]
    surfaces = [dict(item) for item in list(payload.get("surfaces") or []) if isinstance(item, dict)]
    if surfaces:
        for item in surfaces:
            lines.append(
                f"- {item.get('name', '')}: roots={', '.join(_string_list(item.get('roots')))}; "
                f"verify={_clean_text(item.get('verify_command'), default='missing')}"
            )
    else:
        lines.append("- (none)")
    lines.extend(["", "## Module Boundaries"])
    boundaries = [dict(item) for item in list(payload.get("module_boundaries") or []) if isinstance(item, dict)]
    if boundaries:
        for item in boundaries:
            lines.append(
                f"- {item.get('name', '')}: surface={item.get('surface', '')}; "
                f"roots={', '.join(_string_list(item.get('roots')))}; "
                f"layers={', '.join(_string_list(item.get('layers')))}"
            )
    else:
        lines.append("- (none)")
    lines.extend(["", "## Approval Points"])
    for item in list(payload.get("approval_points") or []):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- {item.get('name', '')}: required={bool(item.get('required'))}; reason={_clean_text(item.get('reason'))}"
        )
    confidence_issues = _string_list(payload.get("confidence_issues"))
    if confidence_issues:
        lines.extend(["", "## Confidence Issues"])
        lines.extend(f"- {item}" for item in confidence_issues)
    return "\n".join(lines).rstrip() + "\n"


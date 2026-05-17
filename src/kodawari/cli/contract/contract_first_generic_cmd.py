"""Additive generic planning commands for contract-first workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.architecture_plan import (
    architecture_plan_to_prd_intake,
    build_architecture_plan,
    render_architecture_plan_markdown,
)
from kodawari.autopilot.planning.init_scaffold import (
    SCAFFOLD_MANIFEST_FILENAME,
    scaffold_project,
    write_scaffold_manifest,
)
from kodawari.autopilot.planning.prd_contract import build_prd_intake, render_prd_intake_markdown
from kodawari.autopilot.planning.repo_inventory import build_repo_inventory, render_repo_inventory_markdown
from kodawari.cli.artifact_versions import ArtifactSchemaVersionError
from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.contract.contract_first_schema import (
    ContractFirstSchemaValidationError,
    load_contract_first_artifact,
    validate_contract_first_payload,
    write_contract_first_artifact,
)
from kodawari.cli.io_atomic import CorruptArtifactError, atomic_write_text
from kodawari.cli.contract.planning_requirements import PlanningStrictnessError, validate_task_plan_requirements
from kodawari.cli.provenance import build_cli_provenance


def _emit(payload: dict[str, Any]) -> int:
    normalized = normalize_mutating_payload(payload)
    print(json.dumps(normalized, ensure_ascii=False, indent=2))
    return int(normalized.get("_rc", 0) or 0)


def _planning_dir(project_root: Path, feature: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return (project_root / "planning" / feature).resolve()


def _provenance(command: str, *, project_root: Path, planning_dir: Path | None = None) -> dict[str, Any]:
    return build_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
    )


def _write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)


def _emit_contract_error(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    error: Exception,
    remediation: list[str],
    extra: dict[str, Any] | None = None,
) -> int:
    error_code = "contract_first_artifact_invalid"
    validation_errors: list[dict[str, str]] = []
    if isinstance(error, ArtifactSchemaVersionError):
        error_code = "artifact_schema_version_invalid"
    elif isinstance(error, ContractFirstSchemaValidationError):
        error_code = "artifact_schema_invalid"
        validation_errors = list(error.errors)
    elif isinstance(error, CorruptArtifactError):
        error_code = "artifact_corrupt"
        if error.quarantine_path is not None:
            remediation = [*remediation, f"Quarantined copy: {error.quarantine_path}"]
    payload = build_error_payload(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
        error=str(error),
        error_code=error_code,
        remediation=remediation,
        next_action=f"Fix the contract-first artifact problem, then rerun `kodawari {command}`.",
        extra={
            "_rc": 2,
            "status": "FAIL",
            "entrypoint": f"kodawari {command}",
            "feature": feature,
            "planning_dir": str(planning_dir),
            **({"validation_errors": validation_errors} if validation_errors else {}),
            **(extra or {}),
        },
    )
    return _emit(payload)


def _load_contract_json(path: Path, *, schema_name: str | None = None) -> dict[str, Any]:
    try:
        return load_contract_first_artifact(path, schema_name=schema_name)
    except ValueError as exc:
        if "required file not found:" in str(exc):
            raise FileNotFoundError(str(exc)) from exc
        raise


def _default_mode(*, project_root: Path, requested: str) -> str:
    if requested in {"existing", "greenfield"}:
        return requested
    code_markers = (
        project_root / "app",
        project_root / "src",
        project_root / "backend",
        project_root / "web",
        project_root / "package.json",
        project_root / "manage.py",
    )
    return "existing" if any(marker.exists() for marker in code_markers) else "greenfield"


def _artifact_path(planning_dir: Path, explicit: str | None, *, name: str) -> Path:
    return Path(explicit).resolve() if explicit else (planning_dir / name)


def _load_optional_architecture_plan(
    *,
    architecture_path: Path,
    archetype: str,
    capabilities: list[str] | None,
    resolved_mode: str,
) -> tuple[dict[str, Any] | None, str, list[str] | None, str]:
    if not architecture_path.exists():
        return None, archetype, capabilities, resolved_mode
    architecture_plan = _load_contract_json(architecture_path, schema_name="architecture_plan")
    if not archetype or archetype == "auto":
        archetype = str(architecture_plan.get("archetype") or "auto")
    if not capabilities:
        capabilities = list(architecture_plan.get("capabilities") or [])
    return architecture_plan, archetype, capabilities, str(architecture_plan.get("planning_mode") or resolved_mode)


def _write_repo_inventory_artifact(
    *,
    repo_path: Path,
    project_root: Path,
    archetype: str,
    capabilities: list[str] | None,
    resolved_mode: str,
) -> dict[str, Any]:
    repo_inventory = build_repo_inventory(
        project_root=project_root,
        archetype=archetype or "auto",
        capabilities=list(capabilities or []),
        mode=resolved_mode,
    )
    validate_contract_first_payload("repo_inventory", repo_inventory)
    write_contract_first_artifact(repo_path, repo_inventory, schema_name="repo_inventory")
    return repo_inventory


def _resolve_task_plan_intake(
    *,
    intake_path: Path,
    architecture_plan: dict[str, Any] | None,
) -> dict[str, Any]:
    if intake_path.exists():
        return _load_contract_json(intake_path, schema_name="prd_intake")
    if architecture_plan is not None:
        return architecture_plan_to_prd_intake(architecture_plan)
    raise FileNotFoundError(f"required file not found: {intake_path}")


def _low_confidence_validation_errors(intake: dict[str, Any]) -> list[str]:
    if str(intake.get("confidence") or "high").lower() != "low":
        return []
    issues = [str(item) for item in list(intake.get("confidence_issues") or []) if str(item).strip()]
    details = issues or ["semantic confidence below threshold"]
    return [f"input PRD intake low confidence: {item}" for item in details]


def _strictness_error_with_intake(
    error: PlanningStrictnessError,
    *,
    intake: dict[str, Any],
) -> PlanningStrictnessError:
    requirements = dict(error.requirements)
    requirements["input_intake_confidence"] = str(intake.get("confidence") or "high")
    requirements["input_confidence_issues"] = [
        str(item) for item in list(intake.get("confidence_issues") or []) if str(item).strip()
    ]
    validation_errors = _low_confidence_validation_errors(intake)
    if validation_errors:
        requirements["validation_errors"] = validation_errors
    return PlanningStrictnessError(
        error_code=error.error_code,
        message=str(error),
        requirements=requirements,
    )


def resolve_task_plan_context(
    *,
    project_root: Path,
    planning_dir: Path,
    intake_path: str | None,
    architecture_plan_path: str | None,
    repo_inventory_path: str | None,
    archetype: str,
    capabilities: list[str] | None,
    mode: str,
) -> dict[str, Any]:
    resolved_mode = _default_mode(project_root=project_root, requested=str(mode or "auto").strip().lower())
    architecture_path = _artifact_path(planning_dir, architecture_plan_path, name="ARCHITECTURE_PLAN.json")
    repo_path = _artifact_path(planning_dir, repo_inventory_path, name="REPO_INVENTORY.json")
    intake_resolved = _artifact_path(planning_dir, intake_path, name="PRD_INTAKE.json")
    architecture_plan, archetype, capabilities, resolved_mode = _load_optional_architecture_plan(
        architecture_path=architecture_path,
        archetype=archetype,
        capabilities=capabilities,
        resolved_mode=resolved_mode,
    )
    repo_inventory = (
        _load_contract_json(repo_path, schema_name="repo_inventory")
        if repo_path.exists()
        else _write_repo_inventory_artifact(
            repo_path=repo_path,
            project_root=project_root,
            archetype=archetype,
            capabilities=capabilities,
            resolved_mode=resolved_mode,
        )
    )
    intake = _resolve_task_plan_intake(
        intake_path=intake_resolved,
        architecture_plan=architecture_plan,
    )
    try:
        requirements = validate_task_plan_requirements(
            repo_inventory=repo_inventory,
            architecture_plan=architecture_plan,
            default_mode=resolved_mode,
        )
    except PlanningStrictnessError as exc:
        raise _strictness_error_with_intake(exc, intake=intake) from exc

    return {
        "planning_mode": resolved_mode,
        "planning_requirements": requirements,
        "intake": intake,
        "architecture_plan": architecture_plan,
        "repo_inventory": repo_inventory,
        "repo_inventory_path": repo_path,
        "architecture_plan_path": architecture_path if architecture_plan is not None else None,
    }


def _resolve_architecture_intake(
    *,
    intake_path: Path,
    prd_path: str | None,
    feature: str,
) -> dict[str, Any]:
    if intake_path.exists():
        return _load_contract_json(intake_path, schema_name="prd_intake")
    if not prd_path:
        return build_prd_intake("", feature=feature)
    raw_text = Path(prd_path).resolve().read_text(encoding="utf-8")
    intake = build_prd_intake(raw_text, feature=feature)
    validate_contract_first_payload("prd_intake", intake)
    write_contract_first_artifact(intake_path, intake, schema_name="prd_intake")
    return intake


def _write_architecture_plan_artifacts(
    *,
    project_root: Path,
    planning_dir: Path,
    intake: dict[str, Any],
    archetype: str,
    capabilities: list[str],
    mode: str,
    output: str | None,
) -> tuple[dict[str, Any], Path, dict[str, Any], Path]:
    repo_inventory_path = planning_dir / "REPO_INVENTORY.json"
    repo_inventory = _write_repo_inventory_artifact(
        repo_path=repo_inventory_path,
        project_root=project_root,
        archetype=archetype,
        capabilities=capabilities,
        resolved_mode=mode,
    )
    plan = build_architecture_plan(
        project_root=project_root,
        prd_intake=intake,
        repo_inventory=repo_inventory,
        archetype=archetype,
        capabilities=capabilities,
        planning_mode=mode,
    )
    validate_contract_first_payload("architecture_plan", plan)
    output_path = Path(output).resolve() if output else (planning_dir / "ARCHITECTURE_PLAN.json")
    write_contract_first_artifact(output_path, plan, schema_name="architecture_plan")
    return plan, output_path, repo_inventory, repo_inventory_path


def _architecture_artifacts_payload(
    *,
    output_path: Path,
    repo_inventory_path: Path,
    intake_path: Path,
) -> dict[str, str]:
    artifacts = {
        "ARCHITECTURE_PLAN.json": str(output_path),
        "REPO_INVENTORY.json": str(repo_inventory_path),
    }
    if intake_path.exists():
        artifacts["PRD_INTAKE.json"] = str(intake_path)
    return artifacts


def _emit_architecture_markdown(
    *,
    plan: dict[str, Any],
    output_path: Path,
    repo_inventory: dict[str, Any],
    repo_inventory_path: Path,
    intake: dict[str, Any],
    intake_path: Path,
) -> dict[str, str]:
    artifacts = {
        "ARCHITECTURE_PLAN.md": str(output_path.with_suffix(".md")),
        "REPO_INVENTORY.md": str(repo_inventory_path.with_suffix(".md")),
    }
    _write_markdown(output_path.with_suffix(".md"), render_architecture_plan_markdown(plan))
    _write_markdown(repo_inventory_path.with_suffix(".md"), render_repo_inventory_markdown(repo_inventory))
    if intake_path.exists():
        intake_md = intake_path.with_suffix(".md")
        _write_markdown(intake_md, render_prd_intake_markdown(intake))
        artifacts["PRD_INTAKE.md"] = str(intake_md)
    return artifacts


def _resolve_architecture_intake_payload(
    *,
    intake_path: Path,
    prd_path: str | None,
    feature: str,
    project_root: Path,
    planning_dir: Path,
) -> dict[str, Any] | int:
    try:
        return _resolve_architecture_intake(
            intake_path=intake_path,
            prd_path=prd_path,
            feature=feature,
        )
    except (FileNotFoundError, ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        remediation = (
            ["Fix PRD intake artifact before rerunning architecture-plan."]
            if intake_path.exists()
            else ["Fix PRD extraction/schema mismatch before rerunning architecture-plan."]
        )
        return _emit_contract_error(
            command="architecture-plan",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=remediation,
        )


def _architecture_success_payload(
    *,
    feature: str,
    planning_dir: Path,
    mode: str,
    plan: dict[str, Any],
    artifacts: dict[str, str],
    project_root: Path,
) -> dict[str, Any]:
    return {
        "_rc": 0 if str(plan.get("confidence") or "high") == "high" else 2,
        "status": "PASS" if str(plan.get("confidence") or "high") == "high" else "FAIL",
        "entrypoint": "kodawari architecture-plan",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "artifacts": artifacts,
        "planning_mode": mode,
        "archetype": plan.get("archetype"),
        "capabilities": plan.get("capabilities"),
        "confidence": plan.get("confidence"),
        "confidence_issues": list(plan.get("confidence_issues") or []),
        "provenance": _provenance("architecture-plan", project_root=project_root, planning_dir=planning_dir),
    }


def _architecture_context(args: argparse.Namespace) -> tuple[Path, str, Path, str, Path]:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    mode = _default_mode(project_root=project_root, requested=str(getattr(args, "mode", "auto") or "auto"))
    intake_path = _artifact_path(planning_dir, getattr(args, "intake", None), name="PRD_INTAKE.json")
    return project_root, feature, planning_dir, mode, intake_path


def _architecture_plan_result(
    *,
    args: argparse.Namespace,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    mode: str,
    intake: dict[str, Any],
    intake_path: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    plan, output_path, repo_inventory, repo_inventory_path = _write_architecture_plan_artifacts(
        project_root=project_root,
        planning_dir=planning_dir,
        intake=intake,
        archetype=str(getattr(args, "archetype", "auto") or "auto"),
        capabilities=list(getattr(args, "capability", []) or []),
        mode=mode,
        output=getattr(args, "output", None),
    )
    artifacts = _architecture_artifacts_payload(
        output_path=output_path,
        repo_inventory_path=repo_inventory_path,
        intake_path=intake_path,
    )
    if bool(getattr(args, "emit_md", False)):
        artifacts.update(
            _emit_architecture_markdown(
                plan=plan,
                output_path=output_path,
                repo_inventory=repo_inventory,
                repo_inventory_path=repo_inventory_path,
                intake=intake,
                intake_path=intake_path,
            )
        )
    return plan, artifacts


def run_architecture_plan_command(args: argparse.Namespace) -> int:
    project_root, feature, planning_dir, mode, intake_path = _architecture_context(args)
    intake = _resolve_architecture_intake_payload(
        intake_path=intake_path,
        prd_path=None if intake_path.exists() else getattr(args, "prd", None),
        feature=feature,
        project_root=project_root,
        planning_dir=planning_dir,
    )
    if isinstance(intake, int):
        return intake

    try:
        plan, artifacts = _architecture_plan_result(
            args=args,
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            mode=mode,
            intake=intake,
            intake_path=intake_path,
        )
    except (ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="architecture-plan",
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            error=exc,
            remediation=["Fix repo inventory / architecture plan schema mismatch before rerunning architecture-plan."],
        )
    return _emit(
        _architecture_success_payload(
            feature=feature,
            planning_dir=planning_dir,
            mode=mode,
            plan=plan,
            artifacts=artifacts,
            project_root=project_root,
        )
    )


def _load_init_architecture_plan(architecture_plan_path: Path | None) -> tuple[dict[str, Any] | None, Path | None]:
    if architecture_plan_path is None:
        return None, None
    return _load_contract_json(architecture_plan_path, schema_name="architecture_plan"), architecture_plan_path.parent


def _refresh_repo_inventory_after_init(
    *,
    planning_dir: Path | None,
    project_root: Path,
    archetype: str,
    capabilities: list[str],
) -> dict[str, Any]:
    if planning_dir is None:
        return {}
    repo_inventory = _write_repo_inventory_artifact(
        repo_path=planning_dir / "REPO_INVENTORY.json",
        project_root=project_root,
        archetype=archetype,
        capabilities=capabilities,
        resolved_mode="greenfield",
    )
    return {"REPO_INVENTORY.json": str(planning_dir / "REPO_INVENTORY.json"), "repo_inventory": repo_inventory}


def _init_inputs(args: argparse.Namespace, plan: dict[str, Any] | None) -> tuple[str, list[str]]:
    archetype = str((plan or {}).get("archetype") or getattr(args, "archetype", "") or "")
    capabilities = list((plan or {}).get("capabilities") or list(getattr(args, "capability", []) or []))
    return archetype, capabilities


def _resolve_init_plan_or_error(
    *,
    args: argparse.Namespace,
    project_root: Path,
    architecture_plan_path: Path | None,
) -> tuple[dict[str, Any] | None, Path | None] | int:
    try:
        return _load_init_architecture_plan(architecture_plan_path)
    except (FileNotFoundError, ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        fallback_dir = architecture_plan_path.parent if architecture_plan_path is not None else project_root
        return _emit_contract_error(
            command="init",
            project_root=project_root,
            planning_dir=fallback_dir,
            feature=str(getattr(args, "feature", "") or ""),
            error=exc,
            remediation=["Fix ARCHITECTURE_PLAN.json before rerunning init."],
        )


def _init_success_payload(
    *,
    project_root: Path,
    scaffold: dict[str, Any],
    artifacts: dict[str, Any],
    planning_dir: Path | None,
) -> dict[str, Any]:
    return {
        "_rc": 0,
        "status": "PASS",
        "entrypoint": "kodawari init",
        "project_root": str(project_root),
        "archetype": scaffold.get("archetype"),
        "capabilities": scaffold.get("capabilities"),
        "artifacts": artifacts,
        "created_files": list(scaffold.get("created_files") or []),
        "skipped_files": list(scaffold.get("skipped_files") or []),
        "provenance": _provenance("init", project_root=project_root, planning_dir=planning_dir),
    }


def _refresh_init_artifacts_or_error(
    *,
    project_root: Path,
    planning_dir: Path | None,
    scaffold: dict[str, Any],
    archetype: str,
    capabilities: list[str],
    feature: str,
) -> dict[str, Any] | int:
    try:
        artifacts = _refresh_repo_inventory_after_init(
            planning_dir=planning_dir,
            project_root=project_root,
            archetype=str(scaffold.get("archetype") or archetype),
            capabilities=list(scaffold.get("capabilities") or capabilities),
        )
    except (ArtifactSchemaVersionError, ContractFirstSchemaValidationError, CorruptArtifactError, ValueError) as exc:
        return _emit_contract_error(
            command="init",
            project_root=project_root,
            planning_dir=planning_dir or project_root,
            feature=feature,
            error=exc,
            remediation=["Fix scaffolded repo inventory before rerunning init."],
        )
    artifacts.pop("repo_inventory", None)
    return artifacts


def run_init_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    architecture_plan_path = Path(args.architecture_plan).resolve() if getattr(args, "architecture_plan", None) else None
    resolved = _resolve_init_plan_or_error(
        args=args,
        project_root=project_root,
        architecture_plan_path=architecture_plan_path,
    )
    if isinstance(resolved, int):
        return resolved
    plan, planning_dir = resolved
    archetype, capabilities = _init_inputs(args, plan)
    try:
        scaffold = scaffold_project(
            project_root=project_root,
            archetype=archetype,
            capabilities=capabilities,
        )
    except ValueError as exc:
        return _emit(
            {
                "_rc": 2,
                "status": "FAIL",
                "entrypoint": "kodawari init",
                "error": str(exc),
                "error_code": "init_invalid_arguments",
                "provenance": _provenance("init", project_root=project_root, planning_dir=None),
            }
        )
    artifacts: dict[str, Any] = {"created_files": list(scaffold.get("created_files") or [])}
    # A3: persist scaffold result so a later planning round (which only sees a
    # near-empty filesystem and would otherwise hit detect_archetype's
    # fastapi_api fallback) can prefer the explicit archetype chosen at init.
    if planning_dir is not None:
        write_scaffold_manifest(planning_dir, scaffold=scaffold, project_root=project_root)
        artifacts[SCAFFOLD_MANIFEST_FILENAME] = str((planning_dir / SCAFFOLD_MANIFEST_FILENAME).resolve())
    refreshed = _refresh_init_artifacts_or_error(
        project_root=project_root,
        planning_dir=planning_dir,
        scaffold=scaffold,
        archetype=archetype,
        capabilities=capabilities,
        feature=str((plan or {}).get("feature") or ""),
    )
    if isinstance(refreshed, int):
        return refreshed
    artifacts.update(refreshed)
    return _emit(
        _init_success_payload(
            project_root=project_root,
            scaffold=scaffold,
            artifacts=artifacts,
            planning_dir=planning_dir,
        )
    )



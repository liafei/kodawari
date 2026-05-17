"""Generic contract-first planning bootstrap path for autopilot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.architecture_plan import build_architecture_plan
from kodawari.autopilot.planning.init_scaffold import (
    SCAFFOLD_MANIFEST_SCHEMA_VERSION,
    read_scaffold_manifest,
    scaffold_project,
)
from kodawari.autopilot.planning.prd_contract import build_prd_intake
from kodawari.autopilot.planning.repo_inventory import build_repo_inventory
from kodawari.autopilot.planning.stage_profiles import EPIC_PLAN, TAKE_TASK
from kodawari.autopilot.planning.task_card import build_task_card, validate_task_card
from kodawari.autopilot.planning.task_graph import build_task_graph, validate_task_graph
from kodawari.cli.contract import bridge_artifacts
from kodawari.cli.contract import task_activation
from kodawari.cli.contract.bridge_types import AutopilotPlanningBridgeError, AutopilotPlanningSnapshot
from kodawari.cli.contract.next_task_selector import TaskSelection
from kodawari.project_model import profile_from_archetype


def ensure_generic_planning(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    prd_path: Path | None,
) -> AutopilotPlanningSnapshot:
    steps_run: list[str] = []
    artifacts: dict[str, str] = {}
    planning_mode = default_mode(project_root)
    intake = ensure_prd_intake(
        planning_dir,
        feature=feature,
        prd_path=prd_path,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    repo_inventory = ensure_repo_inventory(
        planning_dir,
        project_root=project_root,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    architecture_plan = ensure_architecture_plan(
        planning_dir,
        project_root=project_root,
        intake=intake,
        repo_inventory=repo_inventory,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    return planning_snapshot(
        project_root=project_root,
        planning_dir=planning_dir,
        prd_path=prd_path,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        intake=intake,
    )


def planning_snapshot(
    *,
    project_root: Path,
    planning_dir: Path,
    prd_path: Path | None,
    planning_mode: str,
    steps_run: list[str],
    artifacts: dict[str, str],
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
    intake: dict[str, Any],
) -> AutopilotPlanningSnapshot:
    resolved_mode, archetype, capabilities = resolved_architecture_context(
        planning_mode=planning_mode,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
    )
    refreshed_inventory = maybe_init_greenfield(
        project_root=project_root,
        planning_dir=planning_dir,
        planning_mode=resolved_mode,
        archetype=archetype,
        capabilities=capabilities,
        repo_inventory=repo_inventory,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    task_id, task_card = ensure_task_graph_and_card(
        project_root=project_root,
        planning_dir=planning_dir,
        intake=intake,
        repo_inventory=refreshed_inventory,
        architecture_plan=architecture_plan,
        planning_mode=resolved_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    task_label, task_scope = task_runtime(task_card)
    generated_graph = "task-plan" in steps_run
    return AutopilotPlanningSnapshot(
        planning_mode=resolved_mode,
        archetype=archetype,
        capabilities=tuple(capabilities),
        primary_task_id=task_id,
        task_label=task_label,
        task_scope=task_scope,
        task_card_path=(planning_dir / "TASK_CARD_ACTIVE.json").resolve(),
        prd_path=prd_path.resolve() if prd_path is not None else None,
        steps_run=tuple(steps_run),
        artifacts=dict(artifacts),
        stage_profile=EPIC_PLAN.profile_id if generated_graph else TAKE_TASK.profile_id,
        selection_action="take_task",
        selection_reason="generated task graph" if generated_graph else "reused existing task graph",
        planning_source_status="generic",
    )


def ensure_prd_intake(
    planning_dir: Path,
    *,
    feature: str,
    prd_path: Path | None,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    path = planning_dir / "PRD_INTAKE.json"
    payload = bridge_artifacts.load_optional_contract_json(path, schema_name="prd_intake")
    if payload is None:
        payload = build_prd_intake_artifact(path, feature=feature, prd_path=prd_path)
        steps_run.append("prd-intake")
    artifacts[path.name] = str(path.resolve())
    return payload


def build_prd_intake_artifact(path: Path, *, feature: str, prd_path: Path | None) -> dict[str, Any]:
    if prd_path is None:
        raise AutopilotPlanningBridgeError(
            error_code="prd_required",
            message="Contract-first planning truth is missing and autopilot did not receive a PRD input.",
            remediation=["Provide `--prd <path>` when running autopilot from an empty planning state."],
        )
    payload = build_prd_intake(prd_path.read_text(encoding="utf-8"), feature=feature)
    bridge_artifacts.write_contract_artifact(path, payload, schema_name="prd_intake")
    return payload


def ensure_repo_inventory(
    planning_dir: Path,
    *,
    project_root: Path,
    planning_mode: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    path = planning_dir / "REPO_INVENTORY.json"
    payload = bridge_artifacts.load_optional_contract_json(path, schema_name="repo_inventory")
    if payload is None:
        # A3: when greenfield and a SCAFFOLD_MANIFEST exists (i.e. kodawari
        # init has already run with an explicit archetype), prefer those values
        # over the "auto" default — otherwise detect_archetype's empty-dir
        # fallback locks the project to fastapi_api regardless of what the user
        # asked for at init time.
        archetype, capabilities = _scaffold_archetype_hint(
            planning_dir=planning_dir,
            planning_mode=planning_mode,
        )
        payload = write_repo_inventory(
            path,
            project_root=project_root,
            archetype=archetype,
            capabilities=capabilities,
            planning_mode=planning_mode,
        )
        steps_run.append("repo-inventory")
    artifacts[path.name] = str(path.resolve())
    return payload


def _scaffold_archetype_hint(
    *,
    planning_dir: Path,
    planning_mode: str,
) -> tuple[str, list[str]]:
    """Return (archetype, capabilities) hint from SCAFFOLD_MANIFEST.json when
    greenfield mode is active and the manifest is present and well-formed.

    Falls back to ("auto", []) when:
    - planning_mode is not greenfield (existing projects keep auto-detect)
    - manifest is absent (no init has run yet)
    - manifest has unrecognized schema_version (forward-compat: log via WARN
      semantics by silently degrading; consumer will fall back to auto)
    - manifest archetype is blank
    """
    if str(planning_mode or "").strip().lower() != "greenfield":
        return "auto", []
    manifest = read_scaffold_manifest(planning_dir)
    if not manifest:
        return "auto", []
    if str(manifest.get("schema_version") or "") != SCAFFOLD_MANIFEST_SCHEMA_VERSION:
        return "auto", []
    archetype = str(manifest.get("archetype") or "").strip()
    if not archetype:
        return "auto", []
    capabilities = [str(item) for item in list(manifest.get("capabilities") or []) if str(item).strip()]
    return archetype, capabilities


def ensure_architecture_plan(
    planning_dir: Path,
    *,
    project_root: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    planning_mode: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    path = planning_dir / "ARCHITECTURE_PLAN.json"
    payload = bridge_artifacts.load_optional_contract_json(path, schema_name="architecture_plan")
    if payload is None:
        payload = write_architecture_plan(
            path,
            project_root=project_root,
            intake=intake,
            repo_inventory=repo_inventory,
            planning_mode=planning_mode,
        )
        steps_run.append("architecture-plan")
    artifacts[path.name] = str(path.resolve())
    return payload


def resolved_architecture_context(
    *,
    planning_mode: str,
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
) -> tuple[str, str, list[str]]:
    resolved_mode = str(architecture_plan.get("planning_mode") or planning_mode or "existing")
    archetype = str(architecture_plan.get("archetype") or repo_inventory.get("archetype") or "auto")
    capabilities = string_list(
        architecture_plan.get("capabilities") or repo_inventory.get("capabilities") or [],
    )
    return resolved_mode, archetype, capabilities


def maybe_init_greenfield(
    *,
    project_root: Path,
    planning_dir: Path,
    planning_mode: str,
    archetype: str,
    capabilities: list[str],
    repo_inventory: dict[str, Any],
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    if not should_run_init(project_root=project_root, planning_mode=planning_mode):
        return repo_inventory
    scaffold_project(project_root=project_root, archetype=archetype, capabilities=capabilities)
    steps_run.append("init")
    refreshed = write_repo_inventory(
        planning_dir / "REPO_INVENTORY.json",
        project_root=project_root,
        archetype=archetype,
        capabilities=capabilities,
        planning_mode=planning_mode,
    )
    artifacts["REPO_INVENTORY.json"] = str((planning_dir / "REPO_INVENTORY.json").resolve())
    return refreshed


def ensure_task_graph_and_card(
    *,
    project_root: Path,
    planning_dir: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
    planning_mode: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    task_graph = ensure_task_graph(
        planning_dir,
        project_root=project_root,
        intake=intake,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    return ensure_task_card(planning_dir, task_graph=task_graph, steps_run=steps_run, artifacts=artifacts)


def ensure_task_graph(
    planning_dir: Path,
    *,
    project_root: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
    planning_mode: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    path = planning_dir / "TASK_GRAPH.json"
    payload = bridge_artifacts.load_optional_contract_json(path, schema_name="task_graph")
    if payload is None:
        payload = write_task_graph(
            path,
            project_root=project_root,
            intake=intake,
            repo_inventory=repo_inventory,
            architecture_plan=architecture_plan,
            planning_mode=planning_mode,
        )
        steps_run.append("task-plan")
    artifacts[path.name] = str(path.resolve())
    return payload


def ensure_task_card(
    planning_dir: Path,
    *,
    task_graph: dict[str, Any],
    steps_run: list[str],
    artifacts: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    selection = select_task_or_raise(planning_dir, task_graph)
    task_id = selection.task_id
    payload = activate_selected_task_card(
        planning_dir,
        task_graph=task_graph,
        task_id=task_id,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    return task_id, payload


def write_repo_inventory(
    path: Path,
    *,
    project_root: Path,
    archetype: str,
    capabilities: list[str],
    planning_mode: str,
) -> dict[str, Any]:
    payload = build_repo_inventory(
        project_root=project_root,
        archetype=archetype,
        capabilities=capabilities,
        mode=planning_mode,
    )
    bridge_artifacts.write_contract_artifact(path, payload, schema_name="repo_inventory")
    return payload


def write_architecture_plan(
    path: Path,
    *,
    project_root: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    planning_mode: str,
) -> dict[str, Any]:
    payload = build_architecture_plan(
        project_root=project_root,
        prd_intake=intake,
        repo_inventory=repo_inventory,
        archetype=str(repo_inventory.get("archetype") or "auto"),
        capabilities=string_list(repo_inventory.get("capabilities")),
        planning_mode=planning_mode,
    )
    bridge_artifacts.write_contract_artifact(path, payload, schema_name="architecture_plan")
    return payload


def write_task_graph(
    path: Path,
    *,
    project_root: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
    planning_mode: str,
) -> dict[str, Any]:
    payload = build_task_graph(
        intake,
        project_root=project_root,
        project_profile=profile_from_archetype(
            str(architecture_plan.get("archetype") or repo_inventory.get("archetype") or "auto"),
        ),
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        planning_mode=planning_mode,
    )
    errors = validate_task_graph(payload)
    if errors:
        raise AutopilotPlanningBridgeError(
            error_code="task_graph_invalid",
            message=errors[0],
            remediation=["Refine the architecture plan or PRD intake before rerunning autopilot."],
            details={"validation_errors": list(errors)},
        )
    bridge_artifacts.write_contract_artifact(path, payload, schema_name="task_graph")
    return payload


def write_task_card(
    planning_dir: Path,
    *,
    task_graph: dict[str, Any],
    task_id: str,
) -> dict[str, Any]:
    payload = build_task_card(task_graph, task_id)
    errors = validate_task_card(
        payload,
        planning_mode=str(task_graph.get("planning_mode") or "existing"),
    )
    if errors:
        raise AutopilotPlanningBridgeError(
            error_code="task_card_invalid",
            message=errors[0],
            remediation=["Fix task executability or narrow the task scope before rerunning autopilot."],
            details={"validation_errors": list(errors), "task_id": task_id},
        )
    named_path = planning_dir / f"TASK_CARD_{task_id}.json"
    active_path = planning_dir / "TASK_CARD_ACTIVE.json"
    bridge_artifacts.write_contract_artifact(named_path, payload, schema_name="task_card")
    bridge_artifacts.write_contract_artifact(active_path, payload, schema_name="task_card")
    return payload


def select_task_or_raise(planning_dir: Path, task_graph: dict[str, Any]) -> TaskSelection:
    return task_activation.select_task_or_raise(planning_dir, task_graph)


def activate_selected_task_card(
    planning_dir: Path,
    *,
    task_graph: dict[str, Any],
    task_id: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return task_activation.activate_selected_task_card(
        planning_dir,
        task_graph=task_graph,
        task_id=task_id,
        steps_run=steps_run,
        artifacts=artifacts,
        load_active_card=lambda path: bridge_artifacts.load_optional_contract_json(path, schema_name="task_card"),
        write_task_card=lambda selected_task_id: write_task_card(
            planning_dir,
            task_graph=task_graph,
            task_id=selected_task_id,
        ),
    )


def task_card_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return task_activation.task_card_semantic_payload(payload)


def select_primary_task_id(task_graph: dict[str, Any]) -> str:
    return task_activation.select_primary_task_id(task_graph)


def task_runtime(task_card: dict[str, Any]) -> tuple[str, str]:
    return task_activation.task_runtime(task_card)


def default_mode(project_root: Path) -> str:
    code_markers = (
        project_root / "app",
        project_root / "src",
        project_root / "backend",
        project_root / "web",
        project_root / "package.json",
        project_root / "manage.py",
    )
    return "existing" if any(marker.exists() for marker in code_markers) else "greenfield"


def should_run_init(*, project_root: Path, planning_mode: str) -> bool:
    if str(planning_mode or "").strip().lower() != "greenfield":
        return False
    markers = (
        project_root / "app",
        project_root / "src",
        project_root / "backend",
        project_root / "web",
        project_root / "package.json",
        project_root / "manage.py",
    )
    return not any(marker.exists() for marker in markers)


def string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]

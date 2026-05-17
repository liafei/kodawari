"""Bridge autopilot entrypoint onto contract-first planning truth."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.planning_orchestrator import PlanningConfig, run_planning_conversation
from kodawari.cli.contract import bridge_artifacts as _bridge_artifacts
from kodawari.cli.contract import generic_bootstrap as _generic_bootstrap
from kodawari.cli.contract import model_bootstrap as _model_bootstrap
from kodawari.cli.contract import planner_config_env as _planner_config_env
from kodawari.cli.contract import planning_source as _planning_source
from kodawari.cli.contract import planning_telemetry as _planning_telemetry
from kodawari.cli.contract.bridge_types import (
    AutopilotPlanningBridgeError,
    AutopilotPlanningSnapshot,
)
from kodawari.cli.contract.next_task_selector import PlanningAction, TaskSelection


CONTRACT_FIRST_AUTOPILOT_ARTIFACTS = (
    "PLANNING_CONVERSATION.json",
    "PRD_INTAKE.json",
    "REPO_INVENTORY.json",
    "ARCHITECTURE_PLAN.json",
    "TASK_GRAPH.json",
    "TASK_CARD_ACTIVE.json",
)


def resolve_autopilot_prd_path(args: Any) -> Path | None:
    raw = str(getattr(args, "prd", None) or getattr(args, "requirements_file", None) or "").strip()
    if not raw:
        return None
    candidate = Path(raw).resolve()
    if candidate.exists() and candidate.is_file():
        return candidate
    raise AutopilotPlanningBridgeError(
        error_code="prd_missing",
        message=f"PRD file not found: {candidate}",
        remediation=["Provide `--prd <path>` or a valid `--requirements-file <path>` before rerunning autopilot."],
    )


def ensure_contract_first_planning(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    prd_path: Path | None,
    task_direction: str = "",
    use_model_planning: bool = False,
    force_replan: bool = False,
) -> AutopilotPlanningSnapshot:
    planning_dir.mkdir(parents=True, exist_ok=True)
    if use_model_planning:
        snapshot = _ensure_model_driven_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            prd_path=prd_path,
            task_direction=task_direction,
            force_replan=force_replan,
        )
        route = "model"
    else:
        snapshot = _generic_bootstrap.ensure_generic_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            prd_path=prd_path,
        )
        route = "generic"
    _append_planning_telemetry(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        snapshot=snapshot,
        route=route,
        force_replan=force_replan,
    )
    return snapshot


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _env_text(name: str, default: str = "") -> str:
    return _planner_config_env.env_text(name, default)


def _env_int(name: str, default: int) -> int:
    return _planner_config_env.env_int(name, default)


def _env_optional_int(name: str) -> int | None:
    return _planner_config_env.env_optional_int(name)


def _env_bool(name: str, default: bool = False) -> bool:
    return _planner_config_env.env_bool(name, default)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _planning_action_value(value: Any) -> str:
    return str(value.value if isinstance(value, PlanningAction) else value or "").strip()


def _safe_planning_role_models(project_root: Path) -> dict[str, str]:
    return _planning_telemetry.safe_planning_role_models(project_root)


def _append_planning_telemetry(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    snapshot: AutopilotPlanningSnapshot,
    route: str,
    force_replan: bool,
) -> None:
    _planning_telemetry.append_planning_telemetry(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        snapshot=snapshot,
        route=route,
        force_replan=force_replan,
    )


def _sha256_text(value: str) -> str:
    return _planning_source.sha256_text(value)


def _sha256_file(path: Path | None) -> str:
    return _planning_source.sha256_file(path)


def _planning_source_contract(
    *,
    feature: str,
    prd_path: Path | None,
    task_direction: str,
) -> dict[str, Any]:
    return _planning_source.planning_source_contract(
        feature=feature,
        prd_path=prd_path,
        task_direction=task_direction,
    )


def _attach_planning_source(task_graph: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    return _planning_source.attach_planning_source(task_graph, source)


def _planning_source_status(
    task_graph: dict[str, Any],
    current_source: dict[str, Any],
) -> tuple[bool, str, dict[str, str]]:
    return _planning_source.planning_source_status(task_graph, current_source)


def _task_direction_from_prd(prd_path: Path | None) -> str:
    return _planning_source.task_direction_from_prd(prd_path)


def _env_blocking_severities() -> frozenset[str] | None:
    return _planner_config_env.env_blocking_severities()


def _planning_candidate_roots(project_root: Path, repo_inventory: dict[str, Any] | None) -> list[Path]:
    return _planner_config_env.planning_candidate_roots(project_root, repo_inventory)


def _planning_candidate_files(project_root: Path, repo_inventory: dict[str, Any] | None) -> list[str]:
    return _planner_config_env.planning_candidate_files(project_root, repo_inventory)


def _planning_candidate_line_counts(project_root: Path, candidate_files: list[str]) -> dict[str, int]:
    return _planner_config_env.planning_candidate_line_counts(project_root, candidate_files)


def _suggest_max_rounds(
    *,
    project_root: Path | None,
    task_direction: str,
    repo_inventory: dict[str, Any] | None,
) -> int:
    return _planner_config_env.suggest_max_rounds(
        project_root=project_root,
        task_direction=task_direction,
        repo_inventory=repo_inventory,
    )


def _planning_config_from_env(
    project_root: Path | None = None,
    *,
    task_direction: str = "",
    repo_inventory: dict[str, Any] | None = None,
) -> PlanningConfig:
    return _planner_config_env.planning_config_from_env(
        project_root,
        task_direction=task_direction,
        repo_inventory=repo_inventory,
        suggest_max_rounds_fn=_suggest_max_rounds,
        env_blocking_severities_fn=_env_blocking_severities,
    )


def _ensure_model_driven_planning(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    prd_path: Path | None,
    task_direction: str,
    force_replan: bool,
) -> AutopilotPlanningSnapshot:
    return _model_bootstrap.ensure_model_driven_planning(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        prd_path=prd_path,
        task_direction=task_direction,
        force_replan=force_replan,
        run_planning_conversation_fn=run_planning_conversation,
        planning_config_from_env_fn=_planning_config_from_env,
        raise_if_context_scout_awaiting_decision_fn=_raise_if_context_scout_awaiting_decision,
    )


def _planning_snapshot(
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
    return _generic_bootstrap.planning_snapshot(
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


def _ensure_prd_intake(
    planning_dir: Path,
    *,
    feature: str,
    prd_path: Path | None,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return _generic_bootstrap.ensure_prd_intake(
        planning_dir,
        feature=feature,
        prd_path=prd_path,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _build_prd_intake(path: Path, *, feature: str, prd_path: Path | None) -> dict[str, Any]:
    return _generic_bootstrap.build_prd_intake_artifact(path, feature=feature, prd_path=prd_path)


def _ensure_repo_inventory(
    planning_dir: Path,
    *,
    project_root: Path,
    planning_mode: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return _generic_bootstrap.ensure_repo_inventory(
        planning_dir,
        project_root=project_root,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _ensure_architecture_plan(
    planning_dir: Path,
    *,
    project_root: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    planning_mode: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return _generic_bootstrap.ensure_architecture_plan(
        planning_dir,
        project_root=project_root,
        intake=intake,
        repo_inventory=repo_inventory,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _resolved_architecture_context(
    *,
    planning_mode: str,
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
) -> tuple[str, str, list[str]]:
    return _generic_bootstrap.resolved_architecture_context(
        planning_mode=planning_mode,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
    )


def _maybe_init_greenfield(
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
    return _generic_bootstrap.maybe_init_greenfield(
        project_root=project_root,
        planning_dir=planning_dir,
        planning_mode=planning_mode,
        archetype=archetype,
        capabilities=capabilities,
        repo_inventory=repo_inventory,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _ensure_task_graph_and_card(
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
    return _generic_bootstrap.ensure_task_graph_and_card(
        project_root=project_root,
        planning_dir=planning_dir,
        intake=intake,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _ensure_task_graph(
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
    return _generic_bootstrap.ensure_task_graph(
        planning_dir,
        project_root=project_root,
        intake=intake,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _ensure_task_card(
    planning_dir: Path,
    *,
    task_graph: dict[str, Any],
    steps_run: list[str],
    artifacts: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    return _generic_bootstrap.ensure_task_card(
        planning_dir,
        task_graph=task_graph,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _write_repo_inventory(
    path: Path,
    *,
    project_root: Path,
    archetype: str,
    capabilities: list[str],
    planning_mode: str,
) -> dict[str, Any]:
    return _generic_bootstrap.write_repo_inventory(
        path,
        project_root=project_root,
        archetype=archetype,
        capabilities=capabilities,
        planning_mode=planning_mode,
    )


def _write_architecture_plan(
    path: Path,
    *,
    project_root: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    planning_mode: str,
) -> dict[str, Any]:
    return _generic_bootstrap.write_architecture_plan(
        path,
        project_root=project_root,
        intake=intake,
        repo_inventory=repo_inventory,
        planning_mode=planning_mode,
    )


def _write_task_graph(
    path: Path,
    *,
    project_root: Path,
    intake: dict[str, Any],
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
    planning_mode: str,
) -> dict[str, Any]:
    return _generic_bootstrap.write_task_graph(
        path,
        project_root=project_root,
        intake=intake,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        planning_mode=planning_mode,
    )


def _write_task_card(
    planning_dir: Path,
    *,
    task_graph: dict[str, Any],
    task_id: str,
) -> dict[str, Any]:
    return _generic_bootstrap.write_task_card(planning_dir, task_graph=task_graph, task_id=task_id)


def _load_optional_contract_json(path: Path, *, schema_name: str) -> dict[str, Any] | None:
    return _bridge_artifacts.load_optional_contract_json(path, schema_name=schema_name)


def _write_contract_artifact(path: Path, payload: dict[str, Any], *, schema_name: str) -> None:
    _bridge_artifacts.write_contract_artifact(path, payload, schema_name=schema_name)


def _context_scout_decision(conversation_payload: dict[str, Any]) -> dict[str, Any]:
    scout = conversation_payload.get("context_scout")
    if not isinstance(scout, dict):
        return {}
    decision = scout.get("decision")
    return dict(decision) if isinstance(decision, dict) else {}


def _raise_if_context_scout_awaiting_decision(conversation_payload: dict[str, Any]) -> None:
    decision = _context_scout_decision(conversation_payload)
    if str(decision.get("status") or "").strip().upper() != "AWAITING_USER_DECISION":
        return
    raise AutopilotPlanningBridgeError(
        error_code="context_scout_user_decision_required",
        message="Context Scout requires a user decision before executor startup.",
        remediation=[
            "Confirm the recommended scout tier, name the exact files to inspect, or set WORKFLOW_CONTEXT_SCOUT_DEFAULTS=auto for headless runs.",
        ],
        details={
            "selected_tier": str(decision.get("selected_tier") or ""),
            "prompt": str(decision.get("prompt") or ""),
        },
    )


def _reuse_existing_task_graph_snapshot(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    prd_path: Path | None,
    planning_mode: str,
    repo_inventory: dict[str, Any],
    existing_conversation: dict[str, Any] | None,
    current_source: dict[str, Any],
    steps_run: list[str],
    artifacts: dict[str, str],
) -> AutopilotPlanningSnapshot | None:
    return _model_bootstrap.reuse_existing_task_graph_snapshot(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        prd_path=prd_path,
        planning_mode=planning_mode,
        repo_inventory=repo_inventory,
        existing_conversation=existing_conversation,
        current_source=current_source,
        steps_run=steps_run,
        artifacts=artifacts,
        raise_if_context_scout_awaiting_decision_fn=_raise_if_context_scout_awaiting_decision,
    )


def _select_task_or_raise(planning_dir: Path, task_graph: dict[str, Any]) -> TaskSelection:
    return _generic_bootstrap.select_task_or_raise(planning_dir, task_graph)


def _activate_selected_task_card(
    planning_dir: Path,
    *,
    task_graph: dict[str, Any],
    task_id: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    return _generic_bootstrap.activate_selected_task_card(
        planning_dir,
        task_graph=task_graph,
        task_id=task_id,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _task_card_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _generic_bootstrap.task_card_semantic_payload(payload)


def _select_primary_task_id(task_graph: dict[str, Any]) -> str:
    return _generic_bootstrap.select_primary_task_id(task_graph)


def _task_runtime(task_card: dict[str, Any]) -> tuple[str, str]:
    return _generic_bootstrap.task_runtime(task_card)


def _default_mode(project_root: Path) -> str:
    return _generic_bootstrap.default_mode(project_root)


def _should_run_init(*, project_root: Path, planning_mode: str) -> bool:
    return _generic_bootstrap.should_run_init(project_root=project_root, planning_mode=planning_mode)


def _string_list(values: Any) -> list[str]:
    return _generic_bootstrap.string_list(values)


__all__ = [
    "AutopilotPlanningBridgeError",
    "AutopilotPlanningSnapshot",
    "CONTRACT_FIRST_AUTOPILOT_ARTIFACTS",
    "ensure_contract_first_planning",
    "resolve_autopilot_prd_path",
]

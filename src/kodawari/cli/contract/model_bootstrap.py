"""Model-driven contract-first planning bootstrap path for autopilot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

from kodawari.autopilot.planning.planning_orchestrator import (
    PLANNING_FAILURE_FILENAME,
    PLANNING_PROGRESS_SCHEMA_VERSION,
    plan_to_task_cards,
    plan_to_task_graph,
    result_to_artifact,
)
from kodawari.autopilot.planning.task_splitter import split_plan
from kodawari.autopilot.planning.stage_profiles import EPIC_PLAN, TAKE_TASK
from kodawari.autopilot.planning.task_card import validate_task_card
from kodawari.autopilot.planning.task_graph import validate_task_graph
from kodawari.cli.contract import bridge_artifacts
from kodawari.cli.contract import generic_bootstrap
from kodawari.cli.contract import planning_source
from kodawari.cli.contract.bridge_types import AutopilotPlanningBridgeError, AutopilotPlanningSnapshot

RunPlanningConversation = Callable[..., Any]
PlanningConfigFactory = Callable[..., Any]
ContextScoutGuard = Callable[[dict[str, Any]], None]


@dataclass
class _PlanningInputs:
    planning_mode: str
    refreshed_inventory: dict[str, Any]
    resolved_task_direction: str
    current_source: dict[str, Any]
    existing_conversation: dict[str, Any] | None
    conversation_path: Path


def ensure_model_driven_planning(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    prd_path: Path | None,
    task_direction: str,
    force_replan: bool,
    run_planning_conversation_fn: RunPlanningConversation,
    planning_config_from_env_fn: PlanningConfigFactory,
    raise_if_context_scout_awaiting_decision_fn: ContextScoutGuard,
) -> AutopilotPlanningSnapshot:
    steps_run: list[str] = []
    artifacts: dict[str, str] = {}
    inputs = _resolve_planning_inputs(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        prd_path=prd_path,
        task_direction=task_direction,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    if not force_replan:
        reused = reuse_existing_task_graph_snapshot(
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            prd_path=prd_path,
            planning_mode=inputs.planning_mode,
            repo_inventory=inputs.refreshed_inventory,
            existing_conversation=inputs.existing_conversation,
            current_source=inputs.current_source,
            steps_run=steps_run,
            artifacts=artifacts,
            raise_if_context_scout_awaiting_decision_fn=raise_if_context_scout_awaiting_decision_fn,
        )
        if reused is not None:
            return reused
    if _can_reuse_existing_conversation(inputs, task_direction=task_direction, prd_path=prd_path):
        return _bootstrap_from_existing_conversation(
            inputs=inputs,
            feature=feature,
            planning_dir=planning_dir,
            project_root=project_root,
            steps_run=steps_run,
            artifacts=artifacts,
            raise_if_context_scout_awaiting_decision_fn=raise_if_context_scout_awaiting_decision_fn,
        )
    if not inputs.resolved_task_direction:
        raise AutopilotPlanningBridgeError(
            error_code="planning_input_required",
            message="Model-driven planning requires --task or --prd input.",
            remediation=["Provide `--task <text>` or `--prd <path>` before rerunning autopilot."],
        )
    return _bootstrap_from_fresh_plan(
        inputs=inputs,
        feature=feature,
        prd_path=prd_path,
        planning_dir=planning_dir,
        project_root=project_root,
        steps_run=steps_run,
        artifacts=artifacts,
        run_planning_conversation_fn=run_planning_conversation_fn,
        planning_config_from_env_fn=planning_config_from_env_fn,
        raise_if_context_scout_awaiting_decision_fn=raise_if_context_scout_awaiting_decision_fn,
    )


def _resolve_planning_inputs(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    prd_path: Path | None,
    task_direction: str,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> _PlanningInputs:
    planning_mode = generic_bootstrap.default_mode(project_root)
    repo_inventory = generic_bootstrap.ensure_repo_inventory(
        planning_dir,
        project_root=project_root,
        planning_mode=planning_mode,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    refreshed_inventory = generic_bootstrap.maybe_init_greenfield(
        project_root=project_root,
        planning_dir=planning_dir,
        planning_mode=planning_mode,
        archetype=str(repo_inventory.get("archetype") or "auto"),
        capabilities=generic_bootstrap.string_list(repo_inventory.get("capabilities")),
        repo_inventory=repo_inventory,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    conversation_path = planning_dir / "PLANNING_CONVERSATION.json"
    existing_conversation = bridge_artifacts.load_optional_contract_json(
        conversation_path,
        schema_name="planning_conversation",
    )
    resolved_task_direction = _resolved_task_direction(
        task_direction=task_direction,
        existing_conversation=existing_conversation,
        prd_path=prd_path,
        feature=feature,
    )
    return _PlanningInputs(
        planning_mode=planning_mode,
        refreshed_inventory=refreshed_inventory,
        resolved_task_direction=resolved_task_direction,
        current_source=planning_source.planning_source_contract(
            feature=feature,
            prd_path=prd_path,
            task_direction=resolved_task_direction,
        ),
        existing_conversation=existing_conversation,
        conversation_path=conversation_path,
    )


def _resolved_task_direction(
    *,
    task_direction: str,
    existing_conversation: dict[str, Any] | None,
    prd_path: Path | None,
    feature: str,
) -> str:
    return (
        _clean_text(task_direction)
        or str(dict(existing_conversation or {}).get("task_direction") or "").strip()
        or planning_source.task_direction_from_prd(prd_path)
        or f"Implement feature {feature}"
    )


def _can_reuse_existing_conversation(
    inputs: _PlanningInputs,
    *,
    task_direction: str,
    prd_path: Path | None,
) -> bool:
    return inputs.existing_conversation is not None and not _clean_text(task_direction) and prd_path is None


def _bootstrap_from_existing_conversation(
    *,
    inputs: _PlanningInputs,
    feature: str,
    planning_dir: Path,
    project_root: Path,
    steps_run: list[str],
    artifacts: dict[str, str],
    raise_if_context_scout_awaiting_decision_fn: ContextScoutGuard,
) -> AutopilotPlanningSnapshot:
    existing_conversation = dict(inputs.existing_conversation or {})
    artifacts[inputs.conversation_path.name] = str(inputs.conversation_path.resolve())
    raise_if_context_scout_awaiting_decision_fn(existing_conversation)
    task_graph_payload = _task_graph_from_existing_conversation(
        inputs=inputs,
        feature=feature,
        planning_dir=planning_dir,
        project_root=project_root,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    primary_task_id, active_card = generic_bootstrap.ensure_task_card(
        planning_dir,
        task_graph=task_graph_payload,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    return _snapshot_from_existing_conversation(
        inputs=inputs,
        planning_dir=planning_dir,
        primary_task_id=primary_task_id,
        active_card=active_card,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _task_graph_from_existing_conversation(
    *,
    inputs: _PlanningInputs,
    feature: str,
    planning_dir: Path,
    project_root: Path,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    task_graph_path = planning_dir / "TASK_GRAPH.json"
    task_graph_payload = bridge_artifacts.load_optional_contract_json(task_graph_path, schema_name="task_graph")
    if task_graph_payload is None:
        task_graph_payload = _build_task_graph_from_existing_conversation(
            inputs=inputs,
            feature=feature,
            project_root=project_root,
        )
        bridge_artifacts.write_contract_artifact(task_graph_path, task_graph_payload, schema_name="task_graph")
        steps_run.append("task-plan")
    artifacts[task_graph_path.name] = str(task_graph_path.resolve())
    return task_graph_payload


def _build_task_graph_from_existing_conversation(
    *,
    inputs: _PlanningInputs,
    feature: str,
    project_root: Path,
) -> dict[str, Any]:
    final_plan = dict(dict(inputs.existing_conversation or {}).get("final_plan") or {})
    if not list(final_plan.get("tasks") or []):
        raise AutopilotPlanningBridgeError(
            error_code="planning_conversation_invalid",
            message="PLANNING_CONVERSATION.json is missing final_plan.tasks.",
            remediation=["Regenerate planning artifacts by running autopilot with --task or --prd."],
        )
    splitter_enabled = os.environ.get("WORKFLOW_TASK_SPLITTER", "").lower() in {"1", "true", "yes", "on"}
    task_graph_payload = plan_to_task_graph(
        final_plan,
        feature=feature,
        repo_inventory=inputs.refreshed_inventory,
        project_root=project_root,
        splitter_enabled=splitter_enabled,
    )
    _raise_if_graph_errors(
        task_graph_payload,
        remediation=["Regenerate planning artifacts by running autopilot with --task or --prd."],
    )
    return planning_source.attach_planning_source(task_graph_payload, inputs.current_source)


def _snapshot_from_existing_conversation(
    *,
    inputs: _PlanningInputs,
    planning_dir: Path,
    primary_task_id: str,
    active_card: dict[str, Any],
    steps_run: list[str],
    artifacts: dict[str, str],
) -> AutopilotPlanningSnapshot:
    existing_conversation = dict(inputs.existing_conversation or {})
    task_label, task_scope = generic_bootstrap.task_runtime(active_card)
    approval = dict(existing_conversation.get("approval") or {})
    active_scope_view = dict(approval.get("active_scope_view") or {})
    return AutopilotPlanningSnapshot(
        planning_mode=inputs.planning_mode,
        archetype=_existing_conversation_archetype(existing_conversation, inputs.refreshed_inventory),
        capabilities=_existing_conversation_capabilities(existing_conversation, inputs.refreshed_inventory),
        primary_task_id=primary_task_id,
        task_label=task_label,
        task_scope=task_scope,
        task_card_path=(planning_dir / "TASK_CARD_ACTIVE.json").resolve(),
        prd_path=None,
        steps_run=tuple(steps_run),
        artifacts=dict(artifacts),
        planning_status=str(existing_conversation.get("status") or ""),
        planning_approval_decision=str(approval.get("decision") or ""),
        planning_approval_reason=str(approval.get("reason") or ""),
        planning_approval_active_scope_decision=str(active_scope_view.get("decision") or ""),
        input_fingerprint=str(existing_conversation.get("input_fingerprint") or ""),
        task_direction=str(existing_conversation.get("task_direction") or ""),
        stage_profile=TAKE_TASK.profile_id,
        selection_action="take_task",
        selection_reason="reused existing planning conversation",
        planning_source_status="legacy_conversation",
    )


def _existing_conversation_archetype(
    existing_conversation: dict[str, Any],
    refreshed_inventory: dict[str, Any],
) -> str:
    return str(existing_conversation.get("archetype") or refreshed_inventory.get("archetype") or "auto")


def _existing_conversation_capabilities(
    existing_conversation: dict[str, Any],
    refreshed_inventory: dict[str, Any],
) -> tuple[str, ...]:
    return tuple(
        generic_bootstrap.string_list(existing_conversation.get("capabilities"))
        or generic_bootstrap.string_list(refreshed_inventory.get("capabilities"))
    )


def _bootstrap_from_fresh_plan(
    *,
    inputs: _PlanningInputs,
    feature: str,
    prd_path: Path | None,
    planning_dir: Path,
    project_root: Path,
    steps_run: list[str],
    artifacts: dict[str, str],
    run_planning_conversation_fn: RunPlanningConversation,
    planning_config_from_env_fn: PlanningConfigFactory,
    raise_if_context_scout_awaiting_decision_fn: ContextScoutGuard,
) -> AutopilotPlanningSnapshot:
    try:
        planning_result = _run_fresh_planning_conversation(
            inputs=inputs,
            feature=feature,
            prd_path=prd_path,
            planning_dir=planning_dir,
            project_root=project_root,
            run_planning_conversation_fn=run_planning_conversation_fn,
            planning_config_from_env_fn=planning_config_from_env_fn,
        )
    except AutopilotPlanningBridgeError as exc:
        _write_fresh_planning_exception(
            planning_dir=planning_dir,
            feature=feature,
            task_direction=inputs.resolved_task_direction,
            exc=exc,
        )
        raise
    except Exception as exc:
        _write_fresh_planning_exception(
            planning_dir=planning_dir,
            feature=feature,
            task_direction=inputs.resolved_task_direction,
            exc=exc,
        )
        raise AutopilotPlanningBridgeError(
            error_code="fresh_planning_exception",
            message=f"Fresh model planning failed before approved contract artifacts were written: {type(exc).__name__}: {str(exc)[:500]}",
            remediation=[
                "Inspect .planning_failure.json and planner trace artifacts.",
                "Fix the workflow planning failure or rerun with a narrower task direction.",
            ],
            details={
                "planning_status": "error",
                "reason": "fresh_planning_exception",
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
            },
        ) from exc
    conversation_payload = _write_fresh_conversation(planning_result, inputs.conversation_path, steps_run, artifacts)
    # GPT-v6: inline PLANNING_APPROVAL_REQUIRED auto-accept. Runs AFTER the
    # conversation is on disk (so the response file we write references a
    # real artifact) and BEFORE _validate_fresh_planning_result (which would
    # otherwise raise on status=escalation_required and abort the bootstrap).
    # The helper is conservative — only auto-accepts when the planner self-
    # graded ``auto_approve``, scores are both >= 8, blocking history is
    # strictly non-increasing to 0, and every selected-plan task respects
    # the files-to-change cap + invariants rule. All other paths no-op and
    # let the existing manual approval gate fire.
    auto_accept_audit: dict[str, Any] | None = None
    auto_accepted = False
    if str(getattr(planning_result, "status", "") or "").strip().lower() == "escalation_required":
        from kodawari.autopilot.escalation.planning_auto_accept import (
            try_auto_accept_planning_approval,
        )

        auto_result = try_auto_accept_planning_approval(
            project_root=project_root,
            planning_dir=planning_dir,
            conversation_payload=conversation_payload,
        )
        if auto_result.applied:
            auto_accepted = True
            auto_accept_audit = dict(auto_result.audit)
            # Mutate planning_result so the rest of the bootstrap pipeline
            # treats this as a normally-approved plan.
            planning_result.final_plan = dict(auto_result.selected_plan or planning_result.final_plan)
            planning_result.status = "approved"
            # Rewrite PLANNING_CONVERSATION.json with the post-accept state
            # so any consumer that re-reads it (resume hooks, audit tools)
            # sees the resolved view, not the pre-accept escalation_required.
            conversation_payload = dict(conversation_payload)
            conversation_payload["status"] = "approved"
            conversation_payload["final_plan"] = dict(auto_result.selected_plan or {})
            conversation_payload["auto_accept"] = auto_accept_audit
            try:
                bridge_artifacts.write_contract_artifact(
                    inputs.conversation_path,
                    conversation_payload,
                    schema_name="planning_conversation",
                )
            except Exception as exc:  # noqa: BLE001 — non-fatal; response on disk is the source of truth
                logger.warning("auto_accept conversation rewrite failed: %s", exc)
    _validate_fresh_planning_result(planning_result)
    raise_if_context_scout_awaiting_decision_fn(conversation_payload)
    # GPT-v6: auto-accepted plans skip the splitter — the planner's clean
    # decomposition is what we approved; running the splitter would let it
    # rewrite the task set after the gate had already cleared.
    if not auto_accepted:
        planning_result = _apply_task_splitter(
            planning_result=planning_result,
            project_root=project_root,
            planning_config_from_env_fn=planning_config_from_env_fn,
        )
    task_graph_payload = _write_fresh_task_graph(
        inputs=inputs,
        planning_result=planning_result,
        feature=feature,
        planning_dir=planning_dir,
        project_root=project_root,
        steps_run=steps_run,
        artifacts=artifacts,
        force_splitter_off=auto_accepted,
    )
    cards = _write_fresh_task_cards(planning_result, task_graph_payload, planning_dir, artifacts)
    primary_task_id, active_card, active_card_path, selection = _select_and_write_active_card(
        planning_dir,
        task_graph_payload=task_graph_payload,
        cards=cards,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    return _snapshot_from_fresh_plan(
        inputs=inputs,
        planning_result=planning_result,
        planning_dir=planning_dir,
        prd_path=prd_path,
        primary_task_id=primary_task_id,
        active_card=active_card,
        active_card_path=active_card_path,
        selection=selection,
        steps_run=steps_run,
        artifacts=artifacts,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_fresh_planning_exception(
    *,
    planning_dir: Path,
    feature: str,
    task_direction: str,
    exc: Exception,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": PLANNING_PROGRESS_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "feature": str(feature or "").strip(),
        "task_direction": str(task_direction or "").strip(),
        "status": "error",
        "reason": "fresh_planning_exception",
        "error_type": type(exc).__name__,
        "message": str(exc)[:1000],
    }
    if isinstance(exc, AutopilotPlanningBridgeError):
        payload["error_code"] = exc.error_code
        payload["details"] = dict(exc.details or {})
    else:
        payload["error_code"] = "fresh_planning_exception"
    path = planning_dir / PLANNING_FAILURE_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return


def _run_fresh_planning_conversation(
    *,
    inputs: _PlanningInputs,
    feature: str,
    prd_path: Path | None,
    planning_dir: Path,
    project_root: Path,
    run_planning_conversation_fn: RunPlanningConversation,
    planning_config_from_env_fn: PlanningConfigFactory,
) -> Any:
    return run_planning_conversation_fn(
        config=planning_config_from_env_fn(
            project_root,
            task_direction=inputs.resolved_task_direction,
            repo_inventory=inputs.refreshed_inventory,
        ),
        project_root=project_root,
        planning_dir=planning_dir,
        task_direction=inputs.resolved_task_direction,
        repo_inventory=inputs.refreshed_inventory,
        prd_path=prd_path,
        feature=feature,
    )


def _write_fresh_conversation(
    planning_result: Any,
    conversation_path: Path,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> dict[str, Any]:
    conversation_payload = result_to_artifact(planning_result)
    bridge_artifacts.write_contract_artifact(conversation_path, conversation_payload, schema_name="planning_conversation")
    steps_run.append("planning-conversation")
    artifacts[conversation_path.name] = str(conversation_path.resolve())
    return conversation_payload


def _validate_fresh_planning_result(planning_result: Any) -> None:
    if not list(dict(planning_result.final_plan).get("tasks") or []):
        raise AutopilotPlanningBridgeError(
            error_code="planning_conversation_invalid",
            message="Model planner produced no executable tasks.",
            remediation=["Refine --task/--prd input and rerun autopilot."],
            details={"planning_status": planning_result.status},
        )
    status = str(planning_result.status).strip().lower()
    if status == "error":
        raise AutopilotPlanningBridgeError(
            error_code="planning_conversation_error",
            message="Model-driven planning did not complete successfully.",
            remediation=["Check planner/reviewer CLI availability and rerun autopilot."],
            details={"planning_status": planning_result.status},
        )
    if status in {"escalation_required", "precondition_blocked"}:
        escalation = dict(getattr(planning_result, "escalation", None) or {})
        reason = str(escalation.get("gate_reason") or escalation.get("termination_reason") or status).strip()
        raise AutopilotPlanningBridgeError(
            error_code="planning_escalation_required" if status == "escalation_required" else "planning_precondition_blocked",
            message="Model-driven planning stopped before executable contract artifacts were approved.",
            remediation=[
                "Resolve the planning blocker or rerun with a narrower task direction.",
                "Do not execute stale TASK_GRAPH/TASK_CARD artifacts from an unapproved planning result.",
            ],
            details={"planning_status": planning_result.status, "reason": reason},
        )


def _apply_task_splitter(
    *,
    planning_result: Any,
    project_root: Path,
    planning_config_from_env_fn: PlanningConfigFactory,
) -> Any:
    """Apply task splitter to planning result's final_plan.

    Modifies planning_result.final_plan in place; returns modified result.
    On splitter failure, falls back to original plan without raising.
    """
    try:
        config = planning_config_from_env_fn(project_root)
    except Exception:
        return planning_result

    if not config.task_splitter_enabled:
        return planning_result

    final_plan = dict(planning_result.final_plan or {})
    original_tasks = list(final_plan.get("tasks") or [])
    if not original_tasks:
        return planning_result

    split_result, error = split_plan(
        executable="claude",
        plan_payload=final_plan,
        model=config.planner_model,
        driver=config.planner_driver,
        transport=config.plan_reviewer_transport,
        timeout_seconds=config.planner_timeout_seconds,
        project_root=project_root,
    )

    if error or split_result is None:
        return planning_result

    split_tasks = split_result.get("tasks")
    if not isinstance(split_tasks, list) or not split_tasks:
        return planning_result

    final_plan["tasks"] = split_tasks
    planning_result.final_plan = final_plan
    return planning_result


def _write_fresh_task_graph(
    *,
    inputs: _PlanningInputs,
    planning_result: Any,
    feature: str,
    planning_dir: Path,
    project_root: Path,
    steps_run: list[str],
    artifacts: dict[str, str],
    force_splitter_off: bool = False,
) -> dict[str, Any]:
    splitter_enabled = os.environ.get("WORKFLOW_TASK_SPLITTER", "").lower() in {"1", "true", "yes", "on"}
    # GPT-v6: when the planning gate auto-accepted, the splitter must NOT
    # rewrite the task set even if WORKFLOW_TASK_SPLITTER=1 was opted in
    # at the env level — the approved plan is what we approved.
    if force_splitter_off:
        splitter_enabled = False
    task_graph_payload = plan_to_task_graph(
        planning_result.final_plan,
        feature=feature,
        repo_inventory=inputs.refreshed_inventory,
        project_root=project_root,
        splitter_enabled=splitter_enabled,
    )
    task_graph_payload = planning_source.attach_planning_source(task_graph_payload, inputs.current_source)
    _raise_if_graph_errors(
        task_graph_payload,
        remediation=["Refine planning conversation output before rerunning autopilot."],
    )
    task_graph_path = planning_dir / "TASK_GRAPH.json"
    bridge_artifacts.write_contract_artifact(task_graph_path, task_graph_payload, schema_name="task_graph")
    steps_run.append("task-plan")
    artifacts[task_graph_path.name] = str(task_graph_path.resolve())
    return task_graph_payload


def _raise_if_graph_errors(task_graph_payload: dict[str, Any], *, remediation: list[str]) -> None:
    graph_errors = validate_task_graph(task_graph_payload)
    if graph_errors:
        raise AutopilotPlanningBridgeError(
            error_code="task_graph_invalid",
            message=graph_errors[0],
            remediation=remediation,
            details={"validation_errors": list(graph_errors)},
        )


def _write_fresh_task_cards(
    planning_result: Any,
    task_graph_payload: dict[str, Any],
    planning_dir: Path,
    artifacts: dict[str, str],
) -> list[dict[str, Any]]:
    cards = plan_to_task_cards(planning_result.final_plan, task_graph_payload)
    if not cards:
        raise AutopilotPlanningBridgeError(
            error_code="task_card_invalid",
            message="No task cards were generated from model-driven planning output.",
            remediation=["Refine planning conversation output before rerunning autopilot."],
        )
    planning_mode = str(task_graph_payload.get("planning_mode") or "existing")
    for card in cards:
        _write_fresh_task_card(card, planning_dir, artifacts, planning_mode=planning_mode)
    return cards


def _write_fresh_task_card(
    card: dict[str, Any],
    planning_dir: Path,
    artifacts: dict[str, str],
    *,
    planning_mode: str = "existing",
) -> None:
    errors = validate_task_card(card, planning_mode=planning_mode)
    if errors:
        raise AutopilotPlanningBridgeError(
            error_code="task_card_invalid",
            message=errors[0],
            remediation=["Fix generated task cards before rerunning autopilot."],
            details={"validation_errors": list(errors)},
        )
    task_id = str(card.get("task_id") or "").strip().upper()
    if not task_id:
        return
    named_path = planning_dir / f"TASK_CARD_{task_id}.json"
    bridge_artifacts.write_contract_artifact(named_path, card, schema_name="task_card")
    artifacts[named_path.name] = str(named_path.resolve())


def _select_and_write_active_card(
    planning_dir: Path,
    *,
    task_graph_payload: dict[str, Any],
    cards: list[dict[str, Any]],
    steps_run: list[str],
    artifacts: dict[str, str],
) -> tuple[str, dict[str, Any], Path, Any]:
    selection = generic_bootstrap.select_task_or_raise(planning_dir, task_graph_payload)
    active_card = next(
        (dict(card) for card in cards if str(card.get("task_id") or "").strip().upper() == selection.task_id),
        dict(cards[0]),
    )
    active_card_path = planning_dir / "TASK_CARD_ACTIVE.json"
    bridge_artifacts.write_contract_artifact(active_card_path, active_card, schema_name="task_card")
    steps_run.append("task-prepare")
    artifacts[active_card_path.name] = str(active_card_path.resolve())
    return selection.task_id, active_card, active_card_path, selection


def _snapshot_from_fresh_plan(
    *,
    inputs: _PlanningInputs,
    planning_result: Any,
    planning_dir: Path,
    prd_path: Path | None,
    primary_task_id: str,
    active_card: dict[str, Any],
    active_card_path: Path,
    selection: Any,
    steps_run: list[str],
    artifacts: dict[str, str],
) -> AutopilotPlanningSnapshot:
    task_label, task_scope = generic_bootstrap.task_runtime(active_card)
    approval = dict(planning_result.approval)
    active_scope_view = dict(approval.get("active_scope_view") or {})
    return AutopilotPlanningSnapshot(
        planning_mode=inputs.planning_mode,
        archetype=str(planning_result.archetype or inputs.refreshed_inventory.get("archetype") or "auto"),
        capabilities=tuple(planning_result.capabilities or generic_bootstrap.string_list(inputs.refreshed_inventory.get("capabilities"))),
        primary_task_id=primary_task_id,
        task_label=task_label,
        task_scope=task_scope,
        task_card_path=active_card_path.resolve(),
        prd_path=prd_path.resolve() if prd_path is not None else None,
        steps_run=tuple(steps_run),
        artifacts=dict(artifacts),
        planning_status=str(planning_result.status or ""),
        planning_approval_decision=str(approval.get("decision") or ""),
        planning_approval_reason=str(approval.get("reason") or ""),
        planning_approval_active_scope_decision=str(active_scope_view.get("decision") or ""),
        input_fingerprint=str(planning_result.input_fingerprint or ""),
        task_direction=inputs.resolved_task_direction,
        stage_profile=EPIC_PLAN.profile_id,
        selection_action=selection.action_value,
        selection_reason=selection.reason,
        planning_source_status="fresh",
    )


def reuse_existing_task_graph_snapshot(
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
    raise_if_context_scout_awaiting_decision_fn: ContextScoutGuard,
) -> AutopilotPlanningSnapshot | None:
    task_graph_path = planning_dir / "TASK_GRAPH.json"
    if not task_graph_path.exists():
        return None
    task_graph_payload = bridge_artifacts.load_optional_contract_json(task_graph_path, schema_name="task_graph")
    if task_graph_payload is None:
        return None
    source_matches, source_status, mismatches = planning_source.planning_source_status(task_graph_payload, current_source)
    if not source_matches:
        raise AutopilotPlanningBridgeError(
            error_code="planning_graph_stale",
            message="Existing TASK_GRAPH.json no longer matches the current planning input.",
            remediation=[
                "Rerun with --replan to regenerate TASK_GRAPH.json from the current PRD/task input.",
                "Or omit --prd/--task when you intentionally want to continue the existing task graph.",
            ],
            details={"planning_source_status": source_status, "mismatches": mismatches},
        )
    artifacts[task_graph_path.name] = str(task_graph_path.resolve())
    if existing_conversation is not None:
        conversation_path = planning_dir / "PLANNING_CONVERSATION.json"
        artifacts[conversation_path.name] = str(conversation_path.resolve())
        raise_if_context_scout_awaiting_decision_fn(existing_conversation)
    selection = generic_bootstrap.select_task_or_raise(planning_dir, task_graph_payload)
    active_card = generic_bootstrap.activate_selected_task_card(
        planning_dir,
        task_graph=task_graph_payload,
        task_id=selection.task_id,
        steps_run=steps_run,
        artifacts=artifacts,
    )
    task_label, task_scope = generic_bootstrap.task_runtime(active_card)
    return AutopilotPlanningSnapshot(
        planning_mode=planning_mode,
        archetype=str(
            (existing_conversation or {}).get("archetype")
            or task_graph_payload.get("archetype")
            or repo_inventory.get("archetype")
            or "auto"
        ),
        capabilities=tuple(
            generic_bootstrap.string_list((existing_conversation or {}).get("capabilities"))
            or generic_bootstrap.string_list(task_graph_payload.get("capabilities"))
            or generic_bootstrap.string_list(repo_inventory.get("capabilities"))
        ),
        primary_task_id=selection.task_id,
        task_label=task_label,
        task_scope=task_scope,
        task_card_path=(planning_dir / "TASK_CARD_ACTIVE.json").resolve(),
        prd_path=prd_path.resolve() if prd_path is not None else None,
        steps_run=tuple([*steps_run, "next-task-select"]),
        artifacts=dict(artifacts),
        planning_status="",
        planning_approval_decision="",
        planning_approval_reason="",
        planning_approval_active_scope_decision="",
        input_fingerprint=str((existing_conversation or {}).get("input_fingerprint") or ""),
        task_direction=str((existing_conversation or {}).get("task_direction") or current_source.get("task_direction") or ""),
        stage_profile=selection.stage_profile or TAKE_TASK.profile_id,
        selection_action=selection.action_value,
        selection_reason=selection.reason,
        planning_source_status=source_status,
    )


def _clean_text(value: Any) -> str:
    return str(value or "").strip()

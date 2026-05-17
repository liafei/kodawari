"""Adoption-friendly planning facade for kodawari."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from kodawari.autopilot.execution.execution_artifacts import is_test_environment
from kodawari.cli.contract.autopilot_contract_bridge import (
    AutopilotPlanningBridgeError,
    ensure_contract_first_planning,
)
from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.contract.contract_first_schema import load_contract_first_artifact
from kodawari.cli.io_atomic import atomic_write_text
from kodawari.cli.contract.planning_conversation_compat import (
    load_architecture_plan_compatible,
    load_prd_intake_compatible,
)
from kodawari.cli.contract.plans_markdown import load_optional_task_card_active, render_plans_markdown
from kodawari.cli.evidence.provenance import build_cli_provenance


def _planning_dir(project_root: Path, feature: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    return (project_root / "planning" / feature).resolve()


def _resolve_prd_path(args: argparse.Namespace) -> Path | None:
    raw = str(getattr(args, "prd", None) or getattr(args, "requirements_file", None) or "").strip()
    if not raw:
        return None
    path = Path(raw).resolve()
    if not path.exists() or not path.is_file():
        raise AutopilotPlanningBridgeError(
            error_code="prd_missing",
            message=f"PRD file not found: {path}",
            remediation=["Provide `--prd <path>` or a valid `--requirements-file <path>` before rerunning plan."],
        )
    return path


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _emit_error(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    error: AutopilotPlanningBridgeError,
) -> int:
    payload = build_error_payload(
        command="plan",
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
        error=str(error),
        error_code=error.error_code,
        remediation=list(error.remediation) or ["Fix the planning input and rerun `kodawari plan`."],
        next_action="Fix the planning input or artifact issue, then rerun `kodawari plan`.",
        extra={
            "_rc": 2,
            "status": "BLOCKED",
            "feature": feature,
            "planning_dir": str(planning_dir),
            **dict(error.details or {}),
        },
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def _resolve_planner_route(args: argparse.Namespace, *, planning_dir: Path, task_direction: str) -> tuple[bool, str]:
    """Return (use_model_planning, resolved_route_name) from --planner-route arg."""
    route = str(getattr(args, "planner_route", "") or "auto").strip().lower()
    if route == "model":
        return True, "model"
    if route == "generic":
        return False, "generic"
    # auto: infer from explicit PRD/task input or existing model artifacts.
    use_model = bool(
        task_direction
        or str(getattr(args, "prd", "") or getattr(args, "requirements_file", "") or "").strip()
        or (planning_dir / "PLANNING_CONVERSATION.json").exists()
        or (planning_dir / "TASK_GRAPH.json").exists()
    )
    if (
        is_test_environment()
        and os.environ.get("WORKFLOW_FORCE_MODEL_PLANNING", "") != "1"
        and not task_direction
        and not (planning_dir / "PLANNING_CONVERSATION.json").exists()
        and not (planning_dir / "TASK_GRAPH.json").exists()
    ):
        use_model = False
    return use_model, "model" if use_model else "generic"


def _planning_decision_block(
    *,
    feature: str,
    planning_snapshot: Any,
) -> dict[str, Any] | None:
    planning_status = str(getattr(planning_snapshot, "planning_status", "") or "").strip().lower()
    approval_decision = str(getattr(planning_snapshot, "planning_approval_decision", "") or "").strip().lower()
    approval_reason = str(getattr(planning_snapshot, "planning_approval_reason", "") or "").strip()
    if planning_status == "escalation_required":
        decision_kind = "planning_escalation"
        blocking_reason = "planning escalation required"
        question = "Planning did not converge; provide a human resolution before execution."
    elif approval_decision == "human_required":
        decision_kind = "planning_approval"
        blocking_reason = "planning approval required"
        question = "Planning requires human approval before execution."
    else:
        return None
    decision_id = f"{feature}:{decision_kind}"
    return {
        "_rc": 0,
        "status": "awaiting_decision",
        "planning_status": planning_status,
        "planning_approval_decision": approval_decision,
        "planning_approval_reason": approval_reason,
        "blocking_reason": blocking_reason,
        "decision_request": {
            "schema_version": "workflow.plan.decision_hint.v1",
            "decision_id": decision_id,
            "decision_kind": decision_kind,
            "question": question,
            "context_summary": approval_reason or planning_status,
            "recommended_option": "resolve",
            "blocking_reason": blocking_reason,
        },
        "interaction_state": "AWAITING_DECISION",
        "decision_kind": decision_kind,
        "decision_id": decision_id,
        "decision_request_present": False,
        "next_action_type": "await_decision",
        "next_action": "Review PLANNING_CONVERSATION.json and rerun `kodawari autopilot` to record a decision, or revise the planning input and rerun `kodawari plan`.",
        "remediation": [
            "Inspect PLANNING_CONVERSATION.json for unresolved reviewer findings or score-check failures.",
            "Do not execute task cards until the planning decision is resolved.",
        ],
    }


def run_plan_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _planning_dir(project_root, feature, getattr(args, "planning_dir", None))
    task_direction = str(getattr(args, "task", "") or "").strip()
    use_model_planning, resolved_route = _resolve_planner_route(
        args, planning_dir=planning_dir, task_direction=task_direction
    )
    import logging as _logging
    _logging.getLogger(__name__).info("planner_route=%s use_model_planning=%s", resolved_route, use_model_planning)
    try:
        planning_snapshot = ensure_contract_first_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            prd_path=_resolve_prd_path(args),
            task_direction=task_direction,
            use_model_planning=use_model_planning,
            force_replan=bool(getattr(args, "replan", False)),
        )
    except AutopilotPlanningBridgeError as exc:
        return _emit_error(project_root=project_root, planning_dir=planning_dir, feature=feature, error=exc)

    task_graph = load_contract_first_artifact(planning_dir / "TASK_GRAPH.json", schema_name="task_graph")
    intake = load_prd_intake_compatible(planning_dir) or {}
    architecture_plan = load_architecture_plan_compatible(planning_dir) or {}
    task_card_active = load_optional_task_card_active(planning_dir / "TASK_CARD_ACTIVE.json")
    plans_path = planning_dir / "Plans.md"
    atomic_write_text(
        plans_path,
        render_plans_markdown(
            task_graph,
            intake=intake,
            architecture_plan=architecture_plan,
            task_card_active=task_card_active,
            generated_at=str(task_graph.get("generated_at") or ""),
            source_digest=_sha256_file(planning_dir / "TASK_GRAPH.json"),
        ),
    )
    artifacts = dict(planning_snapshot.artifacts)
    artifacts["Plans.md"] = str(plans_path.resolve())
    payload_base = {
        "_rc": 0,
        "status": "PASS",
        "entrypoint": "kodawari plan",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "planning_mode": planning_snapshot.planning_mode,
        "archetype": planning_snapshot.archetype,
        "capabilities": list(planning_snapshot.capabilities),
        "primary_task_id": planning_snapshot.primary_task_id,
        "task_label": planning_snapshot.task_label,
        "task_scope": planning_snapshot.task_scope,
        "steps_run": list(planning_snapshot.steps_run),
        "planner_route": resolved_route,
        "stage_profile": str(getattr(planning_snapshot, "stage_profile", "") or ""),
        "selection_action": str(getattr(planning_snapshot, "selection_action", "") or ""),
        "selection_reason": str(getattr(planning_snapshot, "selection_reason", "") or ""),
        "planning_source_status": str(getattr(planning_snapshot, "planning_source_status", "") or ""),
        "artifacts": artifacts,
        "provenance": build_cli_provenance(
            command="plan",
            project_root=project_root,
            planning_dir=planning_dir,
            module_file=Path(__file__),
        ),
    }
    decision_block = _planning_decision_block(feature=feature, planning_snapshot=planning_snapshot)
    if decision_block is not None:
        payload_base.update(decision_block)
    payload = normalize_mutating_payload(payload_base)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return int(payload.get("_rc", 0) or 0)


__all__ = ["run_plan_command"]


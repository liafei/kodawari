"""Decision-bridge helpers for autopilot CLI runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.cli.delivery.approval_engine import evaluate_auto_approval
from kodawari.cli.contract.autopilot_contract_bridge import AutopilotPlanningSnapshot
from kodawari.cli.runtime.autopilot_decision_runtime import (
    DecisionKind,
    build_decision_request,
    clear_consumed_decision_artifacts,
    decision_already_approved,
    decision_pending,
    decision_runtime_snapshot,
    load_decision_request,
    load_decision_response,
    record_approved_decision,
    response_matches_request,
    write_decision_request,
)
from kodawari.cli.runtime.autopilot_interaction_state import build_interaction_snapshot


def _load_snapshot_artifact(
    planning_snapshot: AutopilotPlanningSnapshot | None,
    name: str,
) -> dict[str, Any]:
    if planning_snapshot is None:
        return {}
    raw_path = str(dict(planning_snapshot.artifacts).get(name) or "").strip()
    if not raw_path:
        return {}
    path = Path(raw_path)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _surface_count(
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
) -> int:
    surfaces = list(architecture_plan.get("surfaces") or [])
    if surfaces:
        return len([item for item in surfaces if isinstance(item, dict)])
    return len([item for item in list(repo_inventory.get("surfaces") or []) if isinstance(item, dict)])


def _task_count(task_graph: dict[str, Any]) -> int:
    return len([item for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)])


def _decision_options(kind: DecisionKind) -> tuple[list[dict[str, str]], set[str]]:
    if kind == DecisionKind.RELEASE_APPROVAL:
        return (
            [
                {"option_id": "ship", "label": "Approve release"},
                {"option_id": "hold", "label": "Hold release", "details": "Keep artifacts for review and rerun later."},
            ],
            {"ship"},
        )
    return (
        [
            {"option_id": "approve", "label": "Approve and continue"},
            {"option_id": "revise", "label": "Revise first", "details": "Pause and update planning before continuing."},
        ],
        {"approve"},
    )


def _decision_spec(
    *,
    feature: str,
    kind: DecisionKind,
    question: str,
    context_summary: str,
    blocking_reason: str,
) -> dict[str, Any]:
    options, approved_options = _decision_options(kind)
    return {
        "decision_id": f"{feature}:{kind.value}",
        "decision_kind": kind,
        "question": question,
        "context_summary": context_summary,
        "options": options,
        "recommended_option": next(iter(sorted(approved_options))),
        "blocking_reason": blocking_reason,
        "approved_options": approved_options,
    }


def build_planning_decision_spec(
    *,
    feature: str,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> dict[str, Any] | None:
    architecture_plan = _load_snapshot_artifact(planning_snapshot, "ARCHITECTURE_PLAN.json")
    prd_intake = _load_snapshot_artifact(planning_snapshot, "PRD_INTAKE.json")
    repo_inventory = _load_snapshot_artifact(planning_snapshot, "REPO_INVENTORY.json")
    task_graph = _load_snapshot_artifact(planning_snapshot, "TASK_GRAPH.json")
    if not architecture_plan:
        return None
    confidence_issues = [str(item).strip() for item in list(prd_intake.get("confidence_issues") or []) if str(item).strip()]
    if confidence_issues:
        return _decision_spec(
            feature=feature,
            kind=DecisionKind.INTENT_CLARIFICATION,
            question="需求存在低置信度点，是否按当前理解继续？",
            context_summary="; ".join(confidence_issues[:3]),
            blocking_reason="planning confidence is low",
        )
    surface_count = _surface_count(repo_inventory, architecture_plan)
    if planning_snapshot is not None and (planning_snapshot.planning_mode == "greenfield" or surface_count > 1):
        return _decision_spec(
            feature=feature,
            kind=DecisionKind.ARCHITECTURE_FREEZE,
            question="架构与边界是否冻结，可以进入实现？",
            context_summary=f"archetype={planning_snapshot.archetype}; surfaces={surface_count}",
            blocking_reason="architecture approval required before implementation",
        )
    if _task_count(task_graph) > 10 or surface_count > 2:
        return _decision_spec(
            feature=feature,
            kind=DecisionKind.TASK_PLAN_FREEZE,
            question="任务图规模较大，是否按当前任务拆分继续？",
            context_summary=f"tasks={_task_count(task_graph)}; surfaces={surface_count}",
            blocking_reason="task plan approval required before implementation",
        )
    return None


def build_release_decision_spec(
    feature: str,
    *,
    execution_context: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    """Build the release-approval decision spec.

    When *execution_context* and *project_root* are provided, the approval engine
    is consulted first. If the engine decides "auto_approve", returns None (caller
    should skip writing a decision request). Otherwise returns the decision spec
    dict as usual.
    """
    if execution_context is not None and project_root is not None:
        decision = evaluate_auto_approval(
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            context=execution_context,
            project_root=project_root,
        )
        if decision.action == "auto_approve":
            return None

    return _decision_spec(
        feature=feature,
        kind=DecisionKind.RELEASE_APPROVAL,
        question="是否批准进入 release tail 并生成 ship-readiness 结论？",
        context_summary="execution/review/verify 已完成，等待最终发布审批。",
        blocking_reason="release approval required before ship-readiness",
    )


def _awaiting_decision_payload(
    *,
    args: argparse.Namespace,
    planning_dir: Path,
    planning_snapshot: AutopilotPlanningSnapshot | None,
    request_payload: dict[str, Any],
    base_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(base_payload or {})
    payload.update(
        {
            "status": "awaiting_decision",
            "entrypoint": "kodawari autopilot",
            "feature": str(args.feature),
            "planning_dir": str(planning_dir),
            "planning_artifact_mode": "contract_first" if planning_snapshot is not None else "legacy",
            "planning_snapshot": planning_snapshot.to_dict() if planning_snapshot is not None else {},
            "decision_request": dict(request_payload),
        }
    )
    payload.update(
        build_interaction_snapshot(
            decision_pending=True,
            decision_kind=request_payload.get("decision_kind"),
            decision_id=request_payload.get("decision_id"),
            decision_request_present=True,
        )
    )
    return payload


def _blocked_decision_payload(
    *,
    base_payload: dict[str, Any] | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
) -> dict[str, Any]:
    payload = dict(base_payload or {})
    payload["status"] = "blocked"
    payload["blocking_reason"] = "human decision did not approve continuation"
    payload["decision_request"] = dict(request_payload)
    payload["decision_response"] = dict(response_payload)
    payload.update(
        build_interaction_snapshot(
            decision_pending=False,
            decision_kind=request_payload.get("decision_kind"),
            decision_id=request_payload.get("decision_id"),
            decision_request_present=False,
            final_status="BLOCKED",
            stop_reason="BLOCKED",
            blocked=True,
            is_terminal=True,
        )
    )
    return payload


def resolve_decision_payload_for_spec(
    *,
    args: argparse.Namespace,
    planning_dir: Path,
    planning_snapshot: AutopilotPlanningSnapshot | None,
    spec: dict[str, Any] | None,
    base_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if spec is None:
        return None
    request_payload = load_decision_request(planning_dir) or {}
    response_payload = load_decision_response(planning_dir) or {}
    requested_id = str(spec.get("decision_id") or "")
    current_id = str(request_payload.get("decision_id") or "")
    if current_id != requested_id:
        if decision_already_approved(planning_dir, requested_id):
            return None
        request_payload = {}
        response_payload = {}
        current_id = ""
    if current_id == requested_id and decision_pending(planning_dir):
        return _awaiting_decision_payload(
            args=args,
            planning_dir=planning_dir,
            planning_snapshot=planning_snapshot,
            request_payload=request_payload,
            base_payload=base_payload,
        )
    if current_id == requested_id and response_matches_request(request_payload, response_payload):
        approved = str(response_payload.get("selected_option") or "").strip() in set(spec.get("approved_options") or set())
        if approved:
            record_approved_decision(planning_dir, requested_id)
            clear_consumed_decision_artifacts(planning_dir)
            return None
        return _blocked_decision_payload(
            base_payload=base_payload,
            request_payload=request_payload,
            response_payload=response_payload,
        )
    request_payload = build_decision_request(
        decision_id=str(spec.get("decision_id") or ""),
        decision_kind=spec.get("decision_kind") or "",
        question=str(spec.get("question") or ""),
        context_summary=str(spec.get("context_summary") or ""),
        options=list(spec.get("options") or []),
        recommended_option=str(spec.get("recommended_option") or ""),
        blocking_reason=str(spec.get("blocking_reason") or ""),
    )
    write_decision_request(planning_dir, request_payload)
    return _awaiting_decision_payload(
        args=args,
        planning_dir=planning_dir,
        planning_snapshot=planning_snapshot,
        request_payload=request_payload,
        base_payload=base_payload,
    )


def build_autopilot_interaction_snapshot(
    *,
    planning_dir: Path,
    run_result: dict[str, Any],
    state: Any,
) -> dict[str, Any]:
    decision = decision_runtime_snapshot(planning_dir)
    unified = dict(getattr(state, "get_unified_status", lambda: {})() or {})
    execution_result = dict(run_result.get("execution_result") or {})
    blocking_reason = str(
        execution_result.get("blocking_reason")
        or unified.get("blocking_reason")
        or run_result.get("blocking_reason")
        or ""
    )
    return build_interaction_snapshot(
        decision_pending=bool(decision.get("decision_pending", False)),
        decision_kind=decision.get("decision_kind"),
        decision_id=decision.get("decision_id"),
        decision_request_present=bool(decision.get("decision_request_present", False)),
        environment_error_code=execution_result.get("error_code"),
        environment_blocking_reason=blocking_reason,
        final_status=unified.get("final_status"),
        stop_reason=unified.get("stop_reason"),
        blocked=bool(unified.get("is_blocked", False)),
        is_terminal=bool(unified.get("is_terminal", False)),
    )


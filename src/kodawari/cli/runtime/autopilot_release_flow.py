"""Decision bridge and release-tail helpers for the autopilot command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

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
from kodawari.cli.runtime.autopilot_decision_bridge import (
    build_release_decision_spec as _bridge_release_decision_spec,
)
from kodawari.cli.runtime.autopilot_interaction_state import build_interaction_snapshot
from kodawari.cli.runtime.autopilot_release_runtime import AutopilotReleaseTailConfig
from kodawari.cli.runtime.autopilot_runtime_flow import explicit_planning_input_requested
from kodawari.cli.runtime.autopilot_workflow_runtime import autopilot_payload_status
from kodawari.infra.contract_version import MERGED_CONTRACT_VERSION

REQUIRED_PLANNING_ARTIFACTS = ["PLAN.md", "TASKS.md", "ACCEPTANCE.md", "GATE.md"]


def build_planning_contract_summary(planning_artifacts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    complete = all(bool(planning_artifacts[name]["exists"]) for name in REQUIRED_PLANNING_ARTIFACTS)
    return {
        "version": MERGED_CONTRACT_VERSION,
        "required_artifacts": list(REQUIRED_PLANNING_ARTIFACTS),
        "complete": complete,
    }


def decision_snapshot(planning_dir: Path) -> dict[str, Any]:
    return decision_runtime_snapshot(planning_dir)


def load_snapshot_artifact(
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


def surface_count(
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
) -> int:
    surfaces = list(architecture_plan.get("surfaces") or [])
    if surfaces:
        return len([item for item in surfaces if isinstance(item, dict)])
    return len([item for item in list(repo_inventory.get("surfaces") or []) if isinstance(item, dict)])


def task_count(task_graph: dict[str, Any]) -> int:
    return len([item for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)])


def decision_options(kind: DecisionKind) -> tuple[list[dict[str, str]], set[str]]:
    if kind == DecisionKind.PLANNING_ESCALATION:
        return (
            [
                {"option_id": "resolve", "label": "Provide resolution"},
                {"option_id": "revise", "label": "Revise planning", "details": "Adjust planning input and rerun."},
            ],
            {"resolve"},
        )
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


def decision_spec(
    *,
    feature: str,
    kind: DecisionKind,
    question: str,
    context_summary: str,
    blocking_reason: str,
) -> dict[str, Any]:
    options, approved_options = decision_options(kind)
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


def planning_decision_spec(
    *,
    feature: str,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> dict[str, Any] | None:
    stage_profile = str(getattr(planning_snapshot, "stage_profile", "") or "").strip()
    if stage_profile in {"take_task", "recovery"}:
        return None
    conversation = load_snapshot_artifact(planning_snapshot, "PLANNING_CONVERSATION.json")
    if conversation:
        spec = _planning_conversation_decision_spec(
            feature=feature,
            conversation=conversation,
        )
        if spec is not None:
            return spec

    architecture_plan = load_snapshot_artifact(planning_snapshot, "ARCHITECTURE_PLAN.json")
    prd_intake = load_snapshot_artifact(planning_snapshot, "PRD_INTAKE.json")
    repo_inventory = load_snapshot_artifact(planning_snapshot, "REPO_INVENTORY.json")
    task_graph = load_snapshot_artifact(planning_snapshot, "TASK_GRAPH.json")
    if not architecture_plan:
        return None
    confidence_issues = [str(item).strip() for item in list(prd_intake.get("confidence_issues") or []) if str(item).strip()]
    if confidence_issues:
        return decision_spec(
            feature=feature,
            kind=DecisionKind.INTENT_CLARIFICATION,
            question="需求存在低置信度点，是否按当前理解继续？",
            context_summary="; ".join(confidence_issues[:3]),
            blocking_reason="planning confidence is low",
        )
    counted_surfaces = surface_count(repo_inventory, architecture_plan)
    if planning_snapshot is not None and (planning_snapshot.planning_mode == "greenfield" or counted_surfaces > 1):
        return decision_spec(
            feature=feature,
            kind=DecisionKind.ARCHITECTURE_FREEZE,
            question="架构与边界是否冻结，可以进入实现？",
            context_summary=f"archetype={planning_snapshot.archetype}; surfaces={counted_surfaces}",
            blocking_reason="architecture approval required before implementation",
        )
    if task_count(task_graph) > 10 or counted_surfaces > 2:
        return decision_spec(
            feature=feature,
            kind=DecisionKind.TASK_PLAN_FREEZE,
            question="任务图规模较大，是否按当前任务拆分继续？",
            context_summary=f"tasks={task_count(task_graph)}; surfaces={counted_surfaces}",
            blocking_reason="task plan approval required before implementation",
        )
    return None


def _planning_checks_summary(checks: dict[str, Any]) -> str:
    failed = [name for name, value in checks.items() if isinstance(value, bool) and not value]
    planner_score = checks.get("planner_score")
    reviewer_score = checks.get("reviewer_score")
    score_segment = f"planner_score={planner_score}, reviewer_score={reviewer_score}"
    if failed:
        return f"failed_checks={failed}; {score_segment}"
    return score_segment


def _planning_escalation_summary(escalation: dict[str, Any]) -> str:
    category = str(escalation.get("conflict_category") or "").strip() or "unknown"
    unresolved = [dict(item) for item in list(escalation.get("unresolved_findings") or []) if isinstance(item, dict)]
    descriptions = [str(item.get("description") or "").strip() for item in unresolved if str(item.get("description") or "").strip()]
    summary = "; ".join(descriptions[:2])
    if summary:
        return f"conflict_category={category}; unresolved={summary}"
    return f"conflict_category={category}; unresolved findings remain"


def _planning_conversation_decision_spec(
    *,
    feature: str,
    conversation: dict[str, Any],
) -> dict[str, Any] | None:
    status = str(conversation.get("status") or "").strip().lower()
    approval = dict(conversation.get("approval") or {})
    approval_decision = str(approval.get("decision") or "").strip().lower()
    if status == "auto_skipped":
        return None
    if status == "precondition_blocked":
        return None
    if status == "escalation_required":
        escalation = dict(conversation.get("escalation") or {})
        return decision_spec(
            feature=feature,
            kind=DecisionKind.PLANNING_ESCALATION,
            question="方案三轮迭代后仍未收敛，是否提供人工裁决后继续？",
            context_summary=_planning_escalation_summary(escalation),
            blocking_reason="planning escalation required",
        )
    if approval_decision == "human_required":
        checks = dict(approval.get("checks") or {})
        return decision_spec(
            feature=feature,
            kind=DecisionKind.PLANNING_APPROVAL,
            question="模型规划已生成，是否人工确认后继续执行？",
            context_summary=_planning_checks_summary(checks),
            blocking_reason=str(approval.get("reason") or "planning approval required"),
        )
    return None


def release_decision_spec(feature: str) -> dict[str, Any]:
    """Legacy decision spec — kept for backward compatibility with external callers."""
    return decision_spec(
        feature=feature,
        kind=DecisionKind.RELEASE_APPROVAL,
        question="是否批准进入 release tail 并生成 ship-readiness 结论？",
        context_summary="execution/review/verify 已完成，等待最终发布审批。",
        blocking_reason="release approval required before ship-readiness",
    )


def _verify_status_from_payload(payload: dict[str, Any]) -> str:
    verify = payload.get("verify_check")
    if isinstance(verify, dict):
        status = str(verify.get("status") or "").strip().upper()
        if status:
            return status
    workflow_chain = payload.get("workflow_chain")
    if isinstance(workflow_chain, dict):
        upstream = workflow_chain.get("upstream")
        if isinstance(upstream, dict):
            verify = upstream.get("verify")
            if isinstance(verify, dict):
                status = str(verify.get("status") or "").strip().upper()
                if status:
                    return status
    unified = payload.get("unified_status")
    if isinstance(unified, dict):
        status = str(unified.get("verify") or "").strip().upper()
        if status:
            return status
    return "UNKNOWN"


def _gate_status_from_payload(payload: dict[str, Any]) -> str:
    gate = payload.get("gate_check")
    if isinstance(gate, dict):
        status = str(gate.get("total_status") or "").strip().upper()
        if status:
            return status
    workflow_chain = payload.get("workflow_chain")
    if isinstance(workflow_chain, dict):
        upstream = workflow_chain.get("upstream")
        if isinstance(upstream, dict):
            gate = upstream.get("gate")
            if isinstance(gate, dict):
                status = str(gate.get("total_status") or "").strip().upper()
                if status:
                    return status
    unified = payload.get("unified_status")
    if isinstance(unified, dict):
        status = str(unified.get("gate") or "").strip().upper()
        if status:
            return status
    return "UNKNOWN"


def _scope_drift_value(scope_payload: dict[str, Any]) -> str:
    if bool(scope_payload.get("drifted")):
        return "drifted"
    if list(scope_payload.get("out_of_scope_files") or []):
        return "drifted"
    status = str(scope_payload.get("status") or "").strip().upper()
    if status == "PASS":
        return "none"
    if status:
        return status.lower()
    return "unknown"


def _scope_drift_from_compliance_report(payload: dict[str, Any]) -> str:
    report = payload.get("compliance_report")
    if not isinstance(report, dict):
        return ""
    for item in list(report.get("checks") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("check_name") or "").strip() != "scope_drift":
            continue
        status = str(item.get("status") or "").strip().upper()
        if status == "PASS":
            return "none"
        if status:
            return status.lower()
    return ""


def _scope_drift_from_payload(payload: dict[str, Any]) -> str:
    direct = payload.get("scope_drift")
    if str(direct or "").strip():
        return str(direct).strip().lower()
    rounds = list(payload.get("rounds") or [])
    for record in reversed(rounds):
        if not isinstance(record, dict):
            continue
        details = record.get("details")
        if not isinstance(details, dict):
            continue
        scope_payload = details.get("scope_drift")
        if isinstance(scope_payload, dict):
            return _scope_drift_value(scope_payload)
    from_compliance = _scope_drift_from_compliance_report(payload)
    if from_compliance:
        return from_compliance
    unified = payload.get("unified_status")
    if isinstance(unified, dict):
        status = str(unified.get("scope_drift") or "").strip().lower()
        if status:
            return status
    return "unknown"


def _build_release_execution_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Build execution context for auto-approval evaluation from the autopilot payload."""
    changed_files = list(payload.get("changed_files") or [])
    return {
        "verify_status": _verify_status_from_payload(payload),
        "gate_status": _gate_status_from_payload(payload),
        "risk_profile": str(payload.get("risk_profile", "medium")),
        "scope_drift": _scope_drift_from_payload(payload),
        "changed_files": changed_files,
        "changed_files_count": len(changed_files),
    }


def awaiting_decision_payload(
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


def blocked_decision_payload(
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


def decision_payload_for_spec(
    *,
    args: argparse.Namespace,
    planning_dir: Path,
    planning_snapshot: AutopilotPlanningSnapshot | None,
    spec: dict[str, Any] | None,
    base_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if spec is None:
        clear_consumed_decision_artifacts(planning_dir)
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
        return awaiting_decision_payload(
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
        return blocked_decision_payload(
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
    return awaiting_decision_payload(
        args=args,
        planning_dir=planning_dir,
        planning_snapshot=planning_snapshot,
        request_payload=request_payload,
        base_payload=base_payload,
    )


def _resolved_existing_decision_response(
    *,
    args: argparse.Namespace,
    planning_dir: Path,
    planning_snapshot: AutopilotPlanningSnapshot | None,
) -> dict[str, Any] | None:
    request_payload = load_decision_request(planning_dir) or {}
    response_payload = load_decision_response(planning_dir) or {}
    if not response_matches_request(request_payload, response_payload):
        return None
    decision_id = str(request_payload.get("decision_id") or "")
    raw_kind = str(request_payload.get("decision_kind") or "")
    try:
        kind = DecisionKind(raw_kind)
    except ValueError:
        kind = None
    approved_options = decision_options(kind)[1] if kind is not None else {"approve", "ship", "resolve"}
    selected = str(response_payload.get("selected_option") or "").strip()
    if selected in approved_options:
        record_approved_decision(planning_dir, decision_id)
        clear_consumed_decision_artifacts(planning_dir)
        return None
    return blocked_decision_payload(
        base_payload=None,
        request_payload=request_payload,
        response_payload=response_payload,
    )


def autopilot_interaction_snapshot(
    *,
    planning_dir: Path,
    run_result: dict[str, Any],
    state: Any,
) -> dict[str, Any]:
    decision = decision_snapshot(planning_dir)
    unified = _effective_interaction_unified_status(run_result=run_result, state=state)
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


def _effective_interaction_unified_status(*, run_result: dict[str, Any], state: Any) -> dict[str, Any]:
    state_unified = dict(getattr(state, "get_unified_status", lambda: {})() or {})
    runtime_unified = dict(run_result.get("unified_status") or {})
    if not runtime_unified:
        return _sanitize_pass_unified_status(state_unified)
    merged = dict(state_unified)
    for key, value in runtime_unified.items():
        if value is not None:
            merged[key] = value
    return _sanitize_pass_unified_status(_sanitize_blocked_unified_status(merged))


def _sanitize_blocked_unified_status(unified: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(unified)
    final_status = str(normalized.get("final_status") or "").strip().upper()
    stop_reason = str(normalized.get("stop_reason") or "").strip().upper()
    if final_status == "BLOCKED" or stop_reason == "BLOCKED_BY_PRECONDITION":
        normalized["final_status"] = "BLOCKED"
        normalized["is_blocked"] = True
        normalized["is_terminal"] = True
        if not stop_reason:
            normalized["stop_reason"] = "BLOCKED"
    return normalized


def _sanitize_pass_unified_status(unified: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(unified)
    final_status = str(normalized.get("final_status") or "").strip().upper()
    stop_reason = str(normalized.get("stop_reason") or "").strip().upper()
    if final_status == "PASS" or stop_reason == "PASS":
        normalized["final_status"] = "PASS"
        normalized["stop_reason"] = "PASS"
        normalized["is_blocked"] = False
        normalized["blocking_reason"] = ""
    return normalized


def build_autopilot_payload(
    *,
    args: argparse.Namespace,
    planning_dir: Path,
    state_path: Path,
    rounds_path: Path,
    plan: Any,
    run_result: dict[str, Any],
    rounds: list[dict[str, Any]],
    planning_artifacts: dict[str, dict[str, Any]],
    state: Any,
    workflow_chain: dict[str, Any] | None = None,
    changed_files_source: str = "none",
    worktree_preflight: dict[str, Any] | None = None,
    planning_snapshot: AutopilotPlanningSnapshot | None = None,
) -> dict[str, Any]:
    status, final_outcome = autopilot_payload_status(
        run_result=run_result,
        workflow_chain=workflow_chain,
    )
    execution_result = dict(run_result.get("execution_result") or {})
    verify_check = dict(run_result.get("verify_check") or {})
    gate_check = dict(run_result.get("gate_check") or {})
    compliance_report = dict(run_result.get("compliance_report") or {})
    execution_backend = str(
        execution_result.get("backend")
        or run_result.get("execution_backend")
        or ""
    ).strip()
    execution_backend_capabilities = dict(
        execution_result.get("backend_capabilities")
        or run_result.get("execution_backend_capabilities")
        or {}
    )
    parallel_runtime = dict(run_result.get("parallel_runtime") or {})
    effective_unified = _effective_interaction_unified_status(run_result=run_result, state=state)
    interaction = autopilot_interaction_snapshot(
        planning_dir=planning_dir,
        run_result=run_result,
        state=state,
    )
    return {
        "status": status,
        "entrypoint": "kodawari autopilot",
        "planning_contract_version": MERGED_CONTRACT_VERSION,
        "feature": args.feature,
        "planning_dir": str(planning_dir),
        "state_path": str(state_path),
        "rounds_path": str(rounds_path),
        "estimated_cycles": getattr(plan, "estimated_cycles", None),
        "estimated_tokens": getattr(plan, "estimated_tokens", None),
        "run_reason": run_result.get("reason"),
        "rounds_executed": len(rounds),
        "changed_files_source": changed_files_source,
        "worktree_preflight": dict(worktree_preflight or {}),
        "planning_artifact_mode": "contract_first" if planning_snapshot is not None else "legacy",
        "planning_artifacts": planning_artifacts,
        "planning_contract": build_planning_contract_summary(planning_artifacts),
        "planning_snapshot": planning_snapshot.to_dict() if planning_snapshot is not None else {},
        "unified_status": effective_unified,
        "tokens_used": int(run_result.get("tokens_used", 0) or 0),
        "token_budget": run_result.get("token_budget"),
        "budget_exhausted": bool(run_result.get("budget_exhausted", False)),
        "collaboration_context": run_result.get("collaboration_context"),
        "execution_backend": execution_backend,
        "execution_backend_capabilities": execution_backend_capabilities,
        "parallel_runtime": parallel_runtime,
        "execution_artifacts": dict(run_result.get("execution_artifacts") or {}),
        "execution_result": execution_result,
        "verify_check": verify_check,
        "gate_check": gate_check,
        "compliance_report": compliance_report,
        "workflow_chain": workflow_chain,
        "risk_profile": str(run_result.get("risk_profile", "medium")),
        "changed_files": sorted(getattr(state, "changed_files", None) or []),
        "changed_files_count": len(getattr(state, "changed_files", None) or []),
        "scope_drift": _scope_drift_from_payload(
            {
                "rounds": list(run_result.get("rounds") or []),
                "compliance_report": compliance_report,
                "unified_status": effective_unified,
            }
        ),
        "final_outcome": final_outcome,
        **interaction,
    }


def maybe_pause_for_planning_decision(
    *,
    args: argparse.Namespace,
    planning_dir: Path,
    planning_snapshot: AutopilotPlanningSnapshot | None,
    policy: Any | None = None,
) -> dict[str, Any] | None:
    resolved_response = _resolved_existing_decision_response(
        args=args,
        planning_dir=planning_dir,
        planning_snapshot=planning_snapshot,
    )
    if resolved_response is not None:
        return resolved_response
    stage_profile = str(getattr(planning_snapshot, "stage_profile", "") or "").strip()
    if stage_profile in {"take_task", "recovery"}:
        clear_consumed_decision_artifacts(planning_dir)
        return None
    # Explicit tier with auto-skip never blocks on a historical conversation state.
    if policy is not None and str(getattr(policy, "decision_policy", "") or "") == "auto-skip":
        return None
    has_model_conversation = bool(
        planning_snapshot is not None
        and str(dict(planning_snapshot.artifacts).get("PLANNING_CONVERSATION.json") or "").strip()
    )
    if planning_snapshot is None or (not has_model_conversation and not explicit_planning_input_requested(args)):
        return None
    return decision_payload_for_spec(
        args=args,
        planning_dir=planning_dir,
        planning_snapshot=planning_snapshot,
        spec=planning_decision_spec(
            feature=str(args.feature),
            planning_snapshot=planning_snapshot,
        ),
    )


def _apply_release_tail_result(
    *,
    payload: dict[str, Any],
    release_tail: dict[str, Any],
    feature: str,
    decision_present: bool,
) -> tuple[dict[str, Any], int | None]:
    payload["release_tail"] = release_tail
    # decision_request_present tracks the PENDING state per contract; once
    # release tail is being applied the decision has been resolved one way or
    # another (auto-approve or explicit approval), so it is no longer pending.
    snapshot_kwargs = {
        "decision_pending": False,
        "decision_request_present": False,
        "final_status": "PASS",
        "stop_reason": "PASS",
        "blocked": False,
        "is_terminal": True,
    }
    if decision_present:
        snapshot_kwargs["decision_kind"] = DecisionKind.RELEASE_APPROVAL.value
        snapshot_kwargs["decision_id"] = f"{feature}:{DecisionKind.RELEASE_APPROVAL.value}"
    if str(release_tail.get("status") or "PASS").upper() == "PASS":
        payload.update(build_interaction_snapshot(**snapshot_kwargs))
        return payload, None
    payload["status"] = "blocked"
    payload["blocking_reason"] = str(release_tail.get("blocking_reason") or "release tail blocked")
    payload.update(
        build_interaction_snapshot(
            **{
                **snapshot_kwargs,
                "final_status": "BLOCKED",
                "stop_reason": "BLOCKED",
                "blocked": True,
            }
        )
    )
    return payload, 1


def _policy_auto_eval(command_runtime: dict[str, Any]) -> bool:
    """C9: auto-invoke telemetry+eval-report when policy says eval is required.

    Falls back to False (matches pre-C9 behavior) when policy is absent or
    when policy is informational only (policy_active=False).
    """
    if not command_runtime.get("policy_active"):
        return False
    policy = command_runtime.get("workflow_policy")
    if policy is None:
        return False
    return bool(getattr(policy, "eval_required", False))


def maybe_run_release_tail(
    *,
    args: argparse.Namespace,
    command_runtime: dict[str, Any],
    payload: dict[str, Any],
    run_release_tail: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], int | None]:
    policy = command_runtime.get("workflow_policy")
    policy_active = bool(command_runtime.get("policy_active"))
    if policy_active and policy is not None and not policy.release_tail_enabled:
        payload["release_tail"] = {
            "status": "skipped",
            "reason": "policy.release_tail_enabled=false",
            "effective_tier": policy.effective_tier,
        }
        return payload, None
    planning_snapshot = command_runtime["planning_snapshot"]
    has_model_conversation = bool(
        planning_snapshot is not None
        and str(dict(planning_snapshot.artifacts).get("PLANNING_CONVERSATION.json") or "").strip()
    )
    if payload.get("status") != "ok":
        return payload, None
    # Fix F3: Legacy/resume (planning_snapshot=None) — skip decision flow
    # but still run release_tail when the run actually completed successfully.
    if planning_snapshot is None:
        run_reason = str(payload.get("run_reason") or "").upper()
        if run_reason in {"PROCEED_TO_GATE", "PIPELINE_FINISH"}:
            risk_profile = str(payload.get("risk_profile", "medium"))
            config = AutopilotReleaseTailConfig(
                risk_profile=risk_profile,
                auto_eval=_policy_auto_eval(command_runtime),
            )
            release_tail = run_release_tail(
                project_root=command_runtime["project_root"],
                planning_dir=command_runtime["planning_dir"],
                feature=command_runtime["feature"],
                config=config,
            )
            # Legacy/resume path: store result for information only, do not enforce blocking.
            # The exit code is always 0; enforcement belongs to contract-first runs.
            payload["release_tail"] = release_tail
        return payload, None
    if not has_model_conversation and not explicit_planning_input_requested(args):
        return payload, None
    # Auto-approval check for contract-first paths.
    execution_context = _build_release_execution_context(payload)
    spec = _bridge_release_decision_spec(
        str(args.feature),
        execution_context=execution_context,
        project_root=command_runtime.get("project_root"),
    )
    # spec is None → auto-approved, skip decision block
    if spec is not None:
        decision_payload = decision_payload_for_spec(
            args=args,
            planning_dir=command_runtime["planning_dir"],
            planning_snapshot=planning_snapshot,
            spec=spec,
            base_payload=payload,
        )
        if decision_payload is not None:
            return decision_payload, 0 if decision_payload.get("status") == "awaiting_decision" else 1
    # Fix 2: Pass risk_profile to release tail config.
    risk_profile = str(payload.get("risk_profile", "medium"))
    config = AutopilotReleaseTailConfig(risk_profile=risk_profile)
    release_tail = run_release_tail(
        project_root=command_runtime["project_root"],
        planning_dir=command_runtime["planning_dir"],
        feature=command_runtime["feature"],
        config=config,
    )
    # spec is None → auto-approved, no .decision_request.json written to disk
    return _apply_release_tail_result(
        payload=payload,
        release_tail=release_tail,
        feature=str(args.feature),
        decision_present=False,
    )


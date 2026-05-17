"""Model-driven planning orchestrator."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from kodawari.autopilot.core.model_config import WorkflowTransportConfig
from kodawari.autopilot.planning.planning_agent import (
    _check_acyclic,
    _upstream_new_files_by_task,
    _validate_plan,
    generate_plan,
)
from kodawari.autopilot.planning.planning_consistency import (
    detect_docs_only_without_test_coverage,
    validate_plan_consistency,
    validate_plan_revision,
)
from kodawari.autopilot.planning.planning_context import build_file_manifest, collect_planning_context, render_context_for_prompt, resolve_plan_paths
from kodawari.autopilot.planning.context_scout import build_context_scout_payload, context_scout_enabled
from kodawari.autopilot.planning.deterministic_repair import (
    apply_deterministic_repairs,
    previous_findings_with_deterministic_refs,
)
from kodawari.autopilot.planning.execution_readiness import (
    evaluate_plan_execution_readiness,
    write_execution_readiness,
)
from kodawari.autopilot.planning.planning_validators import (
    check_missing_source_files,
    normalize_planning_path,
    path_comparison_is_case_insensitive,
    planning_path_key,
    validate_evidence_resolutions,
)
from kodawari.autopilot.planning.plan_reviewer import review_plan
from kodawari.autopilot.planning.active_scope import derive_active_scope
from kodawari.autopilot.planning.review_evidence_scout import build_review_evidence_pack
from kodawari.autopilot.planning.task_input_feasibility import (
    build_infeasibility_escalation,
    evaluate_task_input_feasibility,
)
from kodawari.autopilot.planning.planning_artifacts import (
    _split_tasks_if_needed,
    plan_to_task_cards,
    plan_to_task_graph,
    result_to_artifact,
)
from kodawari.autopilot.planning.task_card import build_task_card
from kodawari.autopilot.planning.planning_findings import (  # noqa: F401
    DEFAULT_BLOCKING_SEVERITIES,
    HIGH_HARD_STOP_CATEGORIES,
    HIGH_HARD_STOP_TERMS,
    META_BLOCKER_CANONICAL_CATEGORY,
    META_BLOCKER_LATE_ROUND_RECOVERY_REASON,
    META_BLOCKER_PLANNER_SCORE_FLOOR,
    META_BLOCKER_REVIEWER_SCORE_FLOOR,
    META_BLOCKER_STREAK_LIMIT,
    META_BLOCKER_STREAK_REASON,
    PLANNER_ENVIRONMENT_ERROR_KINDS,
    SOFT_EXECUTION_GUIDANCE_SEVERITIES,
    SOFT_GATE_ESCALATION_SEVERITIES,
    _CHAT_KIND_TO_PLANNER_ERROR_KIND,
    _all_findings,
    _attach_soft_findings_to_plan_tasks,
    _best_clean_round,
    _blocking_findings,
    _clean_text,
    classify_findings_by_active_scope,
    _dedupe_findings,
    _dict_list,
    demote_findings_already_repaired,
    demote_meta_blocker_findings_to_info,
    derive_active_scope_review_view,
    is_meta_blocker_finding,
    _finding_guidance_text,
    _finding_signature,
    _finding_text_for_policy,
    _finding_token_bag,
    _high_finding_requires_hard_stop,
    _is_blocking_finding,
    _mentioned_task_ids,
    _module_boundary_signature,
    _plan_feedback_signature,
    _plan_tasks,
    _review_findings,
    _round_findings,
    _round_is_selectable_clean,
    _round_quality_key,
    _severity,
    _severity_counts,
    _soft_execution_guidance_findings,
    _soft_gate_requires_escalation,
    _string_list,
    _task_feedback_signature,
    _unresolved_findings,
    _utc_now_iso,
    _verify_recipe_signature,
)

SCHEMA_VERSION = "planning.conversation.v1"
TASK_GRAPH_SCHEMA_VERSION = "contract_first.task_graph.v1"
PLANNING_PROGRESS_SCHEMA_VERSION = "planning.progress.v1"
PLANNING_IN_PROGRESS_FILENAME = ".planning_in_progress.json"
PLANNING_FAILURE_FILENAME = ".planning_failure.json"

@dataclass
class PlanningConfig:
    planner_executable: str = "claude"
    reviewer_executable: str = "codex"
    planner_transport: WorkflowTransportConfig | None = None
    plan_reviewer_transport: WorkflowTransportConfig | None = None
    planner_driver: str = "claude_cli"
    reviewer_driver: str = "codex_cli"
    planner_base_url: str = ""
    planner_api_key_env: str = ""
    planner_api_format: str = ""
    planner_context_max_chars: int = 0
    planner_timeout_seconds: int = 300
    reviewer_timeout_seconds: int = 180
    planner_model: str = ""
    reviewer_model: str = ""
    max_rounds: int = 3
    deadlock_streak_limit: int = 2
    auto_approve_enabled: bool = True
    blocking_severities: frozenset[str] = field(default_factory=lambda: DEFAULT_BLOCKING_SEVERITIES)
    decision_policy: str = "strict-gate"
    task_splitter_enabled: bool = False

    def __post_init__(self) -> None:
        if self.planner_transport is not None:
            self.planner_driver = self.planner_transport.driver or self.planner_driver
            executable = self.planner_transport.primary_executable()
            if executable:
                self.planner_executable = executable
            if self.planner_transport.base_url and not self.planner_base_url:
                self.planner_base_url = self.planner_transport.base_url
            if self.planner_transport.api_key_env and not self.planner_api_key_env:
                self.planner_api_key_env = self.planner_transport.api_key_env
            if self.planner_transport.api_format and not self.planner_api_format:
                self.planner_api_format = self.planner_transport.api_format
        if self.plan_reviewer_transport is not None:
            self.reviewer_driver = self.plan_reviewer_transport.driver or self.reviewer_driver
            executable = self.plan_reviewer_transport.primary_executable()
            if executable:
                self.reviewer_executable = executable

def _effective_blocking_severities(config: PlanningConfig) -> frozenset[str]:
    severities = set(config.blocking_severities) | set(SOFT_GATE_ESCALATION_SEVERITIES)
    if config.decision_policy in {"soft-gate", "auto-skip"}:
        severities.discard("high")
    return frozenset(severities)

@dataclass
class PlanningRound:
    round_number: int
    plan_payload: dict[str, Any]
    review_payload: dict[str, Any] | None
    review_error: str
    structural_issues: list[str]
    blocking_findings_count: int
    timestamp: str
    path_resolution: dict[str, Any] = field(default_factory=dict)
    planner_error: str = ""
    planner_diagnostics: dict[str, Any] = field(default_factory=dict)
    deterministic_repairs: list[dict[str, Any]] = field(default_factory=list)
    planning_readiness: dict[str, Any] = field(default_factory=dict)
    blocking_findings: list[dict[str, Any]] = field(default_factory=list)
    review_evidence_pack: dict[str, Any] = field(default_factory=dict)
    demoted_repaired_findings: list[dict[str, Any]] = field(default_factory=list)

@dataclass
class PlanningResult:
    status: str
    task_direction: str
    rounds: list[PlanningRound]
    final_plan: dict[str, Any]
    final_review: dict[str, Any] | None
    approval: dict[str, Any]
    escalation: dict[str, Any] | None
    business_outcome: str
    out_of_scope: list[str]
    source_of_truth: list[str]
    source_of_truth_canonical: list[str]
    path_type: str
    layers: list[str]
    coverage_hints: list[str]
    module_boundaries: list[dict[str, Any]]
    verify_recipes: list[dict[str, Any]]
    approval_points: list[dict[str, Any]]
    execution_constraints: dict[str, Any]
    confidence: str
    confidence_issues: list[str]
    archetype: str
    capabilities: list[str]
    input_fingerprint: str
    context_scout: dict[str, Any] = field(default_factory=dict)
    prompt_lesson_learning: dict[str, Any] = field(default_factory=dict)
    planning_readiness: dict[str, Any] = field(default_factory=dict)
    final_review_active_scope: dict[str, Any] | None = None
    meta_blocker_demotion_log: list[dict[str, Any]] = field(default_factory=list)

def _extract_contract_fields(
    *,
    plan_payload: dict[str, Any],
    task_direction: str,
    repo_inventory: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any]:
    tasks = _plan_tasks(plan_payload)
    layers = _string_list(plan_payload.get("layers")) or [
        _clean_text(item.get("layer_owner"))
        for item in tasks
        if _clean_text(item.get("layer_owner"))
    ]
    deduped_layers: list[str] = []
    for layer in layers:
        text = _clean_text(layer).lower()
        if text and text not in deduped_layers:
            deduped_layers.append(text)
    coverage_hints = _string_list(plan_payload.get("coverage_hints"))
    if not coverage_hints:
        for task in tasks:
            for hint in _string_list(task.get("coverage_hints")):
                if hint not in coverage_hints:
                    coverage_hints.append(hint)
    source_of_truth = _string_list(plan_payload.get("source_of_truth"))
    source_of_truth_canonical = _string_list(plan_payload.get("source_of_truth_canonical")) or list(source_of_truth)
    module_boundaries = _dict_list(plan_payload.get("module_boundaries"))
    if not module_boundaries:
        for task in tasks:
            roots = _string_list(task.get("files_to_change"))
            module_boundaries.append(
                {
                    "name": _clean_text(task.get("task_id")) or _clean_text(task.get("task_name")) or "module",
                    "surface": _clean_text(task.get("surface")) or "backend",
                    "roots": roots,
                    "layers": [_clean_text(task.get("layer_owner")).lower() or "service"],
                }
            )
    verify_recipes = _dict_list(plan_payload.get("verify_recipes"))
    if not verify_recipes:
        by_surface: dict[str, str] = {}
        for task in tasks:
            surface = _clean_text(task.get("surface")) or "backend"
            verify_cmd = _clean_text(task.get("verify_cmd") or task.get("test_plan"))
            if surface not in by_surface and verify_cmd:
                by_surface[surface] = verify_cmd
        verify_recipes = [
            {"surface": surface, "command": command, "required": True, "roots": []}
            for surface, command in by_surface.items()
        ]
    return {
        "business_outcome": _clean_text(plan_payload.get("business_outcome")) or _clean_text(task_direction),
        "out_of_scope": _string_list(plan_payload.get("out_of_scope")),
        "source_of_truth": source_of_truth,
        "source_of_truth_canonical": source_of_truth_canonical,
        "path_type": _clean_text(plan_payload.get("path_type")).lower() or "read",
        "layers": deduped_layers or ["service", "repository", "route"],
        "coverage_hints": coverage_hints,
        "module_boundaries": module_boundaries,
        "verify_recipes": verify_recipes,
        "approval_points": _dict_list(plan_payload.get("approval_points")),
        "execution_constraints": dict(plan_payload.get("execution_constraints") or {}),
        "confidence": _clean_text(plan_payload.get("confidence")).lower() or "high",
        "confidence_issues": _string_list(plan_payload.get("confidence_issues")),
        "archetype": _clean_text(plan_payload.get("archetype") or repo_inventory.get("archetype")) or "auto",
        "capabilities": _string_list(plan_payload.get("capabilities") or repo_inventory.get("capabilities")),
        "input_fingerprint": fingerprint,
    }

def _progress(msg: str) -> None:
    """Emit a progress line to stderr so the user can see what stage is running."""
    sys.stderr.write(f"[planning] {msg}\n")
    sys.stderr.flush()

def _safe_write_json(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return

def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return

def _round_progress_payload(round_item: PlanningRound) -> dict[str, Any]:
    diagnostics = dict(round_item.planner_diagnostics or {})
    stored_blocking = [dict(item) for item in list(getattr(round_item, "blocking_findings", []) or []) if isinstance(item, dict)]
    if not stored_blocking:
        stored_blocking = _round_findings(round_item, threshold=DEFAULT_BLOCKING_SEVERITIES)
    blocking_findings = _compact_findings(stored_blocking)
    return {
        "round_number": int(round_item.round_number),
        "timestamp": _clean_text(round_item.timestamp),
        "has_plan": bool(round_item.plan_payload),
        "planner_error": _clean_text(round_item.planner_error),
        "review_error": _clean_text(round_item.review_error),
        "blocking_findings_count": int(round_item.blocking_findings_count),
        "blocking_findings": blocking_findings,
        "structural_issues_count": len(list(round_item.structural_issues or [])),
        "structural_issues": [str(item)[:500] for item in list(round_item.structural_issues or [])[:8]],
        "planning_readiness_status": _clean_text(
            dict(round_item.planning_readiness or {}).get("status")
        ),
        "review_evidence_pack": dict(getattr(round_item, "review_evidence_pack", {}) or {}),
        "planner_diagnostics": {
            key: diagnostics.get(key)
            for key in (
                "planner_error_kind",
                "request_bytes",
                "response_bytes",
                "wallclock_ms",
                "context_chars",
                "context_budget",
                "planner_fallback_used",
                "planner_fallback_attempted",
                "planner_fallback_kind",
                "planner_fallback_reason",
                "fallback_context_chars",
            )
            if key in diagnostics
        },
    }

def _compact_findings(findings: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in findings[: max(0, int(limit or 0))]:
        compact.append(
            {
                "severity": _severity(item.get("severity")),
                "category": _clean_text(item.get("category")),
                "description": _clean_text(item.get("description"))[:800],
                "recommendation": _clean_text(item.get("recommendation"))[:800],
                "source": _clean_text(item.get("source")),
            }
        )
    return compact

def _write_planning_progress(
    *,
    planning_dir: Path,
    planning_run_id: str,
    feature: str,
    task_direction: str,
    status: str,
    stage: str,
    rounds: list[PlanningRound],
    context_chars: int = 0,
    context_budget: int = 0,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": PLANNING_PROGRESS_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "planning_run_id": planning_run_id,
        "feature": _clean_text(feature),
        "task_direction": _clean_text(task_direction),
        "status": _clean_text(status),
        "stage": _clean_text(stage),
        "round_count": len(rounds),
        "rounds": [_round_progress_payload(item) for item in rounds],
        "context_chars": int(context_chars or 0),
        "context_budget": int(context_budget or 0),
    }
    if extra:
        payload.update(dict(extra))
    _safe_write_json(Path(planning_dir) / PLANNING_IN_PROGRESS_FILENAME, payload)

def _write_planning_failure(
    *,
    planning_dir: Path,
    planning_run_id: str,
    feature: str,
    task_direction: str,
    status: str,
    reason: str,
    rounds: list[PlanningRound],
    escalation: dict[str, Any] | None = None,
    planning_readiness: dict[str, Any] | None = None,
) -> None:
    escalation_payload = dict(escalation or {})
    error_code = (
        _clean_text(escalation_payload.get("termination_reason"))
        or _clean_text(escalation_payload.get("gate_reason"))
        or _clean_text(reason)
    )
    payload = {
        "schema_version": PLANNING_PROGRESS_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "planning_run_id": planning_run_id,
        "feature": _clean_text(feature),
        "task_direction": _clean_text(task_direction),
        "status": _clean_text(status),
        "reason": _clean_text(reason),
        "error_code": error_code,
        "round_count": len(rounds),
        "rounds": [_round_progress_payload(item) for item in rounds],
        "escalation": escalation_payload,
        "planning_readiness": dict(planning_readiness or {}),
    }
    _safe_write_json(Path(planning_dir) / PLANNING_FAILURE_FILENAME, payload)

def _planning_readiness_blocked(readiness: dict[str, Any] | None) -> bool:
    return _clean_text(dict(readiness or {}).get("status")).upper() == "BLOCKED"

def _ambiguous_resolution_signature(
    plan_payload: dict[str, Any],
) -> tuple[str, ...]:
    """Stable signature of the planner's still-unresolved evidence resolutions.

    Returns the sorted set of ``finding_id``s where the planner reported
    ``status=ambiguous`` in this round. The orchestrator compares the
    signature across rounds to detect a deadlock (same set of unresolved
    findings repeating); once the same signature appears for the
    ``AMBIGUOUS_STREAK_LIMIT``-th time, planning escalates as
    ``planning_evidence_blocked`` rather than burning more rounds. A round
    that closes one finding and adds another resets the streak by changing
    the signature.

    Replaces the earlier ``_needs_human_evidence_decision`` early-terminate
    path. That path treated any ``needs_human_decision`` resolution as a
    hard stop, but combined with the validator's interlock that status was
    the only legal answer for some findings — so a single review pack
    request would unconditionally terminate the run. The streak detector
    keeps the bounded-rounds property without that footgun.
    """
    sigs: list[str] = []
    for item in list(dict(plan_payload or {}).get("evidence_resolutions") or []):
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("status")).lower() != "ambiguous":
            continue
        finding_id = _clean_text(item.get("finding_id"))
        if finding_id:
            sigs.append(finding_id)
    return tuple(sorted(sigs))


# Streak threshold for ambiguous evidence resolutions. After this many
# rounds with the same set of ambiguous finding_ids, planning escalates as
# planning_evidence_blocked. Two rounds is the minimum that distinguishes
# "planner needs another pass to gather evidence" from "planner is stuck".
AMBIGUOUS_STREAK_LIMIT = 2
AMBIGUOUS_ANY_STREAK_LIMIT = 3

def _precondition_finding(readiness: dict[str, Any]) -> dict[str, Any]:
    missing = _string_list(readiness.get("missing_preconditions"))
    blocked_tasks = _string_list(readiness.get("blocked_tasks"))
    task_text = f" for task(s) {', '.join(blocked_tasks)}" if blocked_tasks else ""
    missing_text = ", ".join(missing) if missing else "required preconditions"
    recommendation = _clean_text(readiness.get("suggested_next_task")) or (
        "Add a predecessor task that satisfies the missing preconditions, "
        "or revise the plan to use only existing repo-backed inputs."
    )
    return {
        "severity": "blocking",
        "category": "precondition",
        "description": f"Execution preconditions are missing{task_text}: {missing_text}",
        "recommendation": recommendation,
        "source": "execution_readiness",
    }

def _build_precondition_escalation(
    rounds: list[PlanningRound],
    *,
    readiness: dict[str, Any],
) -> dict[str, Any]:
    payload = _build_escalation(rounds, threshold=DEFAULT_BLOCKING_SEVERITIES) or {}
    payload["gate_reason"] = "blocked_by_precondition"
    payload["termination_reason"] = "blocked_by_precondition"
    payload["planning_readiness"] = dict(readiness)
    return payload


def _early_exit_for_input_infeasibility(
    *,
    planning_dir: Path,
    planning_run_id: str,
    feature: str,
    task_direction: str,
    feasibility: dict[str, Any],
    context_scout_payload: dict[str, Any],
) -> "PlanningResult":
    """Layer D early-exit path. Escalation payload is built by the precheck
    module; here we just write the failure artifact and wrap a minimal
    PlanningResult.
    """
    escalation = build_infeasibility_escalation(feasibility)
    _write_planning_failure(
        planning_dir=planning_dir,
        planning_run_id=planning_run_id,
        feature=feature,
        task_direction=task_direction,
        status="escalation_required",
        reason="task_input_infeasible_surface",
        rounds=[],
        escalation=escalation,
    )
    return PlanningResult(
        status="escalation_required",
        task_direction=task_direction,
        rounds=[],
        final_plan={},
        final_review=None,
        approval={
            "decision": "escalation_required",
            "reason": "task_input_infeasible_surface",
        },
        escalation=escalation,
        business_outcome="",
        out_of_scope=[],
        source_of_truth=[],
        source_of_truth_canonical=[],
        path_type="",
        layers=[],
        coverage_hints=[],
        module_boundaries=[],
        verify_recipes=[],
        approval_points=[],
        execution_constraints={},
        confidence="",
        confidence_issues=[],
        archetype="",
        capabilities=[],
        input_fingerprint="",
        context_scout=dict(context_scout_payload),
    )

def _planner_context_max_chars(config: PlanningConfig) -> int:
    if int(config.planner_context_max_chars or 0) > 0:
        return max(2000, int(config.planner_context_max_chars))
    return 0

def _is_http_chat_planner(config: PlanningConfig) -> bool:
    transport = config.planner_transport
    if transport is None:
        return bool(config.planner_base_url or config.planner_api_key_env or config.planner_api_format)
    kind = _clean_text(transport.kind).lower().replace("-", "_")
    interface = _clean_text(transport.interface).lower().replace("-", "_")
    return kind == "http" and interface == "chat"

def _planner_context_fallback_budgets(config: PlanningConfig) -> list[int]:
    configured = _planner_context_max_chars(config)
    if configured > 0:
        return [configured]
    raw = _clean_text(os.environ.get("WORKFLOW_PLANNER_CONTEXT_FALLBACK_CHARS"))
    if raw:
        values: list[int] = []
        for item in re.split(r"[\s,;]+", raw):
            token = _clean_text(item).lower()
            if not token:
                continue
            if token in {"0", "full", "unlimited", "none"}:
                values.append(0)
                continue
            try:
                parsed = int(token)
            except ValueError:
                continue
            if parsed > 0:
                values.append(max(2000, parsed))
        deduped = list(dict.fromkeys(values))
        return deduped or [0]
    if _is_http_chat_planner(config):
        return [0, 96000, 32000, 12000]
    return [0]

def _render_planner_context_for_budget(context: dict[str, Any], budget: int) -> str:
    if int(budget or 0) > 0:
        return render_context_for_prompt(context, max_chars=int(budget))
    return render_context_for_prompt(context, max_chars=0)

def _planner_context_pressure(round_item: PlanningRound) -> bool:
    diagnostics = dict(round_item.planner_diagnostics or {})
    kind = _clean_text(diagnostics.get("chat_kind")).lower()
    request_bytes = int(diagnostics.get("request_bytes") or 0)
    if kind == "context_overflow":
        return True
    if kind in {"http_timeout", "remote_closed"} and request_bytes >= 16_000:
        return True
    return False

def _planner_environment_error_kind(round_item: PlanningRound) -> str:
    if round_item.plan_payload:
        return ""
    diagnostics = dict(round_item.planner_diagnostics or {})
    kind = _clean_text(diagnostics.get("planner_error_kind")).lower()
    if not kind:
        chat_kind = _clean_text(diagnostics.get("chat_kind")).lower()
        kind = _CHAT_KIND_TO_PLANNER_ERROR_KIND.get(chat_kind, chat_kind)
    if not kind:
        text = _clean_text(round_item.planner_error or round_item.review_error).lower()
        if "max-turns" in text or "max turns" in text:
            kind = "max_turns"
        elif "context overflow" in text or "context length" in text:
            kind = "planner_context_overflow"
        elif "timed out" in text or "timeout" in text:
            kind = "timeout"
        elif "forbidden" in text or "403" in text:
            kind = "auth_forbidden"
        elif "not logged in" in text or "missing credentials" in text:
            kind = "auth_missing"
        elif "executable could not start" in text:
            kind = "executable_missing"
        elif "nested" in text:
            kind = "nested_session"
        elif "home" in text and "eperm" in text:
            kind = "home_access_error"
    return kind if kind in PLANNER_ENVIRONMENT_ERROR_KINDS else ""

def _build_environment_escalation(rounds: list[PlanningRound], *, kind: str) -> dict[str, Any]:
    escalation = _build_escalation(
        rounds,
        threshold=None,
        termination_reason=f"planner_environment_error:{kind}",
    ) or {}
    escalation["gate_reason"] = "planner_environment_error"
    escalation["environment_error_kind"] = kind
    return escalation

def _reviewer_error_kind(round_item: PlanningRound) -> str:
    if not round_item.plan_payload or round_item.review_payload is not None:
        return ""
    text = _clean_text(round_item.review_error).lower()
    if not text:
        return ""
    if "timed out" in text or "timeout" in text:
        return "reviewer_timeout"
    if "forbidden" in text or "403" in text or "authentication failed" in text:
        return "reviewer_auth"
    if "not valid json" in text or "valid json" in text or "invalid json" in text:
        return "reviewer_invalid_json"
    if "empty output" in text:
        return "reviewer_empty_output"
    if "failed to start" in text or "not supported" in text:
        return "reviewer_environment"
    return "reviewer_error"

_TOOL_USE_CHAT_FALLBACK_KINDS = frozenset(
    {
        "planner_transport_timeout",
        "planner_output_truncated_empty",
        "planner_empty_output",
        # Mimo tool_use serializer at 60K+ context occasionally returns
        # malformed JSON that the planner cannot parse. Chat-fallback mode
        # (no tools) is materially more resilient on large prompts because
        # it does not need to satisfy tool_use schema constraints, so route
        # both invalid-JSON variants through the same recovery path.
        "planner_tool_use_invalid_json",
        "planner_tool_use_checkpoint_invalid_json",
    }
)


def _tool_use_chat_fallback_enabled() -> bool:
    raw = str(os.environ.get("WORKFLOW_PLANNER_TOOL_USE_CHAT_FALLBACK", "")).strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _planner_fallback_context_limit() -> int:
    raw = str(os.environ.get("WORKFLOW_PLANNER_TOOL_USE_FALLBACK_CONTEXT_CHARS", "")).strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return max(4_000, min(parsed, 96_000))
    return 32_000


def _compact_tool_use_fallback_context(context_text: str) -> str:
    limit = _planner_fallback_context_limit()
    text = str(context_text or "")
    if len(text) <= limit:
        return text
    marker = "\n\n[... planner tool-use fallback compacted middle context ...]\n\n"
    head = max(1_000, int((limit - len(marker)) * 0.7))
    tail = max(1_000, limit - len(marker) - head)
    return f"{text[:head]}{marker}{text[-tail:]}"


def _tool_use_chat_fallback_kind(config: PlanningConfig, diagnostics: dict[str, Any]) -> str:
    if not _tool_use_chat_fallback_enabled():
        return ""
    transport = config.planner_transport
    if transport is None:
        return ""
    if _clean_text(transport.kind).lower().replace("-", "_") != "http":
        return ""
    if _clean_text(transport.interface).lower().replace("-", "_") != "tool_use":
        return ""
    if _clean_text(diagnostics.get("transport_kind")) != "http_tool_use":
        return ""
    kind = _clean_text(diagnostics.get("planner_error_kind")).lower()
    return kind if kind in _TOOL_USE_CHAT_FALLBACK_KINDS else ""


def _chat_fallback_transport(transport: WorkflowTransportConfig) -> WorkflowTransportConfig:
    return WorkflowTransportConfig(
        name=f"{transport.name or 'planner'}_chat_fallback",
        kind=transport.kind,
        driver=transport.driver,
        interface="chat",
        executable=transport.executable,
        host_executable=transport.host_executable,
        api_format=transport.api_format or "openai_chat",
        base_url=transport.base_url,
        base_url_env=transport.base_url_env,
        api_key_env=transport.api_key_env,
        mcp_server=transport.mcp_server,
        quota_group=transport.quota_group,
        provides=["interface.chat"],
    )


def _build_reviewer_escalation(rounds: list[PlanningRound], *, kind: str) -> dict[str, Any]:
    escalation = _build_escalation(
        rounds,
        threshold=None,
        termination_reason=f"plan_reviewer_error:{kind}",
    ) or {}
    escalation["gate_reason"] = "plan_reviewer_error"
    escalation["reviewer_error_kind"] = kind
    return escalation

def _best_round_can_replace_escalation(
    *,
    config: PlanningConfig,
    escalation: dict[str, Any] | None,
) -> bool:
    if config.decision_policy not in {"soft-gate", "auto-skip"}:
        return False
    payload = dict(escalation or {})
    if payload.get("gate_reason") in {"planner_environment_error", "plan_reviewer_error"}:
        return False
    termination = _clean_text(payload.get("termination_reason"))
    if termination.startswith(("planner_environment_error", "plan_reviewer_error")):
        return False
    if termination == "stubborn_round_limit":
        return False
    return True

def _run_round(
    *,
    config: PlanningConfig,
    task_direction: str,
    context_text: str,
    context_budget: int,
    project_root: Path,
    file_manifest: dict[str, list[str]],
    previous_findings: list[dict[str, Any]] | None,
    previous_plan: dict[str, Any] | None = None,
    round_number: int,
    precondition_replan_hint: dict[str, Any] | None = None,
    review_evidence_packs: list[dict[str, Any]] | None = None,
    planning_dir: Path | None = None,
    resolved_findings_log: list[dict[str, Any]] | None = None,
    planning_mode: str = "existing",
) -> PlanningRound:
    planner_label = (
        config.planner_transport.name
        if config.planner_transport is not None
        else (config.planner_driver or config.planner_executable)
    )
    _progress(f"round {round_number}: calling planner ({planner_label})...")
    planner_diagnostics: dict[str, Any] = {}
    plan_payload, plan_error = generate_plan(
        executable=config.planner_executable,
        task_direction=task_direction,
        context_text=context_text,
        previous_findings=previous_findings,
        previous_plan=previous_plan,
        round_number=round_number,
        timeout_seconds=config.planner_timeout_seconds,
        model=config.planner_model,
        driver=config.planner_driver,
        base_url=config.planner_base_url,
        api_key_env=config.planner_api_key_env,
        api_format=config.planner_api_format,
        transport=config.planner_transport,
        diagnostics_out=planner_diagnostics,
        project_root=project_root,
        planning_dir=planning_dir,
        planning_mode=planning_mode,
    )
    planner_diagnostics.setdefault("context_chars", len(context_text))
    planner_diagnostics.setdefault("context_budget", int(context_budget or 0))
    fallback_kind = _tool_use_chat_fallback_kind(config, planner_diagnostics)
    if plan_payload is None and fallback_kind and config.planner_transport is not None:
        original_error = plan_error or "planner failed"
        original_diagnostics = dict(planner_diagnostics)
        fallback_context = _compact_tool_use_fallback_context(context_text)
        fallback_diagnostics: dict[str, Any] = {}
        _progress(
            f"round {round_number}: planner tool-use failed with {fallback_kind}; "
            "retrying once with compact no-tools chat fallback"
        )
        fallback_payload, fallback_error = generate_plan(
            executable=config.planner_executable,
            task_direction=task_direction,
            context_text=fallback_context,
            previous_findings=previous_findings,
            previous_plan=previous_plan,
            round_number=round_number,
            timeout_seconds=config.planner_timeout_seconds,
            model=config.planner_model,
            driver=config.planner_driver,
            base_url=config.planner_base_url,
            api_key_env=config.planner_api_key_env,
            api_format=config.planner_api_format,
            transport=_chat_fallback_transport(config.planner_transport),
            diagnostics_out=fallback_diagnostics,
            project_root=project_root,
            planning_mode=planning_mode,
            planning_dir=planning_dir,
        )
        if fallback_payload is not None:
            plan_payload = fallback_payload
            plan_error = ""
            planner_diagnostics = dict(fallback_diagnostics)
            planner_diagnostics.update(
                {
                    "planner_fallback_used": True,
                    "planner_fallback_kind": "tool_use_to_chat",
                    "planner_fallback_reason": fallback_kind,
                    "planner_tool_use_failure": original_diagnostics,
                    "planner_tool_use_error": original_error,
                    "fallback_context_chars": len(fallback_context),
                    "context_chars": len(context_text),
                    "context_budget": int(context_budget or 0),
                }
            )
        else:
            planner_diagnostics["planner_fallback_attempted"] = True
            planner_diagnostics["planner_fallback_kind"] = "tool_use_to_chat"
            planner_diagnostics["planner_fallback_reason"] = fallback_kind
            planner_diagnostics["planner_fallback_error"] = fallback_error or "planner fallback failed"
            planner_diagnostics["planner_fallback_diagnostics"] = dict(fallback_diagnostics)
    if plan_payload is None:
        _progress(f"round {round_number}: planner failed — {plan_error}")
        structural_issues = [plan_error or "planner failed"]
        return PlanningRound(
            round_number=round_number,
            plan_payload={},
            review_payload=None,
            review_error=plan_error or "planner failed",
            structural_issues=structural_issues,
            blocking_findings_count=len(structural_issues),
            timestamp=_utc_now_iso(),
            planner_error=plan_error or "planner failed",
            planner_diagnostics=dict(planner_diagnostics),
            blocking_findings=_compact_findings(
                _all_findings(review_payload=None, structural_issues=structural_issues)
            ),
        )

    task_count = len(list(plan_payload.get("tasks") or []))
    _progress(f"round {round_number}: planner returned {task_count} task(s), resolving paths...")
    plan_payload, path_resolution = resolve_plan_paths(plan_payload, file_manifest, project_root)
    if path_resolution.get("auto_resolved"):
        _progress(f"round {round_number}: auto-resolved {len(path_resolution['auto_resolved'])} path(s)")
    plan_payload, deterministic_repairs = apply_deterministic_repairs(
        plan_payload,
        previous_plan=previous_plan,
        previous_findings=previous_findings,
        project_root=project_root,
        task_direction=task_direction,
    )
    taskgraph_resolution_log = [
        item for item in deterministic_repairs if item.get("rule") == "serialize_parallel_file_conflicts"
    ]
    if deterministic_repairs:
        _progress(f"round {round_number}: applied {len(deterministic_repairs)} deterministic repair(s)")
    if taskgraph_resolution_log:
        _progress(
            f"round {round_number}: serialized {len(taskgraph_resolution_log)} "
            "parallel write conflict(s)"
        )
    effective_previous_findings = previous_findings_with_deterministic_refs(
        previous_findings,
        deterministic_repairs,
    )
    structural_issues = _validate_plan(plan_payload, project_root=project_root)
    structural_issues.extend(
        validate_evidence_resolutions(
            plan_payload,
            review_evidence_packs,
            previous_plan=previous_plan,
        )
    )
    structural_issues.extend(
        validate_plan_revision(
            previous_plan=previous_plan,
            current_plan=plan_payload,
            previous_findings=effective_previous_findings,
            precondition_replan_hint=precondition_replan_hint,
        )
    )
    if path_resolution.get("ambiguous"):
        for item in path_resolution["ambiguous"]:
            structural_issues.append(
                f"ambiguous path '{item['original']}': candidates={item['candidates']} — use full path"
            )
    if structural_issues:
        _progress(f"round {round_number}: {len(structural_issues)} structural issue(s)")
    # P4 advisory: detect docs-only tasks in a plan that lacks any
    # downstream test-bearing task. The deterministic guard already lets
    # the docs task proceed (v5 P0 short-circuit), but warning the planner
    # at plan time gives it a chance to schedule the implementation+test
    # follow-up before execution. Pure advisory — does not block.
    plan_advisories = detect_docs_only_without_test_coverage(plan_payload)
    if plan_advisories:
        plan_payload["plan_advisories"] = plan_advisories
        _progress(
            f"round {round_number}: {len(plan_advisories)} docs-only advisory "
            "(plan has docs-only task without test-bearing peer task)"
        )
    planning_readiness = evaluate_plan_execution_readiness(
        project_root=project_root,
        plan_payload=plan_payload,
    )
    if _planning_readiness_blocked(planning_readiness):
        finding = _precondition_finding(planning_readiness)
        review_payload = {
            "score": 0.0,
            "approved": False,
            "assessment": "blocked_by_precondition",
            "findings": [finding],
            "contradictions": [],
            "source": "execution_readiness",
        }
        _progress(
            f"round {round_number}: execution precondition blocked — "
            f"{_clean_text(planning_readiness.get('suggested_next_task')) or planning_readiness.get('reason')}"
        )
        return PlanningRound(
            round_number=round_number,
            plan_payload=plan_payload,
            review_payload=review_payload,
            review_error="",
            structural_issues=structural_issues,
            blocking_findings_count=1,
            timestamp=_utc_now_iso(),
            path_resolution=path_resolution,
            planner_diagnostics=dict(planner_diagnostics),
            deterministic_repairs=[dict(item) for item in deterministic_repairs],
            planning_readiness=dict(planning_readiness),
            blocking_findings=[dict(finding)],
        )
    _progress(f"round {round_number}: calling reviewer ({config.reviewer_driver or config.reviewer_executable})...")
    review_payload, review_error = review_plan(
        executable=config.reviewer_executable,
        plan_payload=plan_payload,
        task_direction=task_direction,
        context_text=context_text,
        structural_issues=structural_issues,
        round_number=round_number,
        timeout_seconds=config.reviewer_timeout_seconds,
        model=config.reviewer_model,
        driver=config.reviewer_driver,
        transport=config.plan_reviewer_transport,
        project_root=project_root,
        resolved_findings=list(resolved_findings_log or []),
    )
    if review_error:
        _progress(f"round {round_number}: reviewer failed — {review_error}")
    review_payload, demoted_findings = demote_findings_already_repaired(
        review_payload,
        deterministic_repairs=deterministic_repairs,
        plan_payload=plan_payload,
    )
    if demoted_findings:
        _progress(
            f"round {round_number}: demoted {len(demoted_findings)} reviewer finding(s) "
            "already covered by deterministic_repair this round"
        )
    blocked = _blocking_findings(
        review_payload=review_payload,
        structural_issues=structural_issues,
        threshold=_effective_blocking_severities(config),
    )
    _progress(f"round {round_number}: done — {len(blocked)} blocking finding(s)")
    return PlanningRound(
        round_number=round_number,
        plan_payload=plan_payload,
        review_payload=review_payload,
        review_error=review_error,
        structural_issues=structural_issues,
        blocking_findings_count=len(blocked),
        timestamp=_utc_now_iso(),
        path_resolution=path_resolution,
        planner_diagnostics=dict(planner_diagnostics),
        deterministic_repairs=[dict(item) for item in deterministic_repairs],
        planning_readiness=dict(planning_readiness),
        blocking_findings=[dict(item) for item in blocked],
        demoted_repaired_findings=[dict(item) for item in demoted_findings],
    )

def _ingest_prompt_lessons_from_successful_planning(
    *,
    project_root: Path,
    config: PlanningConfig,
    rounds: list[PlanningRound],
    status: str,
    planning_run_id: str,
) -> dict[str, Any]:
    repairs = [
        dict(repair)
        for round_item in rounds
        for repair in list(round_item.deterministic_repairs or [])
        if isinstance(repair, dict)
    ]
    if not repairs:
        return {"processed": 0, "promoted": 0, "reason": "no_deterministic_repairs"}
    try:
        from kodawari.autopilot.core.prompt_profiles import model_family
        from kodawari.instincts import ingest_deterministic_repair_prompt_lessons
    except Exception:
        return {"processed": 0, "promoted": 0, "reason": "prompt_lesson_module_unavailable"}
    transport_name = ""
    if config.planner_transport is not None:
        transport_name = str(getattr(config.planner_transport, "name", "") or "")
    try:
        family = model_family(
            model=config.planner_model,
            transport_name=transport_name,
            driver=config.planner_driver,
        )
        return ingest_deterministic_repair_prompt_lessons(
            project_root,
            repairs,
            family=family,
            run_id=planning_run_id,
            final_status=status,
        )
    except Exception as exc:  # noqa: BLE001 - learning must never block planning
        return {
            "processed": 0,
            "promoted": 0,
            "reason": "prompt_lesson_ingest_failed",
            "error": str(exc)[:300],
        }

def _all_existing_files_found(final_plan: dict[str, Any], project_root: Path) -> bool:
    tasks = _plan_tasks(final_plan)
    upstream_new_files_by_task = _upstream_new_files_by_task(tasks)
    root = project_root.resolve()
    for task in tasks:
        task_id = _clean_text(task.get("task_id"))
        files = _string_list(task.get("files_to_change"))
        new_files = set(_string_list(task.get("new_files")))
        for path in files:
            if path in new_files:
                continue
            if path in upstream_new_files_by_task.get(task_id, set()):
                continue
            try:
                candidate = (root / path).resolve()
                if not candidate.is_relative_to(root) or not candidate.exists():
                    return False
            except (OSError, ValueError):
                return False
    return True

def _new_files_properly_marked(final_plan: dict[str, Any]) -> bool:
    for task in _plan_tasks(final_plan):
        files = set(_string_list(task.get("files_to_change")))
        new_files = set(_string_list(task.get("new_files")))
        if not new_files.issubset(files):
            return False
    return True

def _dependency_graph_acyclic(final_plan: dict[str, Any]) -> bool:
    return _check_acyclic(_plan_tasks(final_plan))

def _normalize_score(value: Any, *, default: float = 0.0) -> float:
    if value is None or _clean_text(value) == "":
        return default
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    if score > 10.0:
        return score / 10.0
    if 0.0 < score <= 1.0:
        return score * 10.0
    return score


def _normalize_review_score(value: Any) -> float:
    return _normalize_score(value)


def _all_findings_demoted_by_repair(findings: list[dict[str, Any]]) -> bool:
    return bool(findings) and all(bool(item.get("severity_demoted", False)) for item in findings)

# Shared whitelist of structural check names — used by ``_evaluate_approval``
# (planning_orchestrator) AND ``try_auto_accept_planning_approval``
# (escalation.planning_auto_accept). If you add a check here, both call sites
# pick it up automatically; if you add a check to ``checks`` without listing
# it here, the auto-accept path will silently ignore it.
STRUCTURAL_CHECK_NAMES: tuple[str, ...] = (
    "tasks_non_empty",
    "no_blocking_findings",
    "no_contradictions",
    "all_existing_files_found",
    "new_files_properly_marked",
    "test_plan_present",
    "invariants_present",
    "layer_owner_present",
    "surface_present",
    "dependency_graph_acyclic",
    "reviewer_available",
    "path_type_valid",
    "layers_present",
    "coverage_hints_present",
    "module_boundaries_roots_present",
    "verify_recipes_complete",
    "plan_consistency_ok",
)


def _evaluate_approval(
    *,
    final_plan: dict[str, Any],
    final_review: dict[str, Any] | None,
    project_root: Path,
    config: PlanningConfig,
) -> dict[str, Any]:
    tasks = _plan_tasks(final_plan)
    findings = _review_findings(final_review)
    contradictions = _string_list(dict(final_review or {}).get("contradictions"))
    no_blocking = not any(_is_blocking_finding(item, _effective_blocking_severities(config)) for item in findings)
    reviewer_approved_raw = bool(dict(final_review or {}).get("approved"))
    demoted_by_repair = _all_findings_demoted_by_repair(findings)
    reviewer_approved = reviewer_approved_raw or (no_blocking and demoted_by_repair)
    unresolved_contradictions = bool(contradictions) and not (reviewer_approved and no_blocking)
    reviewer_score = _normalize_review_score(dict(final_review or {}).get("score"))
    reviewer_available = final_review is not None
    raw_planner_score = dict(final_plan.get("self_assessment") or {}).get("score")
    planner_score = _normalize_score(
        raw_planner_score,
        default=reviewer_score if reviewer_available else 0.0,
    )
    score_adjusted_by_repair = False
    if reviewer_approved and not reviewer_approved_raw and demoted_by_repair and reviewer_score < 8.0:
        reviewer_score = max(reviewer_score, min(10.0, planner_score), 8.0)
        score_adjusted_by_repair = True
    path_type = _clean_text(final_plan.get("path_type")).lower()
    plan_layers = _string_list(final_plan.get("layers"))
    plan_coverage = _string_list(final_plan.get("coverage_hints"))
    module_boundaries = _dict_list(final_plan.get("module_boundaries"))
    verify_recipes = _dict_list(final_plan.get("verify_recipes"))
    plan_consistency_issues = validate_plan_consistency(final_plan)
    checks = {
        "tasks_non_empty": bool(tasks),
        "no_blocking_findings": no_blocking,
        "no_contradictions": not unresolved_contradictions,
        "all_existing_files_found": _all_existing_files_found(final_plan, project_root),
        "new_files_properly_marked": _new_files_properly_marked(final_plan),
        "test_plan_present": all(_clean_text(item.get("test_plan")) for item in tasks),
        "invariants_present": all(bool(_string_list(item.get("invariants"))) for item in tasks),
        "layer_owner_present": all(_clean_text(item.get("layer_owner")) for item in tasks),
        "surface_present": all(_clean_text(item.get("surface")) for item in tasks),
        "dependency_graph_acyclic": _dependency_graph_acyclic(final_plan),
        "reviewer_available": reviewer_available,
        "path_type_valid": bool(path_type and path_type in ("read", "write", "both")),
        "layers_present": bool(plan_layers),
        "coverage_hints_present": bool(plan_coverage) or all(bool(_string_list(t.get("coverage_hints"))) for t in tasks),
        "module_boundaries_roots_present": all(bool(_string_list(mb.get("roots"))) for mb in module_boundaries) if module_boundaries else False,
        "verify_recipes_complete": all(
            bool(_clean_text(vr.get("surface")) and _clean_text(vr.get("command")) and isinstance(vr.get("required"), bool))
            for vr in verify_recipes
        ) if verify_recipes else False,
        "plan_consistency_ok": not plan_consistency_issues,
        "planner_score": planner_score,
        "reviewer_score": reviewer_score,
        "score_gap_ok": abs(planner_score - reviewer_score) <= 2.0 if reviewer_available else False,
        "reviewer_approved_effective": reviewer_approved,
        "reviewer_findings_demoted_by_repair": demoted_by_repair,
        "reviewer_score_adjusted_by_repair": score_adjusted_by_repair,
    }
    structural_pass = all(bool(checks[name]) for name in STRUCTURAL_CHECK_NAMES)
    scores_ok = (
        checks["planner_score"] >= 8.0
        and checks["reviewer_score"] >= 8.0
        and checks["score_gap_ok"]
    )
    # A3: when scores are between 7.5 and 8 but the plan is otherwise clean
    # (no blocking findings, all 17 structural checks pass, reviewer effective-
    # approved, planner-vs-reviewer gap acceptable), allow auto-approve via the
    # guarded relaxed path. Observed run08 had planner=7.6 / reviewer=7.5,
    # blocking=0, every structural check PASS — that plan deserved auto-pass
    # but the strict >=8 gate forced a human round-trip.
    scores_ok_relaxed = (
        checks["planner_score"] >= 7.5
        and checks["reviewer_score"] >= 7.5
        and checks["score_gap_ok"]
        and bool(checks.get("no_blocking_findings"))
        and bool(checks.get("reviewer_approved_effective"))
    )
    if config.auto_approve_enabled and structural_pass and scores_ok:
        return {
            "decision": "auto_approve",
            "reason": "all_structural_checks_passed",
            "checks": checks,
            "plan_consistency_issues": plan_consistency_issues,
        }
    if (
        config.auto_approve_enabled
        and structural_pass
        and not scores_ok
        and scores_ok_relaxed
    ):
        return {
            "decision": "auto_approve",
            "reason": "all_structural_checks_passed_relaxed_score",
            "checks": checks,
            "plan_consistency_issues": plan_consistency_issues,
        }
    if not structural_pass:
        reason = "structural_checks_failed"
    elif not scores_ok:
        reason = "score_checks_failed"
    else:
        reason = "auto_approve_disabled"
    return {
        "decision": "human_required",
        "reason": reason,
        "checks": checks,
        "plan_consistency_issues": plan_consistency_issues,
    }

PLANNING_AUDIT_LOG_FILENAME = ".planning_audit_log.json"


def _last_round_blocking_findings(
    rounds: list[PlanningRound],
    *,
    threshold: frozenset[str],
) -> list[dict[str, Any]]:
    """Return blocking findings from the most recent round only.

    Earlier rounds may have surfaced findings the planner has since fixed.
    Active-scope filtering should reflect what the plan we're about to ship
    *currently* exposes, not historical churn.
    """
    if not rounds:
        return []
    last = rounds[-1]
    return _blocking_findings(
        review_payload=last.review_payload,
        structural_issues=last.structural_issues,
        threshold=threshold,
    )


def _evaluate_active_scope_for_auto_skip(
    *,
    last_plan: dict[str, Any] | None,
    rounds: list[PlanningRound],
    planning_dir: Path,
    threshold: frozenset[str],
) -> dict[str, Any]:
    """Split last-round blockers into active-scope vs future-scope buckets.

    Returns a dict with:
      ``active_task_ids``        — task ids selected as active (1+, may be empty)
      ``scope_task_ids``         — sorted list of every task id in the active scope
      ``source``                 — how active task was chosen (hint / task_card_active /
                                   topological_first_leaf / topological_multi_leaf /
                                   unscoped)
      ``active_scope_blockers``  — blocking findings inside the scope
      ``future_scope_blockers``  — blocking findings only about other tasks
      ``unscoped_blockers``      — blockers we couldn't classify; treated as active
                                   to stay safe.
    """
    blockers = _last_round_blocking_findings(rounds, threshold=threshold)
    scope = derive_active_scope(plan_payload=last_plan, planning_dir=planning_dir)
    known_task_ids = [
        _clean_text(task.get("task_id"))
        for task in _plan_tasks(last_plan)
        if _clean_text(task.get("task_id"))
    ]
    in_scope, out_of_scope, unscoped = classify_findings_by_active_scope(
        blockers,
        scope_task_ids=scope["scope_task_ids"],
        known_task_ids=known_task_ids,
    )
    return {
        "active_task_ids": list(scope["active_task_ids"]),
        "scope_task_ids": sorted(scope["scope_task_ids"]),
        "source": str(scope["source"]),
        "active_scope_blockers": [dict(item) for item in in_scope] + [dict(item) for item in unscoped],
        "future_scope_blockers": [dict(item) for item in out_of_scope],
        "unscoped_blockers": [dict(item) for item in unscoped],
    }


def _active_scope_plan(final_plan: dict[str, Any] | None, active_scope_outcome: dict[str, Any]) -> dict[str, Any]:
    scope_task_ids = set(_string_list(active_scope_outcome.get("scope_task_ids")))
    tasks = [
        dict(task)
        for task in _plan_tasks(final_plan)
        if _clean_text(task.get("task_id")) in scope_task_ids
    ]
    payload = dict(final_plan or {})
    payload["tasks"] = tasks
    return payload


def _active_scope_task_checks(
    *,
    final_plan: dict[str, Any] | None,
    active_scope_outcome: dict[str, Any],
    project_root: Path,
) -> dict[str, bool]:
    """Evaluate the task-level structural subset for active-scope artifacts.

    Full-plan approval remains authoritative. These checks only explain
    whether the active task subset was clean enough for lite auto-skip.
    """
    active_plan = _active_scope_plan(final_plan, active_scope_outcome)
    tasks = _plan_tasks(active_plan)
    return {
        "tasks_non_empty": bool(tasks),
        "no_blocking_findings": not list(active_scope_outcome.get("active_scope_blockers") or []),
        "all_existing_files_found": _all_existing_files_found(active_plan, project_root),
        "new_files_properly_marked": _new_files_properly_marked(active_plan),
        "test_plan_present": all(_clean_text(item.get("test_plan")) for item in tasks),
        "invariants_present": all(bool(_string_list(item.get("invariants"))) for item in tasks),
        "layer_owner_present": all(_clean_text(item.get("layer_owner")) for item in tasks),
        "surface_present": all(_clean_text(item.get("surface")) for item in tasks),
        "dependency_graph_acyclic": _dependency_graph_acyclic(active_plan),
        "coverage_hints_present": bool(_string_list(active_plan.get("coverage_hints")))
        or all(bool(_string_list(task.get("coverage_hints"))) for task in tasks),
    }


def _active_scope_outcome_for_result(
    *,
    last_plan: dict[str, Any] | None,
    rounds: list[PlanningRound],
    planning_dir: Path,
    threshold: frozenset[str],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(existing, dict) and existing.get("source"):
        return dict(existing)
    return _evaluate_active_scope_for_auto_skip(
        last_plan=last_plan,
        rounds=rounds,
        planning_dir=planning_dir,
        threshold=threshold,
    )


def _annotate_active_scope_views(
    *,
    approval: dict[str, Any],
    final_plan: dict[str, Any] | None,
    final_review: dict[str, Any] | None,
    status: str,
    rounds: list[PlanningRound],
    planning_dir: Path,
    project_root: Path,
    threshold: frozenset[str],
    active_scope_outcome: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Attach full-plan/active-scope derived views without changing decisions."""
    annotated_approval = dict(approval)
    annotated_approval["scope"] = "full_plan"
    if status != "auto_skipped":
        return annotated_approval, {}
    outcome = _active_scope_outcome_for_result(
        last_plan=final_plan,
        rounds=rounds,
        planning_dir=planning_dir,
        threshold=threshold,
        existing=active_scope_outcome,
    )
    known_task_ids = [
        _clean_text(task.get("task_id"))
        for task in _plan_tasks(final_plan)
        if _clean_text(task.get("task_id"))
    ]
    demoted = list(rounds[-1].demoted_repaired_findings) if rounds else []
    final_review_active_scope = derive_active_scope_review_view(
        final_review,
        active_scope_outcome=outcome,
        known_task_ids=known_task_ids,
        threshold=threshold,
        demoted_repaired_findings=demoted,
    )
    checks = _active_scope_task_checks(
        final_plan=final_plan,
        active_scope_outcome=outcome,
        project_root=project_root,
    )
    if final_review_active_scope.get("approved") is True and all(checks.values()):
        annotated_approval["active_scope_view"] = {
            "decision": "auto_approve_active_scope",
            "reason": "active_task_passes_review",
            "active_task_ids": list(outcome.get("active_task_ids") or []),
            "scope_task_ids": list(outcome.get("scope_task_ids") or []),
            "scope_source": str(outcome.get("source") or ""),
            "checks_in_scope": checks,
            "future_scope_blocker_count": len(list(outcome.get("future_scope_blockers") or [])),
        }
    return annotated_approval, final_review_active_scope


def _record_future_scope_debt(
    *,
    planning_dir: Path,
    active_scope_outcome: dict[str, Any],
    feature: str,
    planning_run_id: str,
) -> None:
    """Append a future-scope-debt entry to ``.planning_audit_log.json``.

    Future-scope blockers do not stop the active run, but they should not
    silently disappear either: a later run picking up TTS-DEG-01 needs to
    know the reviewer flagged it during TTS-DEG-02's planning loop.
    """
    future = list(active_scope_outcome.get("future_scope_blockers") or [])
    if not future:
        return
    entry = {
        "schema_version": "planning.audit_log.v1",
        "recorded_at": _utc_now_iso(),
        "feature": _clean_text(feature),
        "planning_run_id": _clean_text(planning_run_id),
        "active_task_ids": list(active_scope_outcome.get("active_task_ids") or []),
        "active_scope_source": str(active_scope_outcome.get("source") or ""),
        "future_scope_blocker_count": len(future),
        "future_scope_blockers": future,
    }
    path = (Path(planning_dir) / PLANNING_AUDIT_LOG_FILENAME).resolve()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if isinstance(existing, list):
        rows = existing
    elif isinstance(existing, dict):
        rows = list(existing.get("entries") or [])
    else:
        rows = []
    rows.append(entry)
    payload = {"schema_version": "planning.audit_log.v1", "entries": rows}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # audit logging is best-effort; never block planning on write failure
        pass


def _build_escalation(
    rounds: list[PlanningRound],
    *,
    threshold: frozenset[str] | None = DEFAULT_BLOCKING_SEVERITIES,
    stubborn_rounds: int = 0,
    termination_reason: str = "",
    scope: str = "",
) -> dict[str, Any]:
    """Build an escalation payload.

    ``scope`` is a structured field describing what part of the plan this
    escalation concerns: ``active`` (active task is blocked), ``future`` (only
    future tasks have blockers; should not appear in normal escalation paths
    — future-only blockers route to the audit log instead), or ``unscoped``
    (no per-task split was performed).

    Critically, ``error_code`` and ``run_reason`` on ``.planning_failure.json``
    stay tied to ``termination_reason``. ``scope`` is purely additive metadata
    so downstream consumers (self_repair classifier, prompt_lessons, dashboards)
    keep routing on the existing fields.
    """
    deduped = _unresolved_findings(rounds, threshold=threshold)
    conflict_category = _classify_conflict_category(deduped)
    planner_position = _planner_position(rounds)
    reviewer_position = _reviewer_position(rounds)
    payload = {
        "unresolved_findings": deduped,
        "planner_position": planner_position,
        "reviewer_position": reviewer_position,
        "conflict_category": conflict_category,
        "suggested_human_questions": _suggested_human_questions(conflict_category, deduped),
        "scope": _clean_text(scope) or "unscoped",
    }
    if stubborn_rounds > 0:
        payload["stubborn_rounds"] = int(stubborn_rounds)
    if termination_reason:
        payload["termination_reason"] = _clean_text(termination_reason)
    return payload

def _signature_payload(signature: tuple[tuple[str, str, frozenset[str]], ...]) -> list[dict[str, Any]]:
    return [
        {
            "severity": severity,
            "category": category,
            "tokens": sorted(tokens),
        }
        for severity, category, tokens in signature
    ]

def _deadlock_streak_limit(config: PlanningConfig) -> int:
    return max(1, int(getattr(config, "deadlock_streak_limit", 2) or 2))

def _planner_position(rounds: list[PlanningRound]) -> str:
    for item in reversed(rounds):
        payload = dict(item.plan_payload or {})
        summary = _clean_text(payload.get("summary"))
        if summary:
            return summary
    return ""

def _reviewer_position(rounds: list[PlanningRound]) -> str:
    for item in reversed(rounds):
        review = dict(item.review_payload or {})
        assessment = _clean_text(review.get("assessment"))
        if assessment:
            return assessment
    return ""

def _finding_text(finding: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            _clean_text(finding.get("category")),
            _clean_text(finding.get("description")),
            _clean_text(finding.get("recommendation")),
        )
        if part
    ).lower()

def _classify_conflict_category(findings: list[dict[str, Any]]) -> str:
    if not findings:
        return "information"
    for finding in findings:
        text = _finding_text(finding)
        if any(token in text for token in ("depends_on", "dependency", "cycle", "invariant", "constraint")):
            return "constraint"
    for finding in findings:
        text = _finding_text(finding)
        if any(token in text for token in ("architecture", "layer", "boundary", "module", "surface")):
            return "architecture"
    for finding in findings:
        text = _finding_text(finding)
        if any(token in text for token in ("scope", "file", "path", "missing", "new_files")):
            return "scope"
    return "information"

def _suggested_human_questions(
    conflict_category: str,
    findings: list[dict[str, Any]],
) -> list[str]:
    sample = _clean_text(findings[0].get("description")) if findings else ""
    if conflict_category == "architecture":
        return [
            "Which module boundary is authoritative for this change?",
            f"Should the plan keep current architecture constraints, or revise them to address: {sample or 'the top architecture concern'}?",
        ]
    if conflict_category == "constraint":
        return [
            "Should we relax specific invariants, or keep invariants strict and split the task into smaller units?",
            f"Which dependency/constraint should be prioritized first to resolve: {sample or 'the top constraint conflict'}?",
        ]
    if conflict_category == "scope":
        return [
            "Which exact file set is in scope for this iteration?",
            f"Should we split tasks to isolate this scope issue: {sample or 'the top scope conflict'}?",
        ]
    return [
        "What missing context or source file should be added before re-planning?",
        f"Which unresolved point should the planner/reviewer prioritize first: {sample or 'the top unresolved finding'}?",
    ]

def _snippet_paths(context: dict[str, Any]) -> list[str]:
    raw = list(context.get("candidate_snippets") or [])
    dict_items = [item for item in raw if isinstance(item, dict)]
    paths = [_clean_text(item.get("path")) for item in dict_items]
    return [path for path in dict.fromkeys(paths) if path]

def _manifest_paths(context: dict[str, Any], limit: int = 20) -> list[str]:
    manifest_dict = dict(context.get("repo_manifest") or {})
    raw_files = list(manifest_dict.get("files") or [])
    return [_clean_text(path) for path in raw_files[:limit] if _clean_text(path)]

def _context_scout_candidate_files(context: dict[str, Any]) -> list[str]:
    snippets = _snippet_paths(context)
    if snippets:
        return snippets
    return _manifest_paths(context)

def _context_scout_line_counts(project_root: Path, candidate_files: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rel in candidate_files:
        try:
            path = (project_root / rel).resolve()
            if not path.exists() or not path.is_file():
                continue
        except (OSError, ValueError):
            continue
        try:
            counts[rel] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return counts

def _planning_context_scout_payload(
    *,
    project_root: Path,
    context: dict[str, Any],
    task_direction: str,
) -> dict[str, Any]:
    if not context_scout_enabled():
        return {"enabled": False}
    candidates = _context_scout_candidate_files(context)
    line_counts = _context_scout_line_counts(project_root, candidates)
    return dict(
        build_context_scout_payload(
            user_text=task_direction,
            candidate_files=candidates,
            file_line_counts=line_counts,
            files_estimate_override=len(candidates),
        )
    )

def run_planning_conversation(
    *,
    config: PlanningConfig,
    project_root: Path,
    planning_dir: Path,
    task_direction: str,
    repo_inventory: dict[str, Any],
    prd_path: Path | None = None,
    feature: str,
) -> PlanningResult:
    planning_run_id = f"planning-{uuid4().hex}"
    _safe_unlink(Path(planning_dir) / PLANNING_FAILURE_FILENAME)
    _write_planning_progress(
        planning_dir=planning_dir,
        planning_run_id=planning_run_id,
        feature=feature,
        task_direction=task_direction,
        status="running",
        stage="collecting_context",
        rounds=[],
    )
    _progress("collecting planning context...")
    context = collect_planning_context(
        project_root=project_root,
        repo_inventory=repo_inventory,
        prd_path=prd_path,
        task_direction=task_direction,
        feature=feature,
        planning_dir=planning_dir,
    )
    context_scout_payload = _planning_context_scout_payload(
        project_root=project_root,
        context=context,
        task_direction=task_direction,
    )
    context_budgets = _planner_context_fallback_budgets(config)
    context_budget_index = 0
    context_budget = context_budgets[context_budget_index]
    context_text = _render_planner_context_for_budget(context, context_budget)
    file_manifest = build_file_manifest(
        list(dict(context.get("repo_manifest") or {}).get("files") or [])
    )
    manifest_size = sum(len(v) for v in file_manifest.values())
    budget_note = f", budget={context_budget}" if context_budget > 0 else ""
    _progress(f"context ready: {len(context_text)} chars{budget_note}, {len(file_manifest)} basenames ({manifest_size} paths)")
    feasibility = evaluate_task_input_feasibility(
        task_direction=task_direction,
        file_manifest=file_manifest,
    )
    if feasibility.get("status") == "INFEASIBLE":
        _progress(
            "task input feasibility precheck: INFEASIBLE — "
            f"missing route surface(s) {feasibility.get('missing_surfaces')}; "
            "skipping planner rounds and escalating for human decision"
        )
        return _early_exit_for_input_infeasibility(
            planning_dir=planning_dir,
            planning_run_id=planning_run_id,
            feature=feature,
            task_direction=task_direction,
            feasibility=feasibility,
            context_scout_payload=context_scout_payload,
        )
    _progress(f"starting planning conversation (max {config.max_rounds} rounds)...")
    _write_planning_progress(
        planning_dir=planning_dir,
        planning_run_id=planning_run_id,
        feature=feature,
        task_direction=task_direction,
        status="running",
        stage="context_ready",
        rounds=[],
        context_chars=len(context_text),
        context_budget=context_budget,
        extra={"max_rounds": int(config.max_rounds or 0), "manifest_size": manifest_size},
    )
    previous_findings: list[dict[str, Any]] = []
    previous_findings_signature: tuple[tuple[str, str, frozenset[str]], ...] | None = None
    previous_blocking_signature: tuple[tuple[str, str, frozenset[str]], ...] | None = None
    previous_plan_signature: dict[str, Any] | None = None
    # A1 stateful reviewer: accumulate findings the planner has addressed
    # across rounds so the next reviewer call sees them in the prompt and
    # doesn't re-flag them as fresh blockers. Identity uses
    # _finding_signature (severity + canonical_category + token_bag) so
    # reword tricks can't slip past.
    resolved_findings_log: list[dict[str, Any]] = []
    resolved_signature_set: set[tuple[str, str, frozenset[str]]] = set()
    # Phase B: meta-blocker streak counter + audit log. Streak counts
    # consecutive rounds where every blocking finding classifies into the
    # meta_blocker bucket; once it hits META_BLOCKER_STREAK_LIMIT and score
    # guardrails pass, demote_meta_blocker_findings_to_info rewrites those
    # blockers to severity=info so the strict approval path can fire.
    meta_blocker_streak = 0
    meta_blocker_demotion_log: list[dict[str, Any]] = []
    rounds: list[PlanningRound] = []
    last_plan: dict[str, Any] = {}
    last_review: dict[str, Any] | None = None
    status = "error"
    escalation: dict[str, Any] | None = None
    stubborn_rounds = 0
    repeated_blocker_rounds = 0
    precondition_block_streak = 0
    previous_precondition_signature: tuple[str, ...] = ()
    ambiguous_evidence_streak = 0
    consecutive_any_ambiguous = 0
    previous_ambiguous_signature: tuple[str, ...] = ()
    last_all_findings: list[dict[str, Any]] = []
    best_clean_round: PlanningRound | None = None
    review_evidence_packs: list[dict[str, Any]] = []
    blocking_threshold = _effective_blocking_severities(config)
    active_scope_outcome: dict[str, Any] | None = None
    precondition_replan_hint = (
        dict(context.get("precondition_replan_hint") or {})
        if isinstance(context.get("precondition_replan_hint"), dict)
        else {}
    )
    resolved_planning_mode = (
        str(dict(repo_inventory or {}).get("mode") or "existing").strip().lower()
        or "existing"
    )
    for round_number in range(1, max(1, int(config.max_rounds or 1)) + 1):
        current = _run_round(
            config=config,
            task_direction=task_direction,
            context_text=context_text,
            context_budget=context_budget,
            project_root=project_root,
            file_manifest=file_manifest,
            previous_findings=previous_findings,
            previous_plan=last_plan if round_number > 1 else None,
            round_number=round_number,
            precondition_replan_hint=precondition_replan_hint,
            review_evidence_packs=review_evidence_packs,
            planning_dir=planning_dir,
            resolved_findings_log=resolved_findings_log,
            planning_mode=resolved_planning_mode,
        )
        rounds.append(current)
        if current.plan_payload:
            last_plan = dict(current.plan_payload)
        if current.review_payload is not None:
            last_review = dict(current.review_payload)
        all_findings = _all_findings(
            review_payload=current.review_payload,
            structural_issues=current.structural_issues,
        )
        blocked = _blocking_findings(
            review_payload=current.review_payload,
            structural_issues=current.structural_issues,
            threshold=blocking_threshold,
        )
        # Phase B: meta-blocker streak detection. When every blocking finding
        # this round classifies as meta_blocker (reviewer recursing on plan
        # meta-fields like evidence_resolutions[Rxfy] citing itself), the
        # planner cannot close the loop. After META_BLOCKER_STREAK_LIMIT
        # consecutive rounds — and only when score guardrails pass — demote
        # those blockers to severity=info so the strict approval path fires.
        # Real-blocker buckets coexisting (canonical_task_anchor, owner_surface,
        # product_semantics, test_coverage, or any non-meta finding) reset the
        # streak; demotion never silences a real concern.
        if blocked and all(is_meta_blocker_finding(item) for item in blocked):
            meta_blocker_streak += 1
        else:
            meta_blocker_streak = 0
        if meta_blocker_streak >= META_BLOCKER_STREAK_LIMIT:
            meta_planner_score = _normalize_score(
                dict(current.plan_payload.get("self_assessment") or {}).get("score"),
                default=0.0,
            )
            meta_reviewer_score = _normalize_review_score(
                dict(current.review_payload or {}).get("score")
            )
            if (
                meta_planner_score >= META_BLOCKER_PLANNER_SCORE_FLOOR
                and meta_reviewer_score >= META_BLOCKER_REVIEWER_SCORE_FLOOR
            ):
                demoted_payload, demoted_items = demote_meta_blocker_findings_to_info(
                    current.review_payload,
                    reason=META_BLOCKER_STREAK_REASON,
                )
                if demoted_items:
                    current.review_payload = demoted_payload
                    last_review = dict(demoted_payload)
                    meta_blocker_demotion_log.append(
                        {
                            "round": int(current.round_number),
                            "streak": int(meta_blocker_streak),
                            "demoted_count": len(demoted_items),
                            "demotion_reason": META_BLOCKER_STREAK_REASON,
                            "planner_score": float(meta_planner_score),
                            "reviewer_score": float(meta_reviewer_score),
                            "demoted_findings": [
                                {
                                    "category": _clean_text(item.get("category")),
                                    "description": _clean_text(item.get("description"))[:240],
                                }
                                for item in demoted_items
                            ],
                        }
                    )
                    _progress(
                        f"round {round_number}: meta_blocker streak={meta_blocker_streak} "
                        f"hit limit; demoted {len(demoted_items)} meta finding(s) to info "
                        f"(planner={meta_planner_score:.1f}, reviewer={meta_reviewer_score:.1f})"
                    )
                    all_findings = _all_findings(
                        review_payload=current.review_payload,
                        structural_issues=current.structural_issues,
                    )
                    blocked = _blocking_findings(
                        review_payload=current.review_payload,
                        structural_issues=current.structural_issues,
                        threshold=blocking_threshold,
                    )
                    current.blocking_findings_count = len(blocked)
                    current.blocking_findings = list(blocked)
                    meta_blocker_streak = 0
                    # Reset ambiguous-evidence streak counters: the downstream
                    # detectors track plan-side `evidence_resolutions[].status
                    # == "ambiguous"` and would otherwise terminate the run as
                    # `planning_evidence_blocked` even though we just decided
                    # the reviewer's recursive demand was the cause of the
                    # unresolved ambiguity. The planner cannot un-recurse what
                    # the reviewer keeps re-filing; we declared the plan
                    # acceptable, so re-arm the streak from this round.
                    ambiguous_evidence_streak = 0
                    consecutive_any_ambiguous = 0
                    previous_ambiguous_signature = ()
        # Phase C: late-round meta-only recovery. The streak detector misses
        # the single-shot pattern observed on external_trends_v1 R7 — the
        # planner cleared all prior reviewer concerns over R1-R6, then R7
        # reviewer filed one new meta-recursive blocker (evidence_resolutions
        # entry asked to cite a future round's finding). The streak counter
        # only saw one all-meta round and stayed below the limit.
        #
        # The discriminator is "correct narrowness": meta_blocker
        # classification requires the reviewer text to cite a future round's
        # own finding or recurse over the planner-reviewer protocol — a
        # structurally impossible pattern for a legitimate blocker. So at the
        # final round, if every remaining blocker classifies as meta_blocker
        # AND score guardrails pass, single-shot demotion is safe. Hard-stop
        # categories (security/auth/data_loss/etc) are already excluded by
        # is_meta_blocker_finding so a real safety concern cannot land here.
        max_rounds_for_phase_c = max(1, int(config.max_rounds or 1))
        if (
            blocked
            and round_number == max_rounds_for_phase_c
            and all(is_meta_blocker_finding(item) for item in blocked)
        ):
            late_planner_score = _normalize_score(
                dict(current.plan_payload.get("self_assessment") or {}).get("score"),
                default=0.0,
            )
            late_reviewer_score = _normalize_review_score(
                dict(current.review_payload or {}).get("score")
            )
            if (
                late_planner_score >= META_BLOCKER_PLANNER_SCORE_FLOOR
                and late_reviewer_score >= META_BLOCKER_REVIEWER_SCORE_FLOOR
            ):
                late_payload, late_items = demote_meta_blocker_findings_to_info(
                    current.review_payload,
                    reason=META_BLOCKER_LATE_ROUND_RECOVERY_REASON,
                )
                if late_items:
                    current.review_payload = late_payload
                    last_review = dict(late_payload)
                    meta_blocker_demotion_log.append(
                        {
                            "round": int(current.round_number),
                            "streak": int(meta_blocker_streak),
                            "demoted_count": len(late_items),
                            "demotion_reason": META_BLOCKER_LATE_ROUND_RECOVERY_REASON,
                            "planner_score": float(late_planner_score),
                            "reviewer_score": float(late_reviewer_score),
                            "demoted_findings": [
                                {
                                    "category": _clean_text(item.get("category")),
                                    "description": _clean_text(item.get("description"))[:240],
                                }
                                for item in late_items
                            ],
                        }
                    )
                    _progress(
                        f"round {round_number}: final round all-meta blocker "
                        f"set, demoted {len(late_items)} finding(s) to info "
                        f"(planner={late_planner_score:.1f}, "
                        f"reviewer={late_reviewer_score:.1f})"
                    )
                    all_findings = _all_findings(
                        review_payload=current.review_payload,
                        structural_issues=current.structural_issues,
                    )
                    blocked = _blocking_findings(
                        review_payload=current.review_payload,
                        structural_issues=current.structural_issues,
                        threshold=blocking_threshold,
                    )
                    current.blocking_findings_count = len(blocked)
                    current.blocking_findings = list(blocked)
                    meta_blocker_streak = 0
                    # Same race as Phase B (see comment there): the
                    # ambiguous-evidence detectors below would still escalate
                    # if the planner left meta-recursive findings as
                    # `evidence_resolutions[].status == "ambiguous"`. Re-arm
                    # those streak counters since Phase C just declared the
                    # plan acceptable for this final round.
                    ambiguous_evidence_streak = 0
                    consecutive_any_ambiguous = 0
                    previous_ambiguous_signature = ()
        evidence_pack = build_review_evidence_pack(
            round_number=current.round_number,
            plan_payload=current.plan_payload,
            findings=blocked,
            context=context,
        )
        if evidence_pack:
            current.review_evidence_pack = evidence_pack
            review_evidence_packs.append(evidence_pack)
            context["review_triggered_evidence"] = list(review_evidence_packs)
            context_text = _render_planner_context_for_budget(context, context_budget)
            _progress(
                f"round {round_number}: built review-triggered evidence pack "
                f"with {len(list(evidence_pack.get('requests') or []))} request(s)"
            )
        best_clean_round = _best_clean_round(rounds, threshold=blocking_threshold)
        last_all_findings = list(all_findings)
        _write_planning_progress(
            planning_dir=planning_dir,
            planning_run_id=planning_run_id,
            feature=feature,
            task_direction=task_direction,
            status="running",
            stage="round_completed",
            rounds=rounds,
            context_chars=len(context_text),
            context_budget=context_budget,
            extra={"last_round_number": int(current.round_number)},
        )
        if _planning_readiness_blocked(current.planning_readiness):
            try:
                write_execution_readiness(planning_dir, current.planning_readiness)
            except OSError:
                pass
            current_precondition_signature = tuple(
                sorted(_string_list(current.planning_readiness.get("missing_preconditions")))
            )
            if current_precondition_signature == previous_precondition_signature:
                precondition_block_streak += 1
            else:
                precondition_block_streak = 1
                previous_precondition_signature = current_precondition_signature
            # Give the planner one more round to insert prereq tasks. The
            # round we just ran already injected a "blocking finding" of
            # category=precondition into the review payload (see _run_round),
            # so the next round's planner sees it. Break only after the
            # planner has had a chance to revise and *still* produces the
            # same set of missing preconditions twice in a row, OR after
            # max_rounds.
            if precondition_block_streak >= 2 or round_number >= max(1, int(config.max_rounds or 1)):
                status = "precondition_blocked"
                escalation = _build_precondition_escalation(rounds, readiness=current.planning_readiness)
                _progress(
                    "planning stopped — execution preconditions are missing and "
                    f"the planner did not insert prereq tasks in {precondition_block_streak} round(s)"
                )
                break
            _progress(
                f"round {round_number}: precondition block surfaced as blocking finding; "
                "planner gets one more round to insert prereq tasks"
            )
            continue
        else:
            # Readiness cleared this round — reset the streak.
            precondition_block_streak = 0
            previous_precondition_signature = ()
        ambiguous_signature = _ambiguous_resolution_signature(current.plan_payload)
        if ambiguous_signature and ambiguous_signature == previous_ambiguous_signature:
            ambiguous_evidence_streak += 1
        elif ambiguous_signature:
            ambiguous_evidence_streak = 1
            previous_ambiguous_signature = ambiguous_signature
        else:
            ambiguous_evidence_streak = 0
            consecutive_any_ambiguous = 0
            previous_ambiguous_signature = ()
        if ambiguous_signature:
            consecutive_any_ambiguous += 1
        if ambiguous_evidence_streak >= AMBIGUOUS_STREAK_LIMIT:
            status = "escalation_required"
            escalation = _build_escalation(
                rounds,
                threshold=blocking_threshold,
                termination_reason="planning_evidence_blocked",
            )
            escalation["gate_reason"] = "planning_evidence_blocked"
            escalation["ambiguous_evidence_signature"] = list(ambiguous_signature)
            escalation["ambiguous_streak_rounds"] = int(ambiguous_evidence_streak)
            _progress(
                f"escalation required — {len(ambiguous_signature)} review evidence "
                f"finding(s) stayed ambiguous for {ambiguous_evidence_streak} consecutive round(s)"
            )
            break
        if consecutive_any_ambiguous >= AMBIGUOUS_ANY_STREAK_LIMIT:
            status = "escalation_required"
            escalation = _build_escalation(
                rounds,
                threshold=blocking_threshold,
                termination_reason="planning_evidence_blocked",
                scope="active",
            )
            escalation["gate_reason"] = "planning_evidence_blocked"
            escalation["any_ambiguous_streak"] = int(consecutive_any_ambiguous)
            _progress(
                "escalation required - review evidence stayed ambiguous "
                f"across {consecutive_any_ambiguous} consecutive round(s)"
            )
            break
        current_findings_signature = _finding_signature(all_findings)
        current_blocking_signature = _finding_signature(blocked)
        current_plan_signature = _plan_feedback_signature(current.plan_payload)
        # A1: anything that was in last round's findings but is missing this
        # round was effectively addressed — add it to the stateful "resolved"
        # log we hand to next round's reviewer. Match identity by signature,
        # not by raw description (LLMs reword between rounds). Single-element
        # _finding_signature calls preserve item->signature alignment (the
        # multi-element form sorts the tuple).
        if previous_findings:
            current_signature_set = set(current_findings_signature)
            for prev_item in previous_findings:
                single = _finding_signature([prev_item])
                if not single:
                    continue
                prev_sig = single[0]
                if prev_sig in current_signature_set:
                    continue
                if prev_sig in resolved_signature_set:
                    continue
                resolved_signature_set.add(prev_sig)
                resolved_findings_log.append(dict(prev_item))
        plan_unchanged = (
            previous_plan_signature is not None
            and current_plan_signature == previous_plan_signature
        )
        findings_unchanged = (
            previous_findings_signature is not None
            and current_findings_signature == previous_findings_signature
        )
        blocker_signature_unchanged = (
            previous_blocking_signature is not None
            and bool(current_blocking_signature)
            and current_blocking_signature == previous_blocking_signature
        )
        previous_findings = all_findings
        previous_findings_signature = current_findings_signature
        previous_blocking_signature = current_blocking_signature
        previous_plan_signature = current_plan_signature
        if (
            not current.plan_payload
            and _planner_context_pressure(current)
            and context_budget_index + 1 < len(context_budgets)
            and round_number < max(1, int(config.max_rounds or 1))
        ):
            context_budget_index += 1
            context_budget = context_budgets[context_budget_index]
            context_text = _render_planner_context_for_budget(context, context_budget)
            budget_note = f"budget={context_budget}" if context_budget > 0 else "full context"
            _progress(
                f"round {round_number}: planner hit context pressure; "
                f"next round will retry with {budget_note}, {len(context_text)} chars"
            )
            continue
        environment_kind = _planner_environment_error_kind(current)
        if environment_kind:
            status = "escalation_required"
            escalation = _build_environment_escalation(rounds, kind=environment_kind)
            _progress(
                f"escalation required — planner environment error "
                f"{environment_kind!r} is not recoverable by another planning revision"
            )
            break
        reviewer_kind = _reviewer_error_kind(current)
        if reviewer_kind and not blocked:
            status = "escalation_required"
            escalation = _build_reviewer_escalation(rounds, kind=reviewer_kind)
            _progress(
                f"escalation required — plan reviewer error "
                f"{reviewer_kind!r} is not recoverable by planner revision"
            )
            break
        if current.plan_payload and not current.review_error and not blocked:
            if config.decision_policy == "strict-gate":
                status = "approved"
                _progress(f"plan approved after {round_number} round(s) with no blocking findings")
                break
            elif config.decision_policy in {"soft-gate", "auto-skip"}:
                if config.decision_policy == "soft-gate":
                    should_escalate, gate_reason = _soft_gate_requires_escalation(
                        all_findings,
                        threshold=blocking_threshold,
                    )
                    if should_escalate:
                        _progress(
                            f"round {round_number}: soft-gate findings still require revision "
                            f"({gate_reason})"
                        )
                        continue
                status = "auto_skipped"
                escalation = _build_escalation(rounds, threshold=None) if all_findings else None
                _progress(
                    f"plan accepted after {round_number} round(s) with no blocking findings "
                    f"(decision_policy={config.decision_policy})"
                )
                break
            else:
                status = "clean_round"
                _progress(
                    f"plan reached clean round after {round_number} round(s); "
                    f"decision_policy={config.decision_policy}"
                )
                break
        if plan_unchanged and findings_unchanged:
            stubborn_rounds += 1
            _progress(
                f"round {round_number}: planner made no monitored changes against unchanged findings "
                f"({stubborn_rounds} stubborn round(s))"
            )
            if stubborn_rounds >= 2:
                status = "escalation_required"
                escalation = _build_escalation(
                    rounds,
                    threshold=blocking_threshold,
                    stubborn_rounds=stubborn_rounds,
                    termination_reason="stubborn_round_limit",
                )
                _progress(
                    f"escalation required — unchanged findings and plan response persisted for "
                    f"{stubborn_rounds} consecutive stubborn round(s)"
                )
                break
        else:
            stubborn_rounds = 0
        if blocked and blocker_signature_unchanged and current.plan_payload and not current.review_error:
            repeated_blocker_rounds += 1
            _progress(
                f"round {round_number}: reviewer blocking theme repeated "
                f"({repeated_blocker_rounds} repeated blocker round(s))"
            )
            if repeated_blocker_rounds >= _deadlock_streak_limit(config):
                status = "escalation_required"
                escalation = _build_escalation(
                    rounds,
                    threshold=blocking_threshold,
                    stubborn_rounds=repeated_blocker_rounds,
                    termination_reason="planner_reviewer_deadlock",
                )
                escalation["gate_reason"] = "planner_reviewer_deadlock"
                escalation["repeated_blocker_rounds"] = repeated_blocker_rounds + 1
                escalation["repeated_blocker_signature"] = _signature_payload(current_blocking_signature)
                _progress(
                    "escalation required — reviewer blocking theme persisted "
                    f"for {repeated_blocker_rounds + 1} round(s)"
                )
                break
        else:
            repeated_blocker_rounds = 0
    if status == "precondition_blocked":
        pass
    elif status == "escalation_required":
        if escalation is None:
            escalation = _build_escalation(rounds, threshold=blocking_threshold)
    elif status == "clean_round":
        status = "escalation_required"
        escalation = _build_escalation(rounds, threshold=None)
        if escalation is not None:
            escalation["gate_reason"] = "approval_required"
        _progress("planning clean round requires approval (decision_policy=approval-required)")
    elif status != "approved":
        if config.decision_policy == "soft-gate":
            unresolved = _unresolved_findings(rounds, threshold=None)
            should_escalate, gate_reason = _soft_gate_requires_escalation(
                unresolved,
                threshold=blocking_threshold,
            )
            if should_escalate:
                status = "escalation_required"
                escalation = _build_escalation(rounds, threshold=blocking_threshold)
                escalation["gate_reason"] = gate_reason
                _progress(
                    f"planning soft-gate escalated — unresolved findings require approval ({gate_reason})"
                )
            else:
                status = "auto_skipped"
                escalation = _build_escalation(rounds, threshold=None) if unresolved else None
                _progress("planning gate soft-skipped (decision_policy=soft-gate)")
        elif config.decision_policy == "auto-skip":
            # auto-skip historically passed any not-approved plan straight
            # through to execution. That let lite-lane runs ship plans whose
            # *active* task was clean while reviewer blockers about *future*
            # tasks (the same plan's later epic items) silently piled up. We
            # now scope the auto-skip gate to the active task: in-scope
            # blockers escalate; out-of-scope ones are recorded as future-
            # scope debt and audit-logged but do not block the active run.
            active_scope_outcome = _evaluate_active_scope_for_auto_skip(
                last_plan=last_plan,
                rounds=rounds,
                planning_dir=planning_dir,
                threshold=blocking_threshold,
            )
            if active_scope_outcome["active_scope_blockers"]:
                status = "escalation_required"
                escalation = _build_escalation(
                    rounds,
                    threshold=blocking_threshold,
                    scope="active",
                )
                escalation["gate_reason"] = "active_scope_blocker_under_auto_skip"
                escalation["active_scope_task_ids"] = active_scope_outcome["active_task_ids"]
                escalation["active_scope_source"] = active_scope_outcome["source"]
                _progress(
                    "auto-skip escalated — active task scope still has "
                    f"{len(active_scope_outcome['active_scope_blockers'])} blocking finding(s)"
                )
            else:
                status = "auto_skipped"
                escalation = _build_escalation(rounds, threshold=None) if last_all_findings else None
                _record_future_scope_debt(
                    planning_dir=planning_dir,
                    active_scope_outcome=active_scope_outcome,
                    feature=feature,
                    planning_run_id=planning_run_id,
                )
                future_count = len(active_scope_outcome["future_scope_blockers"])
                if future_count:
                    _progress(
                        f"planning auto-skipped (active scope clean; {future_count} "
                        "out-of-scope blocker(s) recorded as future-scope debt)"
                    )
                else:
                    _progress("planning gate auto-skipped (decision_policy=auto-skip)")
        elif rounds and rounds[-1].blocking_findings_count > 0:
            status = "escalation_required"
            escalation = _build_escalation(rounds, threshold=blocking_threshold)
            _progress(f"escalation required — {rounds[-1].blocking_findings_count} unresolved finding(s) after {len(rounds)} round(s)")
        else:
            status = "error"
            _progress(f"planning failed after {len(rounds)} round(s)")

    if (
        status in {"escalation_required", "error"}
        and best_clean_round is not None
        and _best_round_can_replace_escalation(config=config, escalation=escalation)
    ):
        last_plan = dict(best_clean_round.plan_payload)
        last_review = dict(best_clean_round.review_payload or {})
        status = "auto_skipped" if config.decision_policy in {"soft-gate", "auto-skip"} else "approved"
        unresolved = _round_findings(best_clean_round, threshold=None)
        escalation = _build_escalation([best_clean_round], threshold=None) if unresolved else None
        if escalation is not None:
            escalation["gate_reason"] = "selected_best_clean_round"
            escalation["selected_round_number"] = best_clean_round.round_number
        _progress(
            f"selected round {best_clean_round.round_number} as best clean planning round; "
            "later degraded rounds were ignored"
        )

    if status in {"approved", "auto_skipped"}:
        annotated_plan = _attach_soft_findings_to_plan_tasks(
            last_plan,
            last_review,
            threshold=blocking_threshold,
        )
        if annotated_plan is not last_plan:
            last_plan = annotated_plan
            _progress("attached soft reviewer findings to task review_focus for execution")

    prompt_lesson_learning = _ingest_prompt_lessons_from_successful_planning(
        project_root=project_root,
        config=config,
        rounds=rounds,
        status=status,
        planning_run_id=planning_run_id,
    )
    planning_readiness = dict(rounds[-1].planning_readiness or {}) if rounds else {}

    contract_fields = _extract_contract_fields(
        plan_payload=last_plan,
        task_direction=task_direction,
        repo_inventory=repo_inventory,
        fingerprint=_clean_text(context.get("input_fingerprint")),
    )
    approval = _evaluate_approval(
        final_plan=last_plan,
        final_review=last_review,
        project_root=project_root,
        config=config,
    )
    approval, final_review_active_scope = _annotate_active_scope_views(
        approval=approval,
        final_plan=last_plan,
        final_review=last_review,
        status=status,
        rounds=rounds,
        planning_dir=planning_dir,
        project_root=project_root,
        threshold=blocking_threshold,
        active_scope_outcome=active_scope_outcome,
    )
    terminal_reason = ""
    if status == "precondition_blocked":
        terminal_reason = "blocked_by_precondition"
    elif status == "escalation_required":
        terminal_reason = _clean_text(dict(escalation or {}).get("termination_reason")) or "escalation_required"
    elif status == "error":
        terminal_reason = "planning_error"
    _write_planning_progress(
        planning_dir=planning_dir,
        planning_run_id=planning_run_id,
        feature=feature,
        task_direction=task_direction,
        status=status,
        stage="completed",
        rounds=rounds,
        context_chars=len(context_text),
        context_budget=context_budget,
        extra={
            "terminal_reason": terminal_reason,
            "planning_readiness": planning_readiness,
            "escalation": dict(escalation or {}),
        },
    )
    if status in {"approved", "auto_skipped"}:
        _safe_unlink(Path(planning_dir) / PLANNING_FAILURE_FILENAME)
    else:
        _write_planning_failure(
            planning_dir=planning_dir,
            planning_run_id=planning_run_id,
            feature=feature,
            task_direction=task_direction,
            status=status,
            reason=terminal_reason or status,
            rounds=rounds,
            escalation=escalation,
            planning_readiness=planning_readiness,
        )
        # Unified escalation hook: classify the planning failure and write a
        # decision request so kodawari decide can surface options to the
        # user. Non-blocking — if the failure is not escalatable or the
        # per-phase cap is reached, planning still exits with its legacy
        # BLOCKED status.
        try:
            from kodawari.autopilot.escalation import maybe_escalate
            blocking_history = [
                int(r.blocking_findings_count or 0) for r in rounds
            ]
            diagnostics = {
                "run_reason": terminal_reason or status,
                "root_cause": _clean_text(dict(escalation or {}).get("gate_reason")) or terminal_reason,
                "round_count": len(rounds),
                "blocking_findings_history": blocking_history,
                "last_plan_tasks_count": len(list(last_plan.get("tasks") or [])) if isinstance(last_plan, dict) else 0,
                "blocking_reason": (terminal_reason or status),
                "missing_surfaces": list(
                    dict(escalation or {}).get("missing_surfaces") or []
                ),
            }
            maybe_escalate(
                planning_dir=planning_dir,
                phase="planning",
                planning_diagnostics=diagnostics,
                feature=feature,
                failure_summary=(_clean_text(dict(escalation or {}).get("blocking_reason"))
                                 or terminal_reason or status),
                extra_context=diagnostics,
            )
        except Exception:
            # Escalation must never crash planning finalization
            pass
    return PlanningResult(
        status=status,
        task_direction=task_direction,
        rounds=rounds,
        final_plan=last_plan,
        final_review=last_review,
        final_review_active_scope=final_review_active_scope,
        approval=approval,
        escalation=escalation,
        business_outcome=_clean_text(contract_fields["business_outcome"]),
        out_of_scope=list(contract_fields["out_of_scope"]),
        source_of_truth=list(contract_fields["source_of_truth"]),
        source_of_truth_canonical=list(contract_fields["source_of_truth_canonical"]),
        path_type=_clean_text(contract_fields["path_type"]),
        layers=list(contract_fields["layers"]),
        coverage_hints=list(contract_fields["coverage_hints"]),
        module_boundaries=list(contract_fields["module_boundaries"]),
        verify_recipes=list(contract_fields["verify_recipes"]),
        approval_points=list(contract_fields["approval_points"]),
        execution_constraints=dict(contract_fields["execution_constraints"]),
        confidence=_clean_text(contract_fields["confidence"]),
        confidence_issues=list(contract_fields["confidence_issues"]),
        archetype=_clean_text(contract_fields["archetype"]),
        capabilities=list(contract_fields["capabilities"]),
        input_fingerprint=_clean_text(contract_fields["input_fingerprint"]),
        context_scout=dict(context_scout_payload),
        prompt_lesson_learning=dict(prompt_lesson_learning),
        planning_readiness=planning_readiness,
        meta_blocker_demotion_log=list(meta_blocker_demotion_log),
    )

__all__ = [
    "PlanningConfig",
    "PlanningResult",
    "PlanningRound",
    "plan_to_task_cards",
    "plan_to_task_graph",
    "result_to_artifact",
    "run_planning_conversation",
]


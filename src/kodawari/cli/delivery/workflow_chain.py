"""Workflow-chain helpers for develop-family runtime orchestration."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from kodawari.cli.delivery.workflow_chain_gate import (
    blocked_final_quality_review as _blocked_final_quality_review,
    chain_outcome_passed as _chain_outcome_passed,
    default_effective_final_outcome as _default_effective_final_outcome,
    effective_final_outcome as _effective_final_outcome,
    effective_outcome_blocked as _effective_outcome_blocked,
    effective_outcome_blocking_reason as _effective_outcome_blocking_reason,
    gate_summary as _gate_summary,
    normalized_chain_final_outcome as _normalized_chain_final_outcome,
    resolve_gate_blocking_reason,
    runtime_gate_payload as _runtime_gate_payload,
    workflow_chain_outcome as _workflow_chain_outcome,
)
from kodawari.cli.delivery.workflow_chain_outcome import (
    final_blocked_outcome as _final_blocked_outcome,
    final_review_phase_status as _final_review_phase_status,
    pass_outcome as _pass_outcome,
    status_from_bool as _status_from_bool,
    task_cycle_empty as _task_cycle_empty,
    task_entry_passed as _task_entry_passed,
)
from kodawari.cli.delivery.workflow_chain_review import (
    approval_blocking_reason as _approval_blocking_reason,
    approval_summary as _approval_summary,
    final_review_summary as _final_review_summary,
    gate_blocking_reason as _gate_blocking_reason,
    loop_blocking_reason as _loop_blocking_reason,
    peer_review_runtime as _peer_review_runtime,
    peer_review_runtime_blocking_reason as _peer_review_runtime_blocking_reason,
    task_cycle_blocked_reason as _task_cycle_blocked_reason,
    verify_blocking_reason as _verify_blocking_reason,
    verify_payload_from_autopilot as _verify_payload_from_autopilot,
)


TASK_HEADING_RE = re.compile(r"^#{2,6}\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.+?)\s*$")
TASK_CHECKLIST_RE = re.compile(r"^- \[( |x|X)\]\s*([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.+?)\s*$")
IMPLICIT_CHECKLIST_RE = re.compile(r"^- \[( |x|X)\]\s*(.+?)\s*$")
IMPLICIT_HEADING_RE = re.compile(r"^#{2,6}\s*(.+?)\s*$")
WORKFLOW_CHAIN_VERSION = "ws115.chain.v1"
_REASON_STOP_REASON: dict[str, str] = {
    "PROCEED_TO_GATE": "PASS",
    "PIPELINE_FINISH": "PASS",
    "VERIFY_BLOCKED": "HARD_ERROR",
    "GATE_BLOCKED": "HARD_ERROR",
    "OPUS_REVIEW_BLOCKED": "HARD_ERROR",
    "PROTECTED_FILE_BLOCK": "HARD_ERROR",
    "MAX_CYCLES_REACHED": "MAX_CYCLES",
    "COLLABORATION_ROUND_LIMIT": "STUCK",
    "IMPLEMENTATION_ERROR": "HARD_ERROR",
    "EXECUTOR_RECOVERY_REQUIRED": "STUCK",
}


def workflow_chain_snapshot_path(planning_dir: Path) -> Path:
    return (planning_dir / ".workflow_chain.json").resolve()


def load_workflow_chain_snapshot(planning_dir: Path) -> dict[str, Any] | None:
    path = workflow_chain_snapshot_path(planning_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_workflow_chain_snapshot(planning_dir: Path, payload: dict[str, Any]) -> Path:
    path = workflow_chain_snapshot_path(planning_dir)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def parse_task_backlog(
    tasks_path: Path,
    *,
    exclude_labels: set[str] | None = None,
    include_completed: bool = False,
) -> list[dict[str, Any]]:
    exclude = _normalized_exclude_labels(exclude_labels)
    if not tasks_path.exists():
        return []
    lines = tasks_path.read_text(encoding="utf-8").splitlines()
    explicit = _parse_task_lines(lines, exclude=exclude, implicit=False, include_completed=include_completed)
    if explicit:
        return explicit
    return _parse_task_lines(lines, exclude=exclude, implicit=True, include_completed=include_completed)


def build_upstream_result(
    *,
    task_label: str,
    peer_review_enabled: bool,
    payload: dict[str, Any],
) -> dict[str, Any]:
    verify = _verify_payload_from_autopilot(payload)
    gate = _gate_summary(_runtime_gate_payload(payload))
    approvals = _approval_summary(payload, peer_review_enabled=peer_review_enabled, gate=gate, verify=verify)
    peer_review_runtime = _peer_review_runtime(payload)
    stop_reason = _autopilot_stop_reason(payload)
    blocked = _autopilot_blocked(payload, stop_reason=stop_reason)
    passed = _autopilot_passed(payload) and approvals["all_passed"] and not blocked
    return {
        "task_label": task_label,
        "peer_review_enabled": bool(peer_review_enabled),
        "review_mode": {True: "multi_round", False: "single_pass"}[bool(peer_review_enabled)],
        "passed": passed,
        "status": {True: "PASS", False: "BLOCKED"}[passed],
        "reason": str(payload.get("reason") or ""),
        "stop_reason": stop_reason,
        "blocked": blocked,
        "round_outcome": _autopilot_round_outcome(payload),
        "verify": verify,
        "gate": gate,
        "approvals": approvals,
        "peer_review_runtime": peer_review_runtime,
        "loop_outcome": dict(payload.get("loop_outcome") or {}),
        "rounds_executed": len(list(payload.get("rounds") or [])),
    }


def build_task_entry_result(
    *,
    task: dict[str, str],
    autopilot_payload: dict[str, Any],
    peer_review_enabled: bool,
    gate_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    verify_payload = _verify_payload_from_autopilot(autopilot_payload)
    gate_summary = _gate_summary(gate_payload or _runtime_gate_payload(autopilot_payload))
    approvals = _approval_summary(
        autopilot_payload,
        peer_review_enabled=peer_review_enabled,
        gate=gate_summary,
        verify=verify_payload,
    )
    peer_review_runtime = _peer_review_runtime(autopilot_payload)
    stop_reason = _autopilot_stop_reason(autopilot_payload)
    blocked = _autopilot_blocked(autopilot_payload, stop_reason=stop_reason)
    autopilot_passed = _autopilot_passed(autopilot_payload)
    passed = _task_entry_passed(
        autopilot_passed=autopilot_passed,
        approvals=approvals,
        blocked=blocked,
    )
    return {
        "task_id": task["task_id"],
        "task_label": task["label"],
        "task_scope": task["scope"],
        "instinct_match_score": int(task.get("instinct_match_score", 0) or 0),
        "instinct_patterns": [str(item) for item in list(task.get("instinct_patterns") or []) if str(item).strip()],
        "peer_review_enabled": bool(peer_review_enabled),
        "autopilot_status": _status_from_bool(autopilot_passed),
        "autopilot_reason": str(autopilot_payload.get("reason") or ""),
        "stop_reason": stop_reason,
        "blocked": blocked,
        "round_outcome": _autopilot_round_outcome(autopilot_payload),
        "verify": verify_payload,
        "gate": gate_summary,
        "approvals": approvals,
        "peer_review_runtime": peer_review_runtime,
        "outcome": _status_from_bool(passed),
        "blocking_reason": _task_result_blocking_reason(
            passed=passed,
            autopilot_payload=autopilot_payload,
            gate_summary=gate_summary,
            approvals=approvals,
        ),
    }


def build_task_cycle_result(
    *,
    upstream_passed: bool,
    tasks: list[dict[str, str]],
    task_results: list[dict[str, Any]],
) -> dict[str, Any]:
    if not upstream_passed:
        return {
            "entered": False,
            "skipped": True,
            "skip_reason": "upstream_not_passed",
            "tasks_total": len(tasks),
            "tasks_completed": 0,
            "blocked": True,
            "blocked_task": None,
            "blocked_reason": "",
            "tasks": [],
        }
    blocked_task = next((item for item in task_results if item["outcome"] == "BLOCKED"), None)
    completed = sum(1 for item in task_results if item["outcome"] == "PASS")
    return {
        "entered": True,
        "skipped": False,
        "skip_reason": None,
        "tasks_total": len(tasks),
        "tasks_completed": completed,
        "blocked": blocked_task is not None,
        "blocked_task": blocked_task["task_label"] if blocked_task is not None else None,
        "blocked_reason": _task_cycle_blocked_reason(blocked_task),
        "tasks": task_results,
    }


def build_final_quality_review(
    *,
    upstream: dict[str, Any],
    task_cycle: dict[str, Any],
) -> dict[str, Any]:
    summary = _final_review_summary(upstream=upstream, task_cycle=task_cycle)
    return {
        "phase": "FINAL_QUALITY_REVIEW",
        "phase_status": _final_review_phase_status(summary["status"]),
        "review_source": "workflow_chain_aggregation",
        "status": summary["status"],
        "summary": summary["summary"],
        "blocking_reason": summary["blocking_reason"],
        "upstream_passed": bool(upstream.get("passed")),
        "task_cycle_entered": bool(task_cycle.get("entered")),
        "tasks_completed": int(task_cycle.get("tasks_completed", 0) or 0),
        "tasks_total": int(task_cycle.get("tasks_total", 0) or 0),
    }


def build_final_outcome(
    *,
    peer_review_enabled: bool,
    upstream: dict[str, Any],
    task_cycle: dict[str, Any],
    final_review: dict[str, Any],
) -> dict[str, Any]:
    blocked = _final_blocked_outcome(
        peer_review_enabled=peer_review_enabled,
        upstream=upstream,
        task_cycle=task_cycle,
        final_review=final_review,
    )
    if blocked is not None:
        return blocked
    if _task_cycle_empty(task_cycle):
        return _pass_outcome(
            peer_review_enabled=peer_review_enabled,
            task_cycle=task_cycle,
            reason="NO_TASKS_FOUND",
        )
    return _pass_outcome(
        peer_review_enabled=peer_review_enabled,
        task_cycle=task_cycle,
        reason="ALL_TASKS_COMPLETE",
    )


def build_workflow_chain_payload(
    *,
    feature: str,
    planning_dir: Path,
    peer_review_enabled: bool,
    task_cycle_enabled: bool,
    upstream: dict[str, Any],
    task_cycle: dict[str, Any],
    final_review: dict[str, Any],
    final_outcome: dict[str, Any],
) -> dict[str, Any]:
    chain_final_outcome = _normalized_chain_final_outcome(final_outcome)
    return {
        "version": WORKFLOW_CHAIN_VERSION,
        "feature": feature,
        "planning_dir": str(planning_dir.resolve()),
        "peer_review_enabled": bool(peer_review_enabled),
        "task_cycle_enabled": bool(task_cycle_enabled),
        "mode": {True: "peer_review", False: "single_pass"}[bool(peer_review_enabled)],
        "upstream": upstream,
        "task_cycle": task_cycle,
        "final_quality_review": final_review,
        "chain_final_outcome": chain_final_outcome,
        "final_outcome": _default_effective_final_outcome(chain_final_outcome),
    }


def bind_effective_gate_result(
    workflow_chain: dict[str, Any] | None,
    gate_payload: dict[str, Any] | None,
    *,
    state_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chain = dict(workflow_chain or {})
    if not chain:
        return {}
    chain["chain_final_outcome"] = _workflow_chain_outcome(chain)
    gate_summary = _gate_summary(gate_payload)
    chain["final_outcome"] = _effective_final_outcome(
        chain["chain_final_outcome"],
        gate_payload=gate_payload,
        state_payload=state_payload,
        gate_summary=gate_summary,
    )
    if not _chain_outcome_passed(chain["chain_final_outcome"]):
        return chain
    if not _effective_outcome_blocked(chain["final_outcome"]):
        return chain
    chain["final_quality_review"] = _blocked_final_quality_review(
        chain.get("final_quality_review"),
        blocking_reason=_effective_outcome_blocking_reason(chain["final_outcome"]),
    )
    return chain


def _normalized_exclude_labels(exclude_labels: set[str] | None) -> set[str]:
    return {str(item).strip() for item in set(exclude_labels or set()) if str(item).strip()}


def _skip_task_entry(
    task: dict[str, Any] | None,
    *,
    exclude: set[str],
    seen: set[str],
    include_completed: bool = False,
) -> bool:
    if task is None:
        return True
    if not include_completed and bool(task.get("completed", False)):
        return True
    if _skip_process_scope(str(task.get("scope") or "")):
        return True
    return task["label"] in exclude or task["task_id"] in seen


def _parse_task_lines(
    lines: list[str],
    *,
    exclude: set[str],
    implicit: bool,
    include_completed: bool = False,
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    parsed: list[dict[str, Any]] = []
    implicit_sequence = 1
    for raw in lines:
        task = _parse_task_line(raw, implicit=implicit, implicit_sequence=implicit_sequence)
        if task is not None and bool(task.get("implicit", False)):
            implicit_sequence += 1
        if _skip_task_entry(task, exclude=exclude, seen=seen, include_completed=include_completed):
            continue
        seen.add(task["task_id"])
        parsed.append(task)
    return parsed


def _parse_task_line(
    raw: str,
    *,
    implicit: bool,
    implicit_sequence: int,
) -> dict[str, Any] | None:
    line = str(raw).strip()
    if not line:
        return None
    heading = TASK_HEADING_RE.match(line)
    if heading:
        return _task_entry(task_id=heading.group(1), scope=heading.group(2), completed=False, implicit=False)
    checklist = TASK_CHECKLIST_RE.match(line)
    if checklist:
        mark = str(checklist.group(1) or "").strip().lower()
        return _task_entry(
            task_id=checklist.group(2),
            scope=checklist.group(3),
            completed=mark == "x",
            implicit=False,
        )
    if not implicit:
        return None
    return _parse_implicit_task(line, implicit_sequence=implicit_sequence)


def _parse_implicit_task(line: str, *, implicit_sequence: int) -> dict[str, Any] | None:
    return _implicit_checklist_task(line, implicit_sequence=implicit_sequence) or _implicit_heading_task(
        line,
        implicit_sequence=implicit_sequence,
    )


def _implicit_checklist_task(line: str, *, implicit_sequence: int) -> dict[str, Any] | None:
    checklist = IMPLICIT_CHECKLIST_RE.match(line)
    if not checklist:
        return None
    scope = str(checklist.group(2) or "").strip()
    if _skip_implicit_scope(scope):
        return None
    mark = str(checklist.group(1) or "").strip().lower()
    return _task_entry(
        task_id=_implicit_task_id(implicit_sequence),
        scope=scope,
        completed=mark == "x",
        implicit=True,
    )


def _implicit_heading_task(line: str, *, implicit_sequence: int) -> dict[str, Any] | None:
    heading = IMPLICIT_HEADING_RE.match(line)
    if not heading:
        return None
    scope = str(heading.group(1) or "").strip()
    if _skip_implicit_scope(scope):
        return None
    return _task_entry(
        task_id=_implicit_task_id(implicit_sequence),
        scope=scope,
        completed=False,
        implicit=True,
    )


_PROCESS_STEP_SCOPES: frozenset[str] = frozenset(
    {
        "run scoped verify",
        "run kodawari gate",
        "run verify",
        "run gate",
        "run the verify",
        "run the gate",
        "scoped verify",
        "kodawari gate",
    }
)
_PROCESS_STEP_PREFIXES: tuple[str, ...] = (
    "run scoped verify",
    "run kodawari gate",
    "run verify",
    "run gate",
)


def _skip_implicit_scope(scope: str) -> bool:
    text = str(scope or "").strip()
    if not text:
        return True
    lowered = text.lower()
    return lowered in {"tasks", "task", "task list", "tasklist"}


def _skip_process_scope(scope: str) -> bool:
    lowered = str(scope or "").strip().lower()
    if not lowered:
        return False
    if lowered in _PROCESS_STEP_SCOPES:
        return True
    return any(lowered.startswith(prefix) for prefix in _PROCESS_STEP_PREFIXES)


def _implicit_task_id(sequence: int) -> str:
    return f"TASK{int(sequence):03d}"


def _task_entry(*, task_id: str, scope: str, completed: bool, implicit: bool) -> dict[str, Any]:
    normalized_id = str(task_id).strip().upper()
    normalized_scope = str(scope).strip()
    return {
        "task_id": normalized_id,
        "label": f"{normalized_id}: {normalized_scope}",
        "scope": normalized_scope,
        "completed": bool(completed),
        "implicit": bool(implicit),
    }


def _autopilot_passed(payload: dict[str, Any]) -> bool:
    return str(payload.get("reason") or "").upper() in {"PROCEED_TO_GATE", "PIPELINE_FINISH"}


def _reason_text(payload: dict[str, Any]) -> str:
    return str(payload.get("reason") or "").strip().upper()


def _loop_outcome_payload(payload: dict[str, Any]) -> dict[str, Any]:
    loop_outcome = payload.get("loop_outcome")
    return dict(loop_outcome) if isinstance(loop_outcome, dict) else {}


def _unified_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    unified = payload.get("unified_status")
    return dict(unified) if isinstance(unified, dict) else {}


def _autopilot_stop_reason(payload: dict[str, Any]) -> str:
    loop_outcome = _loop_outcome_payload(payload)
    explicit = str(loop_outcome.get("stop_reason") or "").strip().upper()
    if explicit:
        return explicit
    unified = _unified_status_payload(payload)
    unified_reason = str(unified.get("stop_reason") or "").strip().upper()
    if unified_reason:
        return unified_reason
    return _REASON_STOP_REASON.get(_reason_text(payload), "")


def _autopilot_round_outcome(payload: dict[str, Any]) -> str:
    loop_outcome = _loop_outcome_payload(payload)
    explicit = str(loop_outcome.get("round_outcome") or "").strip().lower()
    if explicit:
        return explicit
    stop_reason = _autopilot_stop_reason(payload)
    if stop_reason == "PASS":
        return "ready_for_gate"
    if _autopilot_blocked(payload, stop_reason=stop_reason):
        return "blocked"
    return "unknown"


def _autopilot_blocked(payload: dict[str, Any], *, stop_reason: str) -> bool:
    loop_outcome = _loop_outcome_payload(payload)
    if isinstance(loop_outcome.get("blocked"), bool):
        return bool(loop_outcome.get("blocked"))
    unified = _unified_status_payload(payload)
    if isinstance(unified.get("is_blocked"), bool):
        return bool(unified.get("is_blocked"))
    return stop_reason in {"HARD_ERROR", "MAX_CYCLES", "STUCK", "TOKEN_BUDGET"}


def _task_blocking_reason(
    *,
    autopilot_payload: dict[str, Any],
    gate_summary: dict[str, Any],
) -> str:
    stop_reason = _autopilot_stop_reason(autopilot_payload)
    for reason in (
        _loop_blocking_reason(autopilot_payload),
        _verify_blocking_reason(_verify_payload_from_autopilot(autopilot_payload)),
        _gate_blocking_reason(gate_summary),
        _stop_reason_blocking_reason(stop_reason),
        str(autopilot_payload.get("reason") or "").strip(),
    ):
        if reason:
            return reason
    return ""


def _stop_reason_blocking_reason(stop_reason: str) -> str:
    normalized = str(stop_reason or "").strip().upper()
    if normalized and normalized != "PASS":
        return normalized
    return ""


def _task_result_blocking_reason(
    *,
    passed: bool,
    autopilot_payload: dict[str, Any],
    gate_summary: dict[str, Any],
    approvals: dict[str, Any],
) -> str:
    if passed:
        return ""
    peer_reason = _peer_review_runtime_blocking_reason(_peer_review_runtime(autopilot_payload))
    task_reason = _task_blocking_reason(autopilot_payload=autopilot_payload, gate_summary=gate_summary)
    approval_reason = _approval_blocking_reason(approvals)
    stop_reason = _autopilot_stop_reason(autopilot_payload)
    if _autopilot_blocked(autopilot_payload, stop_reason=stop_reason):
        return peer_reason or task_reason or approval_reason
    return peer_reason or approval_reason or task_reason


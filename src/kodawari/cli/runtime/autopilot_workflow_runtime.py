"""Workflow runtime helpers used by the autopilot CLI command."""

from __future__ import annotations

import argparse
import fnmatch
import json
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
from typing import Any, Callable

from kodawari.autopilot.engine.engine_support import task_id_from_label
from kodawari.autopilot.core.state import SubtaskCheckpoint, SubtaskStatus
from kodawari.cli.contract.contract_first_backlog import activate_task_card, task_graph_backlog_entries
from kodawari.cli.delivery.workflow_chain import (
    build_final_outcome,
    build_final_quality_review,
    build_task_cycle_result,
    build_task_entry_result,
    build_upstream_result,
    build_workflow_chain_payload,
    parse_task_backlog,
)
from kodawari.gate import GateEngine


TaskFactory = Callable[[str, str], tuple[str, str]]
logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bool_flag(args: argparse.Namespace, name: str, default: bool) -> bool:
    value = getattr(args, name, default)
    return bool(default if value is None else value)


def resolve_primary_task(
    args: argparse.Namespace,
    *,
    feature: str,
    requirements_text: str,
    default_task_factory: TaskFactory,
    planning_task_factory: TaskFactory,
) -> tuple[str, str]:
    explicit = _explicit_task(args)
    if explicit is not None:
        return explicit
    if bool_flag(args, "task_cycle", True):
        return planning_task_factory(feature, requirements_text)
    return default_task_factory(feature, requirements_text)


def build_workflow_chain_runtime(
    *,
    args: argparse.Namespace,
    engine: Any,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    upstream_task_label: str,
    upstream_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if not bool_flag(args, "task_cycle", True):
        return None, []

    peer_review_enabled = bool_flag(args, "enable_peer_review", True)
    upstream = build_upstream_result(
        task_label=upstream_task_label,
        peer_review_enabled=peer_review_enabled,
        payload=upstream_payload,
    )
    task_cycle, task_rounds = _task_cycle_runtime(
        engine=engine,
        project_root=project_root,
        planning_dir=planning_dir,
        peer_review_enabled=peer_review_enabled,
        upstream_task_label=upstream_task_label,
        upstream_passed=bool(upstream["passed"]),
    )
    final_review = build_final_quality_review(upstream=upstream, task_cycle=task_cycle)
    final_outcome = build_final_outcome(
        peer_review_enabled=peer_review_enabled,
        upstream=upstream,
        task_cycle=task_cycle,
        final_review=final_review,
    )
    return (
        build_workflow_chain_payload(
            feature=feature,
            planning_dir=planning_dir,
            peer_review_enabled=peer_review_enabled,
            task_cycle_enabled=True,
            upstream=upstream,
            task_cycle=task_cycle,
            final_review=final_review,
            final_outcome=final_outcome,
        ),
        task_rounds,
    )


def autopilot_payload_status(
    *,
    run_result: dict[str, Any],
    workflow_chain: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    final_outcome = _final_outcome_payload(workflow_chain)
    if final_outcome:
        return _status_from_final_outcome(final_outcome), final_outcome
    status = "ok" if run_result.get("reason") in {"PROCEED_TO_GATE", "PIPELINE_FINISH"} else "blocked"
    return status, {}


def _explicit_task(args: argparse.Namespace) -> tuple[str, str] | None:
    task_label = str(getattr(args, "task_label", "") or "").strip()
    if not task_label:
        return None
    task_scope = str(getattr(args, "task_scope", "") or "").strip()
    if task_scope:
        return task_label, task_scope
    return task_label, _scope_from_label(task_label)


def _scope_from_label(task_label: str) -> str:
    if ":" in task_label:
        return task_label.split(":", 1)[1].strip() or task_label
    return task_label


_TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS = 2


def _task_cycle_runtime(
    *,
    engine: Any,
    project_root: Path,
    planning_dir: Path,
    peer_review_enabled: bool,
    upstream_task_label: str,
    upstream_passed: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tasks = _planned_tasks(planning_dir, upstream_task_label)
    if not upstream_passed:
        return build_task_cycle_result(upstream_passed=False, tasks=tasks, task_results=[]), []
    if not tasks:
        return build_task_cycle_result(upstream_passed=True, tasks=[], task_results=[]), []
    _reset_state_for_task_cycle(engine)
    # Honor caller's peer_review_enabled (typically driven by --real-peer-review).
    # Previously hardcoded False here silently bypassed user intent — greenfield
    # bootstrap projects with multiple tasks ALWAYS run in task-cycle mode, so the
    # hardcode meant peer review never fired for greenfield no matter what flag
    # the user passed.
    #
    # Budget cap: when enabled here, also cap per-entry max_rounds at
    # _TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS so an N-entry backlog can't multiply
    # into N × full review loop. This preserves the original cost-control intent
    # of the hardcoded skip while not silently ignoring user flag.
    _apply_task_cycle_peer_review_cap(engine, peer_review_enabled)
    return _completed_task_cycle(
        engine=engine,
        project_root=project_root,
        planning_dir=planning_dir,
        tasks=tasks,
        peer_review_enabled=peer_review_enabled,
    )


def _apply_task_cycle_peer_review_cap(engine: Any, peer_review_enabled: bool) -> None:
    """Cap collaboration_max_rounds for task-cycle entries when peer review is
    active. The cap (_TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS) keeps an N-entry backlog
    from multiplying review rounds beyond a sensible per-entry ceiling. No-op
    when peer review is off (single-pass already bounded) or when caller's
    config is already smaller than the cap (don't widen)."""
    if not peer_review_enabled:
        return
    config = getattr(engine, "config", None)
    if config is None or not hasattr(config, "collaboration_max_rounds"):
        return
    current = int(getattr(config, "collaboration_max_rounds", 0) or 0)
    if 0 < current <= _TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS:
        return
    try:
        config.collaboration_max_rounds = _TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS
    except (AttributeError, TypeError):
        # Frozen / non-settable config; leave the caller's value in place.
        pass


def _planned_tasks(planning_dir: Path, upstream_task_label: str) -> list[dict[str, str]]:
    completed_labels = _completed_task_labels_from_state(planning_dir)
    completed_task_ids = _task_ids_from_labels(completed_labels)
    exclude_task_ids = set(completed_task_ids)
    upstream_task_id = task_id_from_label(upstream_task_label)
    if upstream_task_id:
        exclude_task_ids.add(upstream_task_id)
    contract_first_tasks = task_graph_backlog_entries(
        planning_dir,
        exclude_task_ids=exclude_task_ids,
        completed_task_ids=completed_task_ids,
    )
    contract_first_tasks = _filter_completed_task_entries(
        contract_first_tasks,
        completed_task_ids=completed_task_ids,
        completed_labels=completed_labels,
    )
    if contract_first_tasks:
        return _prioritize_tasks_by_instincts(planning_dir.parent.parent, contract_first_tasks)
    tasks = parse_task_backlog(
        planning_dir / "TASKS.md",
        exclude_labels={upstream_task_label, *completed_labels},
    )
    tasks = _filter_completed_task_entries(
        tasks,
        completed_task_ids=completed_task_ids,
        completed_labels=completed_labels,
    )
    return _prioritize_tasks_by_instincts(planning_dir.parent.parent, tasks)


def _completed_task_labels_from_state(planning_dir: Path) -> set[str]:
    state_path = planning_dir / ".autopilot_state.json"
    if not state_path.exists():
        return set()
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("failed to read completed tasks from autopilot state", exc_info=True)
        return set()
    raw_items = payload.get("completed_tasks") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        return set()
    labels: set[str] = set()
    for item in raw_items:
        label = _completed_task_label(item)
        if label:
            labels.add(label)
    return labels


def _completed_task_label(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("task_label", "label", "task_id"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""
    return str(item or "").strip()


def _task_ids_from_labels(labels: set[str]) -> set[str]:
    return {task_id_from_label(label) for label in labels if task_id_from_label(label)}


def _filter_completed_task_entries(
    tasks: list[dict[str, str]],
    *,
    completed_task_ids: set[str],
    completed_labels: set[str],
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    for task in tasks:
        label = str(task.get("label") or "").strip()
        task_id = str(task.get("task_id") or task_id_from_label(label)).strip().upper()
        if label in completed_labels or task_id in completed_task_ids:
            continue
        filtered.append(task)
    return filtered


def _prioritize_tasks_by_instincts(project_root: Path, tasks: list[dict[str, str]]) -> list[dict[str, str]]:
    hints = _load_instinct_hints(project_root)
    if not hints or not tasks:
        return tasks
    scored = [_task_with_instinct_score(task, hints=hints) for task in tasks]
    return sorted(scored, key=lambda item: int(item.get("instinct_match_score", 0) or 0), reverse=True)


def _load_instinct_hints(project_root: Path) -> list[str]:
    selector = _resolve_instinct_selector()
    if selector is None:
        return []
    payload = _safe_select_instinct_hints(selector, project_root)
    return _normalize_instinct_patterns(payload)


def _resolve_instinct_selector() -> Any | None:
    try:
        from kodawari.instincts import select_instinct_hints
    except Exception:
        logger.warning("instinct selector unavailable during autopilot workflow runtime", exc_info=True)
        return None
    return select_instinct_hints


def _safe_select_instinct_hints(selector: Any, project_root: Path) -> list[dict[str, Any]]:
    try:
        payload = selector(project_root, limit=20, min_confidence=0.5)
    except Exception:
        logger.warning("instinct hint selection failed during autopilot workflow runtime", exc_info=True)
        return []
    return [dict(item) for item in payload if isinstance(item, dict)]


def _normalize_instinct_patterns(payload: list[dict[str, Any]]) -> list[str]:
    patterns: list[str] = []
    for item in payload:
        pattern = str(item.get("pattern") or "").strip()
        if pattern:
            patterns.append(pattern)
    return patterns


def _task_with_instinct_score(task: dict[str, str], *, hints: list[str]) -> dict[str, str]:
    patterns = _matching_instinct_patterns(task, hints=hints)
    enriched = dict(task)
    enriched["instinct_patterns"] = patterns
    enriched["instinct_match_score"] = len(patterns)
    return enriched


def _matching_instinct_patterns(task: dict[str, str], *, hints: list[str]) -> list[str]:
    text = _task_match_text(task)
    matches: list[str] = []
    for pattern in hints:
        if _task_matches_instinct_pattern(text, pattern):
            matches.append(pattern)
    return matches


def _task_match_text(task: dict[str, str]) -> str:
    parts = [
        str(task.get("task_id") or "").strip(),
        str(task.get("label") or "").strip(),
        str(task.get("scope") or "").strip(),
    ]
    return " ".join(part for part in parts if part).replace("\\", "/").lower()


def _task_matches_instinct_pattern(text: str, pattern: str) -> bool:
    normalized = str(pattern or "").strip().replace("\\", "/").lower()
    if not normalized:
        return False
    if normalized in text:
        return True
    return fnmatch.fnmatch(text, f"*{normalized}*")


def _completed_task_cycle(
    *,
    engine: Any,
    project_root: Path,
    planning_dir: Path,
    tasks: list[dict[str, str]],
    peer_review_enabled: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    results: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    for task in tasks:
        task_result, task_rounds = _task_execution_result(
            engine=engine,
            project_root=project_root,
            planning_dir=planning_dir,
            task=task,
            peer_review_enabled=peer_review_enabled,
        )
        results.append(task_result)
        rounds.extend(task_rounds)
        if task_result["outcome"] == "PASS":
            _mark_task_completed(engine, task["label"])
        if task_result["outcome"] == "BLOCKED":
            break
    return build_task_cycle_result(upstream_passed=True, tasks=tasks, task_results=results), rounds


def _task_execution_result(
    *,
    engine: Any,
    project_root: Path,
    planning_dir: Path,
    task: dict[str, str],
    peer_review_enabled: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    _reset_state_for_task_entry(engine)
    _activate_task_scope(engine, planning_dir=planning_dir, task=task)
    subtask_id = _mark_task_running_state(engine, task)
    autopilot_payload = engine.run_collaboration_loop(
        task_label=task["label"],
        task_scope=task["scope"],
        enable_peer_review=peer_review_enabled,
    )
    gate_payload = _task_gate_payload(project_root, autopilot_payload)
    task_result = build_task_entry_result(
        task=task,
        autopilot_payload=autopilot_payload,
        peer_review_enabled=peer_review_enabled,
        gate_payload=gate_payload,
    )
    _mark_task_terminal_state(
        engine,
        task=task,
        subtask_id=subtask_id,
        task_result=task_result,
        autopilot_payload=autopilot_payload,
    )
    return (
        task_result,
        list(autopilot_payload.get("rounds", [])),
    )


def _task_gate_payload(project_root: Path, autopilot_payload: dict[str, Any]) -> dict[str, Any] | None:
    runtime_gate = autopilot_payload.get("gate_check")
    if isinstance(runtime_gate, dict):
        return dict(runtime_gate)
    if str(autopilot_payload.get("reason") or "").upper() not in {"PROCEED_TO_GATE", "PIPELINE_FINISH"}:
        return None
    report = GateEngine(project_root=project_root).evaluate(
        targets=[project_root],
        profile_name="advisory",
    )
    return report.to_dict()


def _reset_state_for_task_cycle(engine: Any) -> None:
    _reset_state_for_task_entry(engine)
    engine.state.subtasks = {}


def _reset_state_for_task_entry(engine: Any) -> None:
    engine.state.cycle = 0
    engine.state.stop_reason = None
    engine.state.final_status = None
    engine.state.last_stage_status = None
    engine.state.last_error = None
    engine.state.active_task = None
    engine.state.active_subtask = None


def _mark_task_completed(engine: Any, task_label: str) -> None:
    if task_label in engine.state.completed_tasks:
        return
    engine.state.completed_tasks.append(task_label)


def _task_cycle_subtask_id(task: dict[str, str]) -> str:
    return f"{str(task['task_id']).upper()}.TASK_CYCLE"


def _mark_task_running_state(engine: Any, task: dict[str, str]) -> str:
    subtask_id = _task_cycle_subtask_id(task)
    previous = engine.state.get_subtask(subtask_id)
    attempt = int(previous.attempt or 0) + 1 if previous is not None else 1
    checkpoint = SubtaskCheckpoint(
        subtask_id=subtask_id,
        title=str(task["label"]),
        parent_task_id=str(task["task_id"]),
        status=SubtaskStatus.RUNNING,
        attempt=attempt,
        started_at=_utc_now_iso(),
        verify_cmd="pytest -q",
    )
    checkpoint.tokens_used = int(engine.state.tokens_used or 0)
    engine.state.add_subtask(checkpoint)
    engine.state.active_subtask = subtask_id
    return subtask_id


def _activate_task_scope(engine: Any, *, planning_dir: Path, task: dict[str, str]) -> None:
    payload = activate_task_card(planning_dir, str(task.get("task_id") or ""))
    if payload is None:
        return
    engine._task_card_payload = dict(payload)
    _apply_task_cycle_verify_cmd(engine, payload)
    try:
        engine.config.task_card_path = (planning_dir / "TASK_CARD_ACTIVE.json").resolve()
    except Exception:
        logger.debug("failed to update engine task_card_path for task-cycle task", exc_info=True)


def _apply_task_cycle_verify_cmd(engine: Any, task_card: dict[str, Any]) -> None:
    """Prefer per-task verification while task-cycle is executing.

    A broad CLI --verify-cmd still belongs to the final quality gate, but using
    it inside a single task can pull future task assertions into the current
    task and create artificial recovery churn.
    """
    original_attr = "_task_cycle_original_verify_cmd"
    if not hasattr(engine, original_attr):
        setattr(engine, original_attr, str(getattr(engine.config, "verify_cmd", "") or "").strip())
    original = str(getattr(engine, original_attr, "") or "").strip()
    if str(os.environ.get("WORKFLOW_TASK_CYCLE_FORCE_GLOBAL_VERIFY") or "").strip().lower() in {"1", "true", "yes", "on"}:
        engine.config.verify_cmd = original
        return
    card_verify = str(task_card.get("verify_cmd") or "").strip()
    engine.config.verify_cmd = card_verify or original


def _mark_task_terminal_state(
    engine: Any,
    *,
    task: dict[str, str],
    subtask_id: str,
    task_result: dict[str, Any],
    autopilot_payload: dict[str, Any],
) -> None:
    checkpoint = engine.state.get_subtask(subtask_id)
    if checkpoint is None:
        checkpoint = SubtaskCheckpoint(
            subtask_id=subtask_id,
            title=str(task["label"]),
            parent_task_id=str(task["task_id"]),
        )
    checkpoint.status = _task_cycle_status(task_result)
    checkpoint.verify_status = _task_verify_status(task_result)
    checkpoint.verify_output = _task_verify_output(task_result)
    checkpoint.error = _task_error_message(task_result, autopilot_payload)
    checkpoint.changed_files = _task_changed_files(task_result)
    checkpoint.tokens_used = int(engine.state.tokens_used or 0)
    checkpoint.completed_at = _utc_now_iso()
    engine.state.add_subtask(checkpoint)
    engine.state.active_subtask = None


def _task_cycle_status(task_result: dict[str, Any]) -> SubtaskStatus:
    if str(task_result.get("outcome") or "").upper() == "PASS":
        return SubtaskStatus.DONE
    return SubtaskStatus.FAILED


def _task_verify_status(task_result: dict[str, Any]) -> str:
    verify = dict(task_result.get("verify") or {})
    status = str(verify.get("status") or "").upper()
    if status:
        return status
    return str(task_result.get("outcome") or "").upper()


def _task_verify_output(task_result: dict[str, Any]) -> str | None:
    verify = dict(task_result.get("verify") or {})
    for key in ("summary", "blocking_reason"):
        message = str(verify.get(key) or "").strip()
        if message:
            return message
    return None


def _task_error_message(task_result: dict[str, Any], autopilot_payload: dict[str, Any]) -> str | None:
    if str(task_result.get("outcome") or "").upper() == "PASS":
        return None
    blocking_reason = str(task_result.get("blocking_reason") or "").strip()
    if blocking_reason:
        return blocking_reason
    return str(autopilot_payload.get("reason") or "TASK_BLOCKED")


def _task_changed_files(task_result: dict[str, Any]) -> list[str]:
    verify = dict(task_result.get("verify") or {})
    artifacts = verify.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [str(item) for item in artifacts if str(item).strip()]


def _final_outcome_payload(workflow_chain: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(workflow_chain, dict):
        return {}
    final_outcome = workflow_chain.get("final_outcome")
    return dict(final_outcome) if isinstance(final_outcome, dict) else {}


def _status_from_final_outcome(final_outcome: dict[str, Any]) -> str:
    status = str(final_outcome.get("status") or "").upper()
    return "ok" if status in {"PASS", "READY_FOR_GATE"} else "blocked"



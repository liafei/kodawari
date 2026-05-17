"""Planning artifact and task graph conversion helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.task_modes import is_verification_only_task
from kodawari.autopilot.planning.planning_agent import _upstream_new_files_by_task
from kodawari.autopilot.planning.planning_validators import (
    check_missing_source_files,
    normalize_planning_path,
    path_comparison_is_case_insensitive,
    planning_path_key,
)
from kodawari.autopilot.planning.task_card import build_task_card


SCHEMA_VERSION = "planning.conversation.v1"
TASK_GRAPH_SCHEMA_VERSION = "contract_first.task_graph.v1"

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _clean_text(value: Any) -> str:
    return str(value or "").strip()

def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item) for item in value if _clean_text(item)]

def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]

def _plan_tasks(plan_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return _dict_list(plan_payload.get("tasks"))

def _task_dependency_closure(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    deps_by_id: dict[str, set[str]] = {}
    for task in tasks:
        task_id = _clean_text(task.get("task_id"))
        if task_id:
            deps_by_id[task_id] = {_clean_text(dep) for dep in list(task.get("depends_on") or []) if _clean_text(dep)}
    closure: dict[str, set[str]] = {task_id: set() for task_id in deps_by_id}
    for task_id in deps_by_id:
        stack = list(deps_by_id.get(task_id, set()))
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            closure[task_id].add(current)
            stack.extend(deps_by_id.get(current, set()))
    return closure

def _serialize_parallel_write_conflicts(
    plan_payload: dict[str, Any],
    *,
    project_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tasks = [dict(item) for item in list(plan_payload.get("tasks") or []) if isinstance(item, dict)]
    if len(tasks) < 2:
        return plan_payload, []
    case_insensitive = path_comparison_is_case_insensitive(project_root)
    log: list[dict[str, Any]] = []
    changed = True
    while changed:
        changed = False
        closure = _task_dependency_closure(tasks)
        for index, current in enumerate(tasks):
            current_id = _clean_text(current.get("task_id"))
            if not current_id:
                continue
            current_files = {
                planning_path_key(normalize_planning_path(path), case_insensitive=case_insensitive): normalize_planning_path(path)
                for path in list(current.get("files_to_change") or [])
                if _clean_text(path)
            }
            for previous in tasks[:index]:
                previous_id = _clean_text(previous.get("task_id"))
                if not previous_id:
                    continue
                if previous_id in closure.get(current_id, set()) or current_id in closure.get(previous_id, set()):
                    continue
                previous_files = {
                    planning_path_key(normalize_planning_path(path), case_insensitive=case_insensitive): normalize_planning_path(path)
                    for path in list(previous.get("files_to_change") or [])
                    if _clean_text(path)
                }
                shared = sorted(set(current_files) & set(previous_files))
                if not shared:
                    continue
                depends = _string_list(current.get("depends_on"))
                if previous_id not in depends:
                    current["depends_on"] = [*depends, previous_id]
                    log.append(
                        {
                            "resolution": "serialized_parallel_write_conflict",
                            "task_id": current_id,
                            "depends_on_added": previous_id,
                            "shared_files": [current_files[key] for key in shared],
                        }
                    )
                    changed = True
                    break
            if changed:
                break
    if not log:
        return plan_payload, []
    resolved = dict(plan_payload)
    resolved["tasks"] = tasks
    resolved["taskgraph_resolution_log"] = [*list(plan_payload.get("taskgraph_resolution_log") or []), *log]
    return resolved, log

def _split_tasks_if_needed(tasks: list[dict[str, Any]], *, splitter_enabled: bool = True) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    id_map: dict[str, list[str]] = {}
    for original in tasks:
        item = dict(original)
        task_id = _clean_text(item.get("task_id")) or "T"
        files = _string_list(item.get("files_to_change"))
        invariants = _string_list(item.get("invariants"))

        needs_files_split = len(files) > 3
        needs_invariants_split = splitter_enabled and len(invariants) > 2

        if not needs_files_split and not needs_invariants_split:
            id_map[task_id] = [task_id]
            expanded.append(item)
            continue

        generated_ids: list[str] = []

        if needs_invariants_split and needs_files_split:
            chunks_files = [files[i : i + 3] for i in range(0, len(files), 3)]
            chunks_inv = [invariants[i : i + 2] for i in range(0, len(invariants), 2)]
            max_idx = max(len(chunks_files), len(chunks_inv))
            for idx in range(max_idx):
                suffix = chr(ord("a") + idx)
                split_id = f"{task_id}_{suffix}"
                generated_ids.append(split_id)
                split_item = dict(item)
                split_item["task_id"] = split_id
                split_item["task_name"] = f"{_clean_text(item.get('task_name'))} ({suffix})".strip()
                split_item["files_to_change"] = chunks_files[idx] if idx < len(chunks_files) else chunks_files[-1]
                split_item["invariants"] = chunks_inv[idx] if idx < len(chunks_inv) else chunks_inv[-1]
                split_item["parent_task_id"] = task_id
                split_item["split_metadata"] = {
                    "splitter_model": "task_splitter",
                    "splitter_version": "1.0",
                    "sub_index": idx,
                    "sibling_count": max_idx,
                    "splitter_status": "split_by_both_axes",
                }
                original_new = set(_string_list(item.get("new_files")))
                split_item["new_files"] = [path for path in split_item["files_to_change"] if path in original_new]
                split_item["depends_on"] = [generated_ids[idx - 1]] if idx > 0 else _string_list(item.get("depends_on"))
                expanded.append(split_item)
        elif needs_invariants_split:
            chunks_inv = [invariants[i : i + 2] for i in range(0, len(invariants), 2)]
            for idx, chunk in enumerate(chunks_inv):
                suffix = chr(ord("a") + idx)
                split_id = f"{task_id}_{suffix}"
                generated_ids.append(split_id)
                split_item = dict(item)
                split_item["task_id"] = split_id
                split_item["task_name"] = f"{_clean_text(item.get('task_name'))} ({suffix})".strip()
                split_item["invariants"] = chunk
                split_item["parent_task_id"] = task_id
                split_item["split_metadata"] = {
                    "splitter_model": "task_splitter",
                    "splitter_version": "1.0",
                    "sub_index": idx,
                    "sibling_count": len(chunks_inv),
                    "splitter_status": "split_by_invariants",
                }
                split_item["depends_on"] = [generated_ids[idx - 1]] if idx > 0 else _string_list(item.get("depends_on"))
                expanded.append(split_item)
        else:
            chunks = [files[i : i + 3] for i in range(0, len(files), 3)]
            for idx, chunk in enumerate(chunks):
                suffix = chr(ord("a") + idx)
                split_id = f"{task_id}_{suffix}"
                generated_ids.append(split_id)
                split_item = dict(item)
                split_item["task_id"] = split_id
                split_item["task_name"] = f"{_clean_text(item.get('task_name'))} ({suffix})".strip()
                split_item["files_to_change"] = chunk
                split_item["parent_task_id"] = task_id
                split_item["split_metadata"] = {
                    "splitter_model": "task_splitter",
                    "splitter_version": "1.0",
                    "sub_index": idx,
                    "sibling_count": len(chunks),
                    "splitter_status": "split_by_files",
                }
                original_new = set(_string_list(item.get("new_files")))
                split_item["new_files"] = [path for path in chunk if path in original_new]
                split_item["depends_on"] = [generated_ids[idx - 1]] if idx > 0 else _string_list(item.get("depends_on"))
                expanded.append(split_item)

        id_map[task_id] = generated_ids

    for item in expanded:
        depends = _string_list(item.get("depends_on"))
        remapped: list[str] = []
        for dep in depends:
            mapped = id_map.get(dep)
            if mapped:
                remapped.append(mapped[-1])
            else:
                remapped.append(dep)
        item["depends_on"] = remapped
    return expanded

def plan_to_task_graph(
    final_plan: dict[str, Any],
    *,
    feature: str,
    repo_inventory: dict[str, Any],
    project_root: Path,
    splitter_enabled: bool = True,
) -> dict[str, Any]:
    final_plan, _resolution_log = _serialize_parallel_write_conflicts(
        final_plan,
        project_root=project_root,
    )
    raw_tasks = _split_tasks_if_needed(_plan_tasks(final_plan), splitter_enabled=splitter_enabled)
    upstream_new_files_by_task = _upstream_new_files_by_task(raw_tasks)
    graph_tasks: list[dict[str, Any]] = []
    graph_issues: list[str] = []
    for item in raw_tasks:
        task_id = _clean_text(item.get("task_id")) or f"T{len(graph_tasks) + 1}"
        core_files = _string_list(item.get("files_to_change"))[:3]
        new_files = set(_string_list(item.get("new_files")))
        scoped_new_files = [path for path in core_files if path in new_files]
        missing = check_missing_source_files(
            core_files,
            task_new_files=new_files,
            upstream_new_files=upstream_new_files_by_task.get(task_id, set()),
            project_root=project_root,
        )
        task_exec_status = "FAIL" if missing else "PASS"
        task_exec_issues = [f"{task_id}: missing source file: {path}" for path in missing]
        graph_issues.extend(task_exec_issues)
        task_payload: dict[str, Any] = {
            "task_id": task_id,
            "task_name": _clean_text(item.get("task_name")) or task_id,
            "depends_on": _string_list(item.get("depends_on")),
            "core_files": core_files,
            "new_files": scoped_new_files,
            "layer_owner": _clean_text(item.get("layer_owner")).lower() or "service",
            "surface": _clean_text(item.get("surface")) or "backend",
            "invariants": _string_list(item.get("invariants"))[:5],
            "test_proof": _clean_text(item.get("test_plan") or item.get("verify_cmd")),
            "verify_cmd": _clean_text(item.get("verify_cmd") or item.get("test_plan")),
            "coverage_hints": _string_list(item.get("coverage_hints")),
            "executability": {"status": task_exec_status, "issues": task_exec_issues},
        }
        execution_constraints = {}
        plan_constraints = final_plan.get("execution_constraints")
        task_constraints = item.get("execution_constraints")
        if isinstance(plan_constraints, dict):
            execution_constraints.update(plan_constraints)
        if isinstance(task_constraints, dict):
            execution_constraints.update(task_constraints)
        if execution_constraints and (
            is_verification_only_task(final_plan, item) or isinstance(task_constraints, dict)
        ):
            task_payload["execution_constraints"] = execution_constraints
        for field in ("do_not_change", "read_only_files", "related_existing_tests", "review_focus", "forbidden_changes"):
            values = _string_list(item.get(field))
            if values:
                task_payload[field] = values
        for field in (
            "target_symbols",
            "read_only_symbols",
            "behavior_changes",
            "allowed_test_mutations",
            "provides",
            "requires",
            "api_contracts",
        ):
            values = _dict_list(item.get(field))
            if values:
                task_payload[field] = values
        freshness = item.get("freshness")
        if isinstance(freshness, dict) and freshness:
            task_payload["freshness"] = dict(freshness)
        graph_tasks.append(task_payload)
    return {
        "schema_version": TASK_GRAPH_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "feature": feature,
        "business_outcome": _clean_text(final_plan.get("business_outcome") or final_plan.get("summary")),
        "planning_mode": _clean_text(repo_inventory.get("mode")) or "existing",
        "archetype": _clean_text(final_plan.get("archetype") or repo_inventory.get("archetype")) or "auto",
        "capabilities": _string_list(final_plan.get("capabilities") or repo_inventory.get("capabilities")),
        "surfaces": sorted(
            {
                _clean_text(task.get("surface"))
                for task in graph_tasks
                if _clean_text(task.get("surface"))
            }
        ),
        "project_layout": dict(repo_inventory.get("project_layout") or {}),
        "project_profile": _clean_text(repo_inventory.get("project_profile")) or "python",
        "coverage_hints": _string_list(final_plan.get("coverage_hints")),
        "taskgraph_resolution_log": [dict(item) for item in _dict_list(final_plan.get("taskgraph_resolution_log"))],
        "boundary_debt": {
            "status": "PASS",
            "details": "model-planned tasks are mapped directly from planner output",
            "items": [],
        },
        "tasks": graph_tasks,
        "executability": {
            "status": "FAIL" if graph_issues else "PASS",
            "issues": graph_issues,
        },
    }

def plan_to_task_cards(final_plan: dict[str, Any], task_graph: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = [dict(item) for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)]
    cards: list[dict[str, Any]] = []
    for task in tasks:
        task_id = _clean_text(task.get("task_id"))
        if not task_id:
            continue
        cards.append(build_task_card(task_graph, task_id))
    return cards

def result_to_artifact(result: PlanningResult) -> dict[str, Any]:
    rounds_payload = []
    for round_item in result.rounds:
        rounds_payload.append(
            {
                "round_number": int(round_item.round_number),
                "plan_payload": dict(round_item.plan_payload),
                "review_payload": dict(round_item.review_payload or {}),
                "planner_error": _clean_text(round_item.planner_error),
                "review_error": _clean_text(round_item.review_error),
                "structural_issues": list(round_item.structural_issues),
                "blocking_findings_count": int(round_item.blocking_findings_count),
                "blocking_findings": [dict(item) for item in getattr(round_item, "blocking_findings", [])],
                "timestamp": _clean_text(round_item.timestamp),
                "path_resolution": dict(round_item.path_resolution),
                "planner_diagnostics": dict(round_item.planner_diagnostics),
                "deterministic_repairs": [dict(item) for item in round_item.deterministic_repairs],
                "planning_readiness": dict(getattr(round_item, "planning_readiness", {}) or {}),
                "review_evidence_pack": dict(getattr(round_item, "review_evidence_pack", {}) or {}),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "input_fingerprint": _clean_text(result.input_fingerprint),
        "task_direction": _clean_text(result.task_direction),
        "business_outcome": _clean_text(result.business_outcome),
        "out_of_scope": list(result.out_of_scope),
        "source_of_truth": list(result.source_of_truth),
        "source_of_truth_canonical": list(result.source_of_truth_canonical),
        "path_type": _clean_text(result.path_type),
        "layers": list(result.layers),
        "coverage_hints": list(result.coverage_hints),
        "archetype": _clean_text(result.archetype),
        "capabilities": list(result.capabilities),
        "module_boundaries": list(result.module_boundaries),
        "verify_recipes": list(result.verify_recipes),
        "approval_points": list(result.approval_points),
        "execution_constraints": dict(result.execution_constraints),
        "confidence": _clean_text(result.confidence),
        "confidence_issues": list(result.confidence_issues),
        "status": _clean_text(result.status),
        "rounds": rounds_payload,
        "final_plan": {**dict(result.final_plan), "tasks": list(dict(result.final_plan).get("tasks") or [])},
        "final_review": dict(result.final_review or {}),
        "final_review_active_scope": dict(getattr(result, "final_review_active_scope", {}) or {}),
        "approval": dict(result.approval),
        "escalation": dict(result.escalation or {}),
        "context_scout": dict(result.context_scout or {}),
        "prompt_lesson_learning": dict(result.prompt_lesson_learning or {}),
        "planning_readiness": dict(getattr(result, "planning_readiness", {}) or {}),
        "meta_blocker_demotion_log": [
            dict(item) for item in list(getattr(result, "meta_blocker_demotion_log", []) or [])
        ],
    }

__all__ = [
    "_split_tasks_if_needed",
    "plan_to_task_cards",
    "plan_to_task_graph",
    "result_to_artifact",
]

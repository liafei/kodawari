"""Contract-first task card helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from kodawari.autopilot.core.task_modes import verification_only_allows_empty_files


SCHEMA_VERSION_V1 = "contract_first.task_card.v1"
SCHEMA_VERSION_V1_1 = "contract_first.task_card.v1.1"
# Backward-compatible export used by existing callers/tests.
SCHEMA_VERSION = SCHEMA_VERSION_V1
DEFAULT_FORBIDDEN = [
    "Do not refactor unrelated modules.",
    "Do not add compatibility shadow fields.",
    "Do not expand scope beyond files_to_change.",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _env_truthy(name: str, *, default: str = "0") -> bool:
    raw = _clean_text(os.environ.get(name, default)).lower()
    return raw in {"1", "true", "yes", "on"}


def _task_card_schema_version() -> str:
    if _env_truthy("WORKFLOW_TASK_CARD_V1_1"):
        return SCHEMA_VERSION_V1_1
    return SCHEMA_VERSION_V1


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _graph_tasks(task_graph: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = task_graph.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [item for item in tasks if isinstance(item, dict)]


def _find_task(task_graph: dict[str, Any], task_id: str) -> dict[str, Any]:
    target = _clean_text(task_id).upper()
    for task in _graph_tasks(task_graph):
        if _clean_text(task.get("task_id")).upper() == target:
            return task
    raise ValueError(f"Task id not found: {task_id}")


def _why_this_layer(task: dict[str, Any]) -> str:
    layer = _clean_text(task.get("layer_owner"), "service")
    return f"This task belongs to {layer} layer because it owns the primary behavior in this step."


def _test_plan(task: dict[str, Any]) -> str:
    proof = _clean_text(task.get("test_proof"))
    if proof:
        return proof
    layer = _clean_text(task.get("layer_owner"), "service")
    return f"Run {layer} unit tests and scoped integration checks for changed files."


def _clean_freshness(value: Any) -> dict[str, Any]:
    payload = dict(value or {}) if isinstance(value, dict) else {}
    cleaned: dict[str, Any] = {}
    commit = _clean_text(payload.get("scouted_at_commit"))
    if commit:
        cleaned["scouted_at_commit"] = commit
    source_hashes = _dict_list(payload.get("source_file_hashes"))
    if source_hashes:
        cleaned["source_file_hashes"] = source_hashes
    symbol_fingerprints = _dict_list(payload.get("target_symbol_fingerprints"))
    if symbol_fingerprints:
        cleaned["target_symbol_fingerprints"] = symbol_fingerprints
    return cleaned


_TEST_MUTATION_REQUIRED_STRING_FIELDS = (
    "file",
    "match_kind",
    "old_pattern",
    "new_pattern",
    "behavior_change_id",
)


def _filter_complete_test_mutations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop allowed_test_mutations entries that miss required string fields.

    Mimo occasionally emits entries with ``behavior_change_id: null`` or other
    sparse shapes. Schema validation rejects the whole task_card on the first
    bad entry, which blocks the artifact write even when the rest of the card
    is fine. Filtering at build time is safe: a malformed mutation can't have
    been useful to the executor anyway.
    """

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if any(not _clean_text(item.get(field)) for field in _TEST_MUTATION_REQUIRED_STRING_FIELDS):
            continue
        out.append(dict(item))
    return out


def _preserve_execution_guidance(card: dict[str, Any], task: dict[str, Any]) -> None:
    """Attach planner/task-graph guidance that executors need for a narrow diff."""
    list_fields = (
        "coverage_hints",
        "do_not_change",
        "read_only_files",
        "related_existing_tests",
        "review_focus",
    )
    dict_list_fields = (
        "target_symbols",
        "read_only_symbols",
        "behavior_changes",
        "allowed_test_mutations",
        "provides",
        "requires",
        "api_contracts",
    )
    for field in list_fields:
        values = _string_list(task.get(field))
        if values:
            card[field] = values
    for field in dict_list_fields:
        values = _dict_list(task.get(field))
        if field == "allowed_test_mutations":
            values = _filter_complete_test_mutations(values)
        if values:
            card[field] = values
    execution_constraints = task.get("execution_constraints")
    if isinstance(execution_constraints, dict) and execution_constraints:
        card["execution_constraints"] = dict(execution_constraints)
    freshness = _clean_freshness(task.get("freshness"))
    if freshness:
        card["freshness"] = freshness


def _preserve_v1_1_fields(card: dict[str, Any], task: dict[str, Any]) -> None:
    """Attach optional v1.1 contract fields emitted by planner/task-graph."""
    _preserve_execution_guidance(card, task)


def _ensure_task_executable(task: dict[str, Any], task_id: str) -> None:
    executability = dict(task.get("executability") or {})
    if str(executability.get("status") or "").upper() != "FAIL":
        return
    issues = [str(item) for item in list(executability.get("issues") or []) if str(item).strip()]
    message = "; ".join(issues) or f"Task {task_id} failed executability validation."
    raise ValueError(message)


def _task_forbidden_changes(task: dict[str, Any]) -> list[str]:
    task_forbidden = _string_list(task.get("forbidden_changes"))
    return list(dict.fromkeys([*DEFAULT_FORBIDDEN, *task_forbidden]))


def _base_task_card(
    *,
    task: dict[str, Any],
    schema_version: str,
    files_to_change: list[str],
    new_files: list[str],
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "schema_version": schema_version,
        "generated_at": _utc_now_iso(),
        "task_id": _clean_text(task.get("task_id")).upper(),
        "task_name": _clean_text(task.get("task_name")),
        "why_this_layer": _why_this_layer(task),
        "files_to_change": files_to_change,
        "new_files": new_files,
        "invariants": _string_list(task.get("invariants"))[:5],
        "test_plan": _test_plan(task),
        "forbidden_changes": _task_forbidden_changes(task),
    }
    surface = _clean_text(task.get("surface"))
    if surface:
        card["surface"] = surface
    return card


_EXISTING_FILES_TO_CHANGE_CAP = 3
_GREENFIELD_FILES_TO_CHANGE_CAP = 5


def _files_to_change_cap(planning_mode: str) -> int:
    """A4: greenfield bootstrap tasks need to declare a full vertical slice
    (schema+model+repo+service+test) in a single task — 3 is too narrow.
    Existing-mode keeps the legacy cap of 3 so refactors stay scoped."""
    if str(planning_mode or "").strip().lower() == "greenfield":
        return _GREENFIELD_FILES_TO_CHANGE_CAP
    return _EXISTING_FILES_TO_CHANGE_CAP


def build_task_card(task_graph: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = _find_task(task_graph, task_id)
    _ensure_task_executable(task, task_id)
    planning_mode = str(task_graph.get("planning_mode") or "existing")
    cap = _files_to_change_cap(planning_mode)
    files_to_change = _string_list(task.get("core_files"))[:cap]
    # Preserve planner-declared new_files and verify_cmd so executor / preflight
    # can consume them downstream. Prior to 2026-04-23 these were dropped here.
    # new_files must be a subset of files_to_change (enforced upstream in planning_agent).
    declared_new_files = _string_list(task.get("new_files"))
    new_files = [item for item in declared_new_files if item in files_to_change]
    verify_cmd = _clean_text(task.get("verify_cmd"))
    schema_version = _task_card_schema_version()
    card = _base_task_card(
        task=task,
        schema_version=schema_version,
        files_to_change=files_to_change,
        new_files=new_files,
    )
    if verify_cmd:
        card["verify_cmd"] = verify_cmd
    _preserve_execution_guidance(card, task)
    if schema_version == SCHEMA_VERSION_V1_1:
        _preserve_v1_1_fields(card, task)
    # `requires` is mandatory per task_card.schema (a planner that has no
    # declared preconditions must explicitly assert that with []). Make sure
    # the field is always present so readiness gating sees a stable shape.
    if "requires" not in card:
        card["requires"] = []
    return card


def validate_task_card(payload: dict[str, Any], *, planning_mode: str = "existing") -> list[str]:
    errors: list[str] = []
    required = ("task_id", "why_this_layer", "files_to_change", "invariants", "test_plan")
    for field in required:
        if field not in payload:
            errors.append(f"missing required field: {field}")
    files_to_change = _string_list(payload.get("files_to_change"))
    if not files_to_change and not verification_only_allows_empty_files(payload):
        errors.append("files_to_change must be non-empty")
    invariants = _string_list(payload.get("invariants"))
    if not invariants:
        errors.append("invariants must be non-empty")
    cap = _files_to_change_cap(planning_mode)
    if len(files_to_change) > cap:
        errors.append(f"files_to_change exceeds {cap} items (planning_mode={planning_mode})")
    return errors


def render_task_card_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Task Card",
        "",
        f"- task_id: {_clean_text(payload.get('task_id'))}",
        f"- task_name: {_clean_text(payload.get('task_name'))}",
        f"- generated_at: {_clean_text(payload.get('generated_at'))}",
        "",
        "## Why This Layer",
        f"- {_clean_text(payload.get('why_this_layer'))}",
        "",
        "## Files To Change",
    ]
    files_to_change = _string_list(payload.get("files_to_change"))
    if files_to_change:
        lines.extend(f"- {item}" for item in files_to_change)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Invariants"])
    invariants = _string_list(payload.get("invariants"))
    if invariants:
        lines.extend(f"- {item}" for item in invariants)
    else:
        lines.append("- (none)")
    lines.extend(
        [
            "",
            "## Test Plan",
            f"- {_clean_text(payload.get('test_plan'))}",
            "",
            "## Forbidden Changes",
        ]
    )
    forbidden = _string_list(payload.get("forbidden_changes"))
    if forbidden:
        lines.extend(f"- {item}" for item in forbidden)
    else:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"

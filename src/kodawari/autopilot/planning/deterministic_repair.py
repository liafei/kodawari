"""Deterministic repairs for machine-fixable planning defects."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import re
from typing import Any

from kodawari.autopilot.core.permission_policy import is_path_blocked_for_write
from kodawari.autopilot.core.task_modes import verification_only_allows_empty_files
from kodawari.autopilot.planning.planning_consistency import finding_task_ids
from kodawari.autopilot.planning.planning_validators import (
    normalize_planning_path,
    path_comparison_is_case_insensitive,
    planning_path_key,
)


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


def _plan_tasks(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in list(plan.get("tasks") or []) if isinstance(item, dict)]


def _task_ids(plan: dict[str, Any]) -> set[str]:
    return {_clean_text(task.get("task_id")) for task in _plan_tasks(plan) if _clean_text(task.get("task_id"))}


def _tasks_by_id(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _clean_text(task.get("task_id")): dict(task)
        for task in _plan_tasks(plan)
        if _clean_text(task.get("task_id"))
    }


_PATH_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_./\\-])((?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.py)")
_TEST_PATH_MENTION_RE = re.compile(r"tests[\\/][A-Za-z0-9_.-]+\.py", re.IGNORECASE)
_EDIT_VERBS = ("update", "modify", "edit", "change", "add")
_PASS_MARKERS = ("pass", "passing", "green", "unchanged")
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _parse_method_endpoint(contract: dict[str, Any]) -> tuple[str, str]:
    method = _clean_text(contract.get("method")).upper()
    endpoint = _clean_text(
        contract.get("endpoint")
        or contract.get("path")
        or contract.get("route")
        or contract.get("name")
    )
    if endpoint:
        parts = endpoint.split(maxsplit=1)
        if len(parts) == 2 and parts[0].upper() in _HTTP_METHODS:
            if not method:
                method = parts[0].upper()
            endpoint = parts[1].strip()
    return method, endpoint


def _response_shape(contract: dict[str, Any]) -> Any:
    for key in ("response_shape", "shape", "response", "shape_id"):
        if key in contract:
            return contract.get(key)
    return None


def _set_response_shape(contract: dict[str, Any], shape: Any) -> dict[str, Any]:
    updated = dict(contract)
    for key in ("response_shape", "shape", "response", "shape_id"):
        if key in updated:
            updated[key] = copy.deepcopy(shape)
            return updated
    updated["response_shape"] = copy.deepcopy(shape)
    return updated


def _canonical_shape_key(shape: Any) -> str:
    try:
        return json.dumps(shape, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return json.dumps(str(shape), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique_shapes(shapes: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for shape in shapes:
        key = _canonical_shape_key(shape)
        if key in seen:
            continue
        seen.add(key)
        unique.append(copy.deepcopy(shape))
    return unique


def _merged_response_shape(shapes: list[Any]) -> Any:
    unique = _unique_shapes(shapes)
    if len(unique) == 1:
        return unique[0]
    return {"variants": unique}


def _contract_kind(item: dict[str, Any]) -> str:
    return _clean_text(item.get("kind") or item.get("type")).lower()


def _log_entry(
    *,
    rule: str,
    location: str,
    before: Any,
    after: Any,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    entry = {
        "rule": rule,
        "location": location,
        "before": before,
        "after": after,
        "reason": reason,
    }
    entry.update(extra)
    return entry


def _zero_based_task_location(task_index: int, suffix: str = "") -> str:
    return f"tasks[{max(task_index - 1, 0)}]{suffix}"


def _truncate_invariants(plan: dict[str, Any]) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = []
    tasks = _plan_tasks(plan)
    for index, task in enumerate(tasks, start=1):
        invariants = _string_list(task.get("invariants"))
        if len(invariants) <= 5:
            continue
        truncated = invariants[:5]
        task["invariants"] = truncated
        log.append(
            _log_entry(
                rule="truncate_invariants",
                location=f"tasks[{index}].invariants",
                before=invariants,
                after=truncated,
                reason="Planner emitted more than 5 invariants; keep the leading 5 before structural validation.",
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _change_log_entries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return _dict_list(plan.get("change_log"))


def _change_log_targets(plan: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for entry in _change_log_entries(plan):
        target = _clean_text(entry.get("task_id") or entry.get("target"))
        if target:
            targets.add(target)
    return targets


def _synthetic_change_log_finding(task_id: str, reason: str) -> dict[str, Any]:
    return {
        "severity": "info",
        "category": "deterministic_repair",
        "task_id": task_id,
        "description": f"auto:no-finding-required change_log target {task_id}",
        "recommendation": reason,
    }


def _add_missing_task_change_log_entries(
    plan: dict[str, Any],
    previous_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if not previous_plan:
        return []
    previous_tasks = _tasks_by_id(previous_plan)
    current_tasks = _tasks_by_id(plan)
    if not previous_tasks and not current_tasks:
        return []
    entries = _change_log_entries(plan)
    declared = _change_log_targets(plan)
    log: list[dict[str, Any]] = []
    missing_changes = [
        (task_id, "removed") for task_id in sorted(set(previous_tasks) - set(current_tasks))
    ] + [
        (task_id, "added") for task_id in sorted(set(current_tasks) - set(previous_tasks))
    ]
    for task_id, action in missing_changes:
        if task_id in declared:
            continue
        entry = {
            "task_id": task_id,
            "fields": ["tasks"],
            "reason": f"{action.capitalize()} by deterministic repair to preserve round-to-round planning auditability.",
        }
        entries.append(entry)
        declared.add(task_id)
        log.append(
            _log_entry(
                rule="add_missing_task_change_log_entry",
                location="change_log",
                before=None,
                after=entry,
                reason=f"Planner revised task set but omitted required {action} task audit entry.",
                synthetic_finding=_synthetic_change_log_finding(
                    task_id,
                    "Allow machine-added change_log entry for an added/removed task caused by replanning.",
                ),
            )
        )
    if log:
        plan["change_log"] = entries
    return log


def _normalize_change_log_refs(
    plan: dict[str, Any],
    previous_plan: dict[str, Any] | None,
    previous_findings: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not previous_plan:
        return []
    known_task_ids = _task_ids(previous_plan) | _task_ids(plan)
    if not known_task_ids:
        return []
    allowed = finding_task_ids(previous_findings)
    log: list[dict[str, Any]] = []
    for index, entry in enumerate(_change_log_entries(plan), start=1):
        target = _clean_text(entry.get("task_id") or entry.get("target"))
        if not target or target in {"plan", "*"} or target not in known_task_ids:
            continue
        if target in allowed:
            continue
        synthetic_finding = _synthetic_change_log_finding(
            target,
            "Allow machine-accepted change_log target because it is a known task id.",
        )
        log.append(
            _log_entry(
                rule="change_log_known_task_ref",
                location=f"change_log[{index}]",
                before={"allowed_from_findings": sorted(allowed)},
                after={"synthetic_finding_task_id": target},
                reason="Known task change_log target can be accepted deterministically without another planner round.",
                synthetic_finding=synthetic_finding,
            )
        )
        allowed.add(target)
    return log


def _recipe_signature(recipe: dict[str, Any]) -> str:
    normalized = {
        "surface": _clean_text(recipe.get("surface")).lower(),
        "command": _clean_text(recipe.get("command")),
        "required": recipe.get("required") if isinstance(recipe.get("required"), bool) else None,
        "roots": _string_list(recipe.get("roots")),
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _root_exists(project_root: Path | None, raw: Any) -> bool:
    if project_root is None:
        return True
    normalized = normalize_planning_path(raw)
    if not normalized:
        return False
    root = project_root.resolve()
    try:
        candidate = (root / normalized).resolve()
        return candidate.is_relative_to(root) and candidate.exists()
    except (OSError, ValueError):
        return False


def _dedupe_verify_recipes(plan: dict[str, Any], project_root: Path | None) -> list[dict[str, Any]]:
    recipes = _dict_list(plan.get("verify_recipes"))
    if not recipes:
        return []
    log: list[dict[str, Any]] = []
    repaired: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_seen: set[str] = set()
    for index, recipe in enumerate(recipes, start=1):
        raw_signature = _recipe_signature(recipe)
        if raw_signature in raw_seen:
            log.append(
                _log_entry(
                    rule="dedupe_verify_recipes",
                    location=f"verify_recipes[{index}]",
                    before=recipe,
                    after=None,
                    reason="Duplicate verify recipe removed before structural review.",
                )
            )
            continue
        raw_seen.add(raw_signature)
        roots = _string_list(recipe.get("roots"))
        existing_roots = [root for root in roots if _root_exists(project_root, root)]
        if project_root is not None and roots != existing_roots:
            updated = dict(recipe)
            updated["roots"] = existing_roots
            log.append(
                _log_entry(
                    rule="filter_missing_verify_recipe_roots",
                    location=f"verify_recipes[{index}].roots",
                    before=roots,
                    after=existing_roots,
                    reason="Drop verify recipe roots that do not exist in the active workspace.",
                )
            )
            recipe = updated
        signature = _recipe_signature(recipe)
        if signature in seen:
            log.append(
                _log_entry(
                    rule="dedupe_verify_recipes",
                    location=f"verify_recipes[{index}]",
                    before=recipe,
                    after=None,
                    reason="Duplicate verify recipe removed after root normalization.",
                )
            )
            continue
        seen.add(signature)
        repaired.append(recipe)
    if log:
        plan["verify_recipes"] = repaired
    return log


def _infer_layer_owner(task: dict[str, Any], plan: dict[str, Any]) -> str:
    text = " ".join(
        [
            _clean_text(task.get("surface")),
            _clean_text(task.get("task_name")),
            " ".join(_string_list(task.get("files_to_change"))),
        ]
    ).replace("\\", "/").lower()
    if "route" in text or "/api/" in text or "/routes/" in text:
        return "route"
    if "service" in text or "/services/" in text:
        return "service"
    if "schema" in text or "migration" in text or "repository" in text:
        return "repository"
    if "test" in text or "/tests/" in text or text.startswith("tests/"):
        return "test"
    if "docs/" in text or text.endswith(".md"):
        return "docs"
    if "frontend" in text or "/src/" in text or ".jsx" in text or ".tsx" in text:
        return "frontend"
    for layer in _string_list(plan.get("layers")):
        if layer:
            return layer.lower()
    return "service"


def _fill_missing_layer_owner(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    log: list[dict[str, Any]] = []
    for index, task in enumerate(tasks, start=1):
        if _clean_text(task.get("layer_owner")):
            continue
        inferred = _infer_layer_owner(task, plan)
        task["layer_owner"] = inferred
        log.append(
            _log_entry(
                rule="infer_missing_layer_owner",
                location=f"tasks[{index}].layer_owner",
                before="",
                after=inferred,
                reason="Planner omitted layer_owner; infer the narrow owner from surface, task name, and files_to_change.",
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _remove_owned_files_from_read_only(plan: dict[str, Any], project_root: Path | None) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    case_insensitive = path_comparison_is_case_insensitive(project_root)
    log: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks, start=1):
        owned = {
            planning_path_key(normalize_planning_path(path), case_insensitive=case_insensitive)
            for path in _string_list(task.get("files_to_change"))
        }
        if not owned:
            continue
        for field in ("read_only_files", "do_not_change"):
            before = _string_list(task.get(field))
            if not before:
                continue
            after = [
                path
                for path in before
                if planning_path_key(normalize_planning_path(path), case_insensitive=case_insensitive) not in owned
            ]
            if before == after:
                continue
            task[field] = after
            log.append(
                _log_entry(
                    rule="remove_owned_files_from_read_only",
                    location=f"tasks[{task_index}].{field}",
                    before=before,
                    after=after,
                    reason="A task cannot both own a write path and mark the same path read-only/do-not-change; files_to_change ownership wins.",
                )
            )
    if log:
        plan["tasks"] = tasks
    return log


def _finding_text(finding: dict[str, Any]) -> str:
    return " ".join(
        _clean_text(finding.get(key))
        for key in ("source", "category", "description", "recommendation")
    )


def _is_high_signal_scope_conflict(finding: dict[str, Any]) -> bool:
    severity = _clean_text(finding.get("severity")).lower()
    if severity not in {"blocking", "critical", "high"}:
        return False
    provenance = " ".join(_clean_text(finding.get(key)).lower() for key in ("source", "category"))
    if not any(marker in provenance for marker in ("review", "reviewer", "structure", "scope", "consistency", "validator")):
        return False
    lowered = _finding_text(finding).lower()
    if "files_to_change" not in lowered:
        return False
    if not any(marker in lowered for marker in ("read_only", "read-only", "只读")):
        return False
    return any(marker in lowered for marker in ("add", "include", "加入", "移除", "remove"))


def _mentioned_python_paths(finding: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for match in _PATH_MENTION_RE.finditer(_finding_text(finding).replace("\\", "/")):
        normalized = normalize_planning_path(match.group(1))
        if normalized:
            paths.append(normalized)
    return list(dict.fromkeys(paths))


def _path_exists_and_writable(project_root: Path | None, path: str) -> bool:
    normalized = normalize_planning_path(path)
    if not normalized or is_path_blocked_for_write(normalized):
        return False
    if project_root is None:
        return True
    root = project_root.resolve()
    try:
        candidate = (root / normalized).resolve()
        return candidate.is_relative_to(root) and candidate.exists()
    except (OSError, ValueError):
        return False


def _task_forbids_path(task: dict[str, Any], path_key: str, *, case_insensitive: bool) -> bool:
    forbidden = _string_list(task.get("do_not_change"))
    for item in list(task.get("forbidden_changes") or []):
        if isinstance(item, str):
            forbidden.append(item)
        elif isinstance(item, dict):
            forbidden.extend(_string_list(item.get("paths")))
            target = _clean_text(item.get("path") or item.get("file"))
            if target:
                forbidden.append(target)
    return any(
        planning_path_key(normalize_planning_path(item), case_insensitive=case_insensitive) == path_key
        for item in forbidden
    )


def _append_change_log_entry_for_scope_promotion(
    plan: dict[str, Any],
    *,
    task_id: str,
    path: str,
) -> dict[str, Any]:
    entry = {
        "task_id": task_id,
        "fields": ["files_to_change", "read_only_files"],
        "reason": f"Promoted {path} from read_only_files to files_to_change after explicit reviewer scope-conflict finding.",
    }
    entries = _change_log_entries(plan)
    entries.append(entry)
    plan["change_log"] = entries
    return entry


def _append_change_log_entry_for_verification_only_demote(
    plan: dict[str, Any],
    *,
    task_id: str,
    paths: list[str],
) -> dict[str, Any]:
    entry = {
        "task_id": task_id,
        "fields": ["files_to_change", "new_files", "read_only_files", "related_existing_tests"],
        "reason": (
            "Demoted verification-only/no-op evidence paths from files_to_change "
            f"to read-only scope: {', '.join(paths)}."
        ),
    }
    entries = _change_log_entries(plan)
    entries.append(entry)
    plan["change_log"] = entries
    return entry


def _append_unique_plan_path_field(plan: dict[str, Any], field: str, paths: list[str]) -> None:
    existing = _string_list(plan.get(field))
    merged = list(existing)
    seen = {item.lower() for item in existing}
    for path in paths:
        normalized = normalize_planning_path(path)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(normalized)
    plan[field] = merged


def _ensure_verification_only_task_constraints(
    plan: dict[str, Any],
    previous_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    log: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks, start=1):
        if not verification_only_allows_empty_files(plan, task):
            continue
        before = copy.deepcopy(task.get("execution_constraints")) if isinstance(task.get("execution_constraints"), dict) else {}
        constraints = dict(before)
        changed = False
        for key in ("verification_only_noop", "executor_must_not_edit"):
            if constraints.get(key) is not True:
                constraints[key] = True
                changed = True
        if not changed:
            continue
        task["execution_constraints"] = constraints
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        entry = None
        if previous_plan:
            entry = {
                "task_id": task_id,
                "fields": ["execution_constraints"],
                "reason": "Promoted explicit verification-only/no-op booleans to the task execution_constraints.",
            }
            entries = _change_log_entries(plan)
            entries.append(entry)
            plan["change_log"] = entries
        log.append(
            _log_entry(
                rule="ensure_verification_only_task_constraints",
                location=_zero_based_task_location(task_index, ".execution_constraints"),
                before=before,
                after=copy.deepcopy(constraints),
                reason="Verification-only/no-op mode must be declared on the task card, not only plan-level metadata.",
                task_id=task_id,
                change_log_entry=entry,
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _demote_verification_only_write_anchors(
    plan: dict[str, Any],
    previous_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    log: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks, start=1):
        files_before = _string_list(task.get("files_to_change"))
        if not files_before or not verification_only_allows_empty_files(plan, task):
            continue
        new_files_before = _string_list(task.get("new_files"))
        normalized = [normalize_planning_path(path) for path in files_before if normalize_planning_path(path)]
        if not normalized:
            continue
        task["files_to_change"] = []
        task["new_files"] = []
        _append_unique_path_field(task, "read_only_files", normalized)
        _append_unique_path_field(task, "do_not_change", normalized)
        test_paths = [path for path in normalized if _is_test_path(path)]
        if test_paths:
            _append_unique_path_field(task, "related_existing_tests", test_paths)
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        change_log_entry = (
            _append_change_log_entry_for_verification_only_demote(
                plan,
                task_id=task_id,
                paths=normalized,
            )
            if previous_plan
            else None
        )
        log.append(
            _log_entry(
                rule="demote_verification_only_write_anchors",
                location=_zero_based_task_location(task_index, ".files_to_change"),
                before={"files_to_change": files_before, "new_files": new_files_before},
                after={
                    "files_to_change": [],
                    "new_files": [],
                    "read_only_files": _string_list(task.get("read_only_files")),
                    "related_existing_tests": _string_list(task.get("related_existing_tests")),
                },
                reason="Explicit verification-only/no-op task must not claim write-owned files; paths become read-only evidence.",
                task_id=task_id,
                change_log_entry=change_log_entry,
                synthetic_finding=_synthetic_change_log_finding(
                    task_id,
                    "Allow deterministic no-op scope demotion change_log entry.",
                ),
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


_SAFE_NO_EDIT_BOUNDARY = (
    "No edits to repository-tracked product source/test/docs/config files. "
    "Workflow-managed planning/scratch artifacts, pytest temp DB files, run outputs, "
    "and pre-existing workspace dirtiness are not product edits."
)
_SAFE_DIRTINESS_CHECK = (
    "Do not use raw VCS workspace dirtiness as a blocking no-op check. Rely on "
    "changed_files=[] plus scoped verify output; if a tracked-file comparison is "
    "available, compare only repository-tracked product source/test/docs/config "
    "files and ignore workflow scratch/tmp artifacts."
)
_CD_PREFIX_RE = re.compile(
    r"^\s*(?:cd|chdir|set-location)\s+(?P<path>(?:\"[^\"]+\"|'[^']+'|[^&;]+?))\s*(?:&&|;)\s*(?P<cmd>.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _contains_broad_no_edit_text(text: str) -> bool:
    lowered = _clean_text(text).lower()
    if not lowered:
        return False
    broad = (
        "any file",
        "any files",
        "every file",
        "workspace",
        "repository",
        "whole repo",
        "entire repo",
        "任何文件",
        "所有文件",
        "整个仓库",
    )
    edit = ("modify", "modification", "edit", "change", "write", "create", "修改", "改动", "写入", "新增")
    return any(item in lowered for item in broad) and any(item in lowered for item in edit)


def _contains_raw_git_dirtiness_check(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else _clean_text(value)
    lowered = text.lower()
    return "git status" in lowered or "git diff" in lowered or "dirtiness" in lowered


def _replace_broad_no_edit_list(values: Any) -> tuple[list[str], bool]:
    before = _string_list(values)
    if not before:
        return [], False
    after: list[str] = []
    changed = False
    for item in before:
        if _contains_broad_no_edit_text(item):
            changed = True
            if _SAFE_NO_EDIT_BOUNDARY not in after:
                after.append(_SAFE_NO_EDIT_BOUNDARY)
            continue
        after.append(item)
    return after, changed


def _strip_workspace_cd_prefix(command: Any, project_root: Path | None) -> tuple[str, bool]:
    text = _clean_text(command)
    if not text or project_root is None:
        return text, False
    match = _CD_PREFIX_RE.match(text)
    if not match:
        return text, False
    raw_path = _clean_text(match.group("path")).strip("'\"")
    if not raw_path:
        return text, False
    root = project_root.resolve()
    try:
        candidate = Path(raw_path).resolve() if Path(raw_path).is_absolute() else (root / raw_path).resolve()
    except OSError:
        return text, False
    if candidate != root:
        return text, False
    stripped = _clean_text(match.group("cmd"))
    return (stripped, True) if stripped else (text, False)


def _normalize_workspace_relative_verify_commands(
    plan: dict[str, Any],
    *,
    project_root: Path | None,
) -> list[dict[str, Any]]:
    if project_root is None:
        return []
    log: list[dict[str, Any]] = []
    recipes = _dict_list(plan.get("verify_recipes"))
    for index, recipe in enumerate(recipes):
        after, changed = _strip_workspace_cd_prefix(recipe.get("command"), project_root)
        if not changed:
            continue
        before = recipe.get("command")
        recipe["command"] = after
        log.append(
            _log_entry(
                rule="normalize_workspace_relative_verify_commands",
                location=f"verify_recipes[{index}].command",
                before=before,
                after=after,
                reason="Verify commands run from project_root; remove hardcoded cd/chained shell prefix.",
            )
        )
    if log:
        plan["verify_recipes"] = recipes
    tasks = _plan_tasks(plan)
    changed_tasks = False
    for task_index, task in enumerate(tasks, start=1):
        task_changes: dict[str, dict[str, str]] = {}
        for field in ("verify_cmd", "test_plan"):
            after, changed = _strip_workspace_cd_prefix(task.get(field), project_root)
            if changed:
                task_changes[field] = {"before": _clean_text(task.get(field)), "after": after}
                task[field] = after
        if not task_changes:
            continue
        changed_tasks = True
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        log.append(
            _log_entry(
                rule="normalize_workspace_relative_verify_commands",
                location=_zero_based_task_location(task_index),
                before={field: item["before"] for field, item in task_changes.items()},
                after={field: item["after"] for field, item in task_changes.items()},
                reason="Task verify commands run from project_root; remove hardcoded cd/chained shell prefix.",
                task_id=task_id,
            )
        )
    if changed_tasks:
        plan["tasks"] = tasks
    return log


def _test_paths_in_text(text: Any) -> set[str]:
    return {normalize_planning_path(match.group(0)) for match in _TEST_PATH_MENTION_RE.finditer(_clean_text(text))}


def _mentions_unrequested_smoke(text: Any, allowed_tests: set[str]) -> bool:
    raw = _clean_text(text)
    if not raw:
        return False
    lowered = raw.lower()
    smoke_markers = ("workspace smoke", "minimal api smoke", "test_t001", "test_t002", "workspace_smoke")
    if not any(marker in lowered for marker in smoke_markers):
        return False
    mentioned_tests = _test_paths_in_text(raw)
    return not mentioned_tests or not mentioned_tests.issubset(allowed_tests)


def _remove_verification_only_unrequested_smoke_gates(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    log: list[dict[str, Any]] = []
    allowed_tests = {
        path
        for task in tasks
        for path in _test_paths_in_text(task.get("verify_cmd"))
        if path
    }
    if not allowed_tests:
        return []
    approval_before = _dict_list(plan.get("approval_points"))
    approval_after = [
        item
        for item in approval_before
        if not _mentions_unrequested_smoke(json.dumps(item, ensure_ascii=False), allowed_tests)
    ]
    if approval_after != approval_before:
        plan["approval_points"] = approval_after
        log.append(
            _log_entry(
                rule="remove_verification_only_unrequested_smoke_gates",
                location="approval_points",
                before=approval_before,
                after=approval_after,
                reason="Verification-only closure must not add blocking smoke gates outside the requested verify_cmd test set.",
            )
        )
    recipes_before = _dict_list(plan.get("verify_recipes"))
    recipes_after = [
        item
        for item in recipes_before
        if not _mentions_unrequested_smoke(json.dumps(item, ensure_ascii=False), allowed_tests)
    ]
    if recipes_after != recipes_before:
        plan["verify_recipes"] = recipes_after
        log.append(
            _log_entry(
                rule="remove_verification_only_unrequested_smoke_gates",
                location="verify_recipes",
                before=recipes_before,
                after=recipes_after,
                reason="Verification-only closure must not retain required smoke recipes outside the requested verify_cmd test set.",
            )
        )
    changed_tasks = False
    for task_index, task in enumerate(tasks, start=1):
        if not verification_only_allows_empty_files(plan, task):
            continue
        before = {
            "invariants": _string_list(task.get("invariants")),
            "coverage_hints": _string_list(task.get("coverage_hints")),
            "test_plan": _clean_text(task.get("test_plan")),
        }
        task["invariants"] = [
            item for item in before["invariants"] if not _mentions_unrequested_smoke(item, allowed_tests)
        ][:5]
        task["coverage_hints"] = [
            item for item in before["coverage_hints"] if not _mentions_unrequested_smoke(item, allowed_tests)
        ]
        if _mentions_unrequested_smoke(before["test_plan"], allowed_tests):
            task["test_plan"] = f"Run verify_cmd and report exit code plus pytest output summary: {_clean_text(task.get('verify_cmd'))}"
        after = {
            "invariants": _string_list(task.get("invariants")),
            "coverage_hints": _string_list(task.get("coverage_hints")),
            "test_plan": _clean_text(task.get("test_plan")),
        }
        if before == after:
            continue
        changed_tasks = True
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        log.append(
            _log_entry(
                rule="remove_verification_only_unrequested_smoke_gates",
                location=_zero_based_task_location(task_index),
                before=before,
                after=after,
                reason="Removed unrequested smoke-test requirements from verification-only/no-op task scope.",
                task_id=task_id,
            )
        )
    if changed_tasks:
        plan["tasks"] = tasks
    return log


def _normalize_verification_only_no_edit_contracts(
    plan: dict[str, Any],
    previous_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    log: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks, start=1):
        if not verification_only_allows_empty_files(plan, task):
            continue
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        before = {
            "execution_constraints": copy.deepcopy(task.get("execution_constraints")),
            "invariants": _string_list(task.get("invariants")),
            "forbidden_changes": _string_list(task.get("forbidden_changes")),
        }
        changed_fields: list[str] = []
        constraints = dict(task.get("execution_constraints") or {})
        if _contains_raw_git_dirtiness_check(constraints) or not _clean_text(constraints.get("no_edit_boundary")):
            constraints["no_edit_boundary"] = _SAFE_NO_EDIT_BOUNDARY
            constraints["dirtiness_check"] = _SAFE_DIRTINESS_CHECK
            changed_fields.append("execution_constraints")
        invariants_after, invariants_changed = _replace_broad_no_edit_list(task.get("invariants"))
        if invariants_changed:
            task["invariants"] = invariants_after[:5]
            changed_fields.append("invariants")
        forbidden_after, forbidden_changed = _replace_broad_no_edit_list(task.get("forbidden_changes"))
        if forbidden_changed:
            task["forbidden_changes"] = forbidden_after
            changed_fields.append("forbidden_changes")
        if not changed_fields:
            continue
        task["execution_constraints"] = constraints
        entry = None
        if previous_plan:
            entry = {
                "task_id": task_id,
                "fields": sorted(set(changed_fields)),
                "reason": "Normalized verification-only no-edit boundary so workflow scratch/tmp artifacts do not create false blockers.",
            }
            entries = _change_log_entries(plan)
            entries.append(entry)
            plan["change_log"] = entries
        after = {
            "execution_constraints": copy.deepcopy(task.get("execution_constraints")),
            "invariants": _string_list(task.get("invariants")),
            "forbidden_changes": _string_list(task.get("forbidden_changes")),
        }
        log.append(
            _log_entry(
                rule="normalize_verification_only_no_edit_contracts",
                location=_zero_based_task_location(task_index),
                before=before,
                after=after,
                reason="Verification-only tasks must not use broad workspace dirtiness as a blocking no-op check.",
                task_id=task_id,
                change_log_entry=entry,
                synthetic_finding=_synthetic_change_log_finding(
                    task_id,
                    "Allow deterministic no-edit-boundary change_log entry.",
                ),
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _task_direction_mentions_frontend_scope(task_direction: str) -> bool:
    lowered = _clean_text(task_direction).lower()
    markers = ("frontend", "front-end", "mobile", "ui", "page", "页面", "界面", "前端")
    return any(marker in lowered for marker in markers)


def _add_verification_only_frontend_read_only_scope(
    plan: dict[str, Any],
    *,
    task_direction: str,
    project_root: Path | None,
) -> list[dict[str, Any]]:
    if not _task_direction_mentions_frontend_scope(task_direction):
        return []
    frontend_path = "mobile/www/index.html"
    if project_root is not None and not (project_root / frontend_path).exists():
        return []
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    verification_tasks = [
        (task_index, task)
        for task_index, task in enumerate(tasks, start=1)
        if verification_only_allows_empty_files(plan, task)
    ]
    if not verification_tasks:
        return []
    source_before = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
    }
    _append_unique_plan_path_field(plan, "source_of_truth", [frontend_path])
    source_after = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
    }
    log: list[dict[str, Any]] = []
    for task_index, task in verification_tasks:
        before = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
            **source_before,
        }
        _append_unique_path_field(task, "read_only_files", [frontend_path])
        _append_unique_path_field(task, "do_not_change", [frontend_path])
        after = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
            **source_after,
        }
        if before == after:
            continue
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        log.append(
            _log_entry(
                rule="add_verification_only_frontend_read_only_scope",
                location=_zero_based_task_location(task_index, ".read_only_files"),
                before=before,
                after=after,
                reason="Task direction includes page/frontend scope; preserve the live frontend file as read-only evidence for no-op closure.",
                task_id=task_id,
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


_VERIFICATION_ONLY_TRUTH_DOCS = (
    "docs/任务计划_v1.1.md",
    "docs/启动交付与运行手册.md",
    "docs/开发交付现状.md",
    "docs/prd_coverage_matrix.md",
)


def _existing_verification_truth_docs(project_root: Path | None) -> list[str]:
    if project_root is None:
        return []
    return [path for path in _VERIFICATION_ONLY_TRUTH_DOCS if (project_root / path).exists()]


def _add_verification_only_truth_docs_read_only_scope(
    plan: dict[str, Any],
    *,
    project_root: Path | None,
) -> list[dict[str, Any]]:
    docs = _existing_verification_truth_docs(project_root)
    if not docs:
        return []
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    verification_tasks = [
        (task_index, task)
        for task_index, task in enumerate(tasks, start=1)
        if verification_only_allows_empty_files(plan, task)
    ]
    if not verification_tasks:
        return []
    source_before = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
    }
    _append_unique_plan_path_field(plan, "source_of_truth", docs)
    _append_unique_plan_path_field(plan, "source_of_truth_canonical", docs)
    source_after = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
    }
    log: list[dict[str, Any]] = []
    for task_index, task in verification_tasks:
        before = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
            **source_before,
        }
        _append_unique_path_field(task, "read_only_files", docs)
        _append_unique_path_field(task, "do_not_change", docs)
        after = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
            **source_after,
        }
        if before == after:
            continue
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        log.append(
            _log_entry(
                rule="add_verification_only_truth_docs_read_only_scope",
                location=_zero_based_task_location(task_index, ".read_only_files"),
                before=before,
                after=after,
                reason="Verification-only closure must preserve project planning/runbook docs as read-only evidence.",
                task_id=task_id,
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _task_direction_mentions_read_later(task_direction: str) -> bool:
    text = _clean_text(task_direction).lower()
    return "read_later" in text or "read later" in text or "稍后阅读" in text


def _add_verification_only_read_later_persistence_scope(
    plan: dict[str, Any],
    *,
    task_direction: str,
    project_root: Path | None,
) -> list[dict[str, Any]]:
    if not _task_direction_mentions_read_later(task_direction):
        return []
    persistence_path = "backend/api/v1/services/edition_assembly.py"
    if project_root is not None and not (project_root / persistence_path).exists():
        return []
    tasks = _plan_tasks(plan)
    verification_tasks = [
        (task_index, task)
        for task_index, task in enumerate(tasks, start=1)
        if verification_only_allows_empty_files(plan, task)
    ]
    if not verification_tasks:
        return []
    source_before = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
    }
    _append_unique_plan_path_field(plan, "source_of_truth", [persistence_path])
    boundaries_before = _dict_list(plan.get("module_boundaries"))
    boundaries_after = copy.deepcopy(boundaries_before)
    for boundary in boundaries_after:
        name_surface = f"{_clean_text(boundary.get('name'))} {_clean_text(boundary.get('surface'))}".lower()
        if "read_later" not in name_surface and "read later" not in name_surface:
            continue
        _append_unique_path_field(boundary, "roots", [persistence_path])
        layers = _string_list(boundary.get("layers"))
        if "service" not in {item.lower() for item in layers}:
            boundary["layers"] = [*layers, "service"]
    if boundaries_after != boundaries_before:
        plan["module_boundaries"] = boundaries_after
    source_after = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
    }
    log: list[dict[str, Any]] = []
    for task_index, task in verification_tasks:
        before = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
            "module_boundaries": boundaries_before,
            **source_before,
        }
        _append_unique_path_field(task, "read_only_files", [persistence_path])
        _append_unique_path_field(task, "do_not_change", [persistence_path])
        after = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
            "module_boundaries": _dict_list(plan.get("module_boundaries")),
            **source_after,
        }
        if before == after:
            continue
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        log.append(
            _log_entry(
                rule="add_verification_only_read_later_persistence_scope",
                location=_zero_based_task_location(task_index, ".read_only_files"),
                before=before,
                after=after,
                reason="Read-later persistence is implemented through daily_read_state in edition_assembly.py; keep it as read-only schema evidence.",
                task_id=task_id,
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _is_canonical_truth_path(path: str) -> bool:
    normalized = normalize_planning_path(path).lower()
    return normalized.startswith("docs/") or normalized.startswith("tests/")


def _demote_verification_only_implementation_canonical_truth(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if not any(verification_only_allows_empty_files(plan, task) for task in _plan_tasks(plan)):
        return []
    canonical_before = _string_list(plan.get("source_of_truth_canonical"))
    source_before = _string_list(plan.get("source_of_truth"))
    canonical_after = [path for path in canonical_before if _is_canonical_truth_path(path)]
    source_after = [path for path in source_before if _is_canonical_truth_path(path)]
    if canonical_before == canonical_after and source_before == source_after:
        return []
    plan["source_of_truth"] = source_after
    plan["source_of_truth_canonical"] = canonical_after
    return [
        _log_entry(
            rule="demote_verification_only_implementation_canonical_truth",
            location="source_of_truth",
            before={"source_of_truth": source_before, "source_of_truth_canonical": canonical_before},
            after={"source_of_truth": source_after, "source_of_truth_canonical": canonical_after},
            reason="Verification-only truth should remain docs/tests; implementation files stay as supporting read-only evidence.",
        )
    ]


def _promote_verification_only_tests_to_truth(plan: dict[str, Any], project_root: Path | None) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    test_paths: list[str] = []
    seen: set[str] = set()
    for task in tasks:
        if not verification_only_allows_empty_files(plan, task):
            continue
        for raw in [*_string_list(task.get("related_existing_tests")), *_test_paths_in_text(task.get("verify_cmd"))]:
            normalized = normalize_planning_path(raw)
            if not normalized or normalized.lower() in seen:
                continue
            if project_root is not None and not (project_root / normalized).exists():
                continue
            seen.add(normalized.lower())
            test_paths.append(normalized)
    if not test_paths:
        return []
    before = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
        "tasks_read_only": [_string_list(task.get("read_only_files")) for task in tasks],
    }
    _append_unique_plan_path_field(plan, "source_of_truth", test_paths)
    _append_unique_plan_path_field(plan, "source_of_truth_canonical", test_paths)
    changed_tasks = False
    for task in tasks:
        if not verification_only_allows_empty_files(plan, task):
            continue
        read_only_before = _string_list(task.get("read_only_files"))
        do_not_change_before = _string_list(task.get("do_not_change"))
        _append_unique_path_field(task, "read_only_files", test_paths)
        _append_unique_path_field(task, "do_not_change", test_paths)
        changed_tasks = changed_tasks or read_only_before != _string_list(task.get("read_only_files"))
        changed_tasks = changed_tasks or do_not_change_before != _string_list(task.get("do_not_change"))
    if changed_tasks:
        plan["tasks"] = tasks
    after = {
        "source_of_truth": _string_list(plan.get("source_of_truth")),
        "source_of_truth_canonical": _string_list(plan.get("source_of_truth_canonical")),
        "tasks_read_only": [_string_list(task.get("read_only_files")) for task in tasks],
    }
    if before == after:
        return []
    return [
        _log_entry(
            rule="promote_verification_only_tests_to_truth",
            location="source_of_truth_canonical",
            before=before,
            after=after,
            reason="Verification-only canonical truth includes the exact tests that define acceptance.",
        )
    ]


def _verification_only_source_docs(plan: dict[str, Any], project_root: Path | None) -> list[str]:
    docs: list[str] = []
    seen: set[str] = set()
    for raw in [*_string_list(plan.get("source_of_truth")), *_string_list(plan.get("source_of_truth_canonical"))]:
        normalized = normalize_planning_path(raw)
        if not normalized.lower().startswith("docs/") or normalized.lower() in seen:
            continue
        if project_root is not None and not (project_root / normalized).exists():
            continue
        seen.add(normalized.lower())
        docs.append(normalized)
    return docs


def _sync_verification_only_source_docs_read_only_scope(
    plan: dict[str, Any],
    *,
    project_root: Path | None,
) -> list[dict[str, Any]]:
    docs = _verification_only_source_docs(plan, project_root)
    if not docs:
        return []
    tasks = _plan_tasks(plan)
    log: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks, start=1):
        if not verification_only_allows_empty_files(plan, task):
            continue
        before = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
        }
        _append_unique_path_field(task, "read_only_files", docs)
        _append_unique_path_field(task, "do_not_change", docs)
        after = {
            "read_only_files": _string_list(task.get("read_only_files")),
            "do_not_change": _string_list(task.get("do_not_change")),
        }
        if before == after:
            continue
        task_id = _clean_text(task.get("task_id")) or _zero_based_task_location(task_index)
        log.append(
            _log_entry(
                rule="sync_verification_only_source_docs_read_only_scope",
                location=_zero_based_task_location(task_index, ".read_only_files"),
                before=before,
                after=after,
                reason="Verification-only tasks must keep docs listed as source-of-truth inside the read-only evidence boundary.",
                task_id=task_id,
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _task_direction_requires_work_all_gate(task_direction: str) -> bool:
    text = _clean_text(task_direction).lower()
    return "work-all" in text or "work all" in text or "workflow work-all" in text


def _ensure_verification_only_work_all_approval(
    plan: dict[str, Any],
    *,
    task_direction: str,
) -> list[dict[str, Any]]:
    if not _task_direction_requires_work_all_gate(task_direction):
        return []
    if not any(verification_only_allows_empty_files(plan, task) for task in _plan_tasks(plan)):
        return []
    approval_points = _dict_list(plan.get("approval_points"))
    existing_text = json.dumps(approval_points, ensure_ascii=False).lower()
    changed = False
    point = {
        "name": "workflow work-all PASS",
        "required": True,
        "reason": "The verification-only closure is complete only when workflow work-all finishes PASS after the scoped pytest command succeeds.",
    }
    if "work-all" not in existing_text and "work all" not in existing_text:
        plan["approval_points"] = [*approval_points, point]
        changed = True
    tasks = _plan_tasks(plan)
    for task in tasks:
        if not verification_only_allows_empty_files(plan, task):
            continue
        invariants = _string_list(task.get("invariants"))
        invariant_text = "workflow work-all PASS is required for closure"
        if invariant_text.lower() not in {item.lower() for item in invariants}:
            task["invariants"] = [*invariants[:4], invariant_text] if len(invariants) >= 5 else [*invariants, invariant_text]
            changed = True
    if changed:
        plan["tasks"] = tasks
    if not changed:
        return []
    return [
        _log_entry(
            rule="ensure_verification_only_work_all_approval",
            location="approval_points",
            before=approval_points,
            after={"approval_points": plan.get("approval_points"), "tasks": tasks},
            reason="Task direction requires workflow work-all PASS as an explicit acceptance gate.",
        )
    ]


def _ensure_verification_only_report_approval(
    plan: dict[str, Any],
    *,
    task_direction: str,
) -> list[dict[str, Any]]:
    text = _clean_text(task_direction).lower()
    if not ("pytest" in text and ("exit" in text or "退出码" in text) and ("report" in text or "报告" in text)):
        return []
    if not any(verification_only_allows_empty_files(plan, task) for task in _plan_tasks(plan)):
        return []
    approval_points = _dict_list(plan.get("approval_points"))
    existing_text = json.dumps(approval_points, ensure_ascii=False).lower()
    if "exit code" in existing_text and "pytest" in existing_text and ("report" in existing_text or "summary" in existing_text):
        return []
    point = {
        "name": "final report includes command exit code pytest summary",
        "required": True,
        "reason": "The final report must include the real command executed, exit code, and pytest output summary.",
    }
    plan["approval_points"] = [*approval_points, point]
    return [
        _log_entry(
            rule="ensure_verification_only_report_approval",
            location="approval_points",
            before=approval_points,
            after=plan["approval_points"],
            reason="Task direction requires a structured final-report evidence contract.",
        )
    ]


def _promote_review_requested_write_paths(
    plan: dict[str, Any],
    previous_findings: list[dict[str, Any]] | None,
    project_root: Path | None,
) -> list[dict[str, Any]]:
    findings = [dict(item) for item in list(previous_findings or []) if isinstance(item, dict)]
    if not findings:
        return []
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    case_insensitive = path_comparison_is_case_insensitive(project_root)
    log: list[dict[str, Any]] = []
    for finding in findings:
        if not _is_high_signal_scope_conflict(finding):
            continue
        for path in _mentioned_python_paths(finding):
            if not _path_exists_and_writable(project_root, path):
                continue
            path_key = planning_path_key(path, case_insensitive=case_insensitive)
            matches: list[tuple[int, dict[str, Any]]] = []
            for index, task in enumerate(tasks):
                read_only_keys = {
                    planning_path_key(normalize_planning_path(item), case_insensitive=case_insensitive)
                    for item in _string_list(task.get("read_only_files"))
                }
                if path_key in read_only_keys:
                    matches.append((index, task))
            if len(matches) != 1:
                continue
            task_index, task = matches[0]
            if _task_forbids_path(task, path_key, case_insensitive=case_insensitive):
                continue
            files_to_change = _string_list(task.get("files_to_change"))
            file_keys = {
                planning_path_key(normalize_planning_path(item), case_insensitive=case_insensitive)
                for item in files_to_change
            }
            if path_key in file_keys or len(files_to_change) >= 3:
                continue
            read_only_before = _string_list(task.get("read_only_files"))
            read_only_after = [
                item
                for item in read_only_before
                if planning_path_key(normalize_planning_path(item), case_insensitive=case_insensitive) != path_key
            ]
            files_after = [*files_to_change, path]
            task["files_to_change"] = files_after
            task["read_only_files"] = read_only_after
            task_id = _clean_text(task.get("task_id")) or f"tasks[{task_index + 1}]"
            change_log_entry = _append_change_log_entry_for_scope_promotion(
                plan,
                task_id=task_id,
                path=path,
            )
            log.append(
                _log_entry(
                    rule="promote_review_requested_write_path",
                    location=f"tasks[{task_index + 1}].files_to_change",
                    before={
                        "files_to_change": files_to_change,
                        "read_only_files": read_only_before,
                    },
                    after={
                        "files_to_change": files_after,
                        "read_only_files": read_only_after,
                    },
                    reason="Reviewer explicitly identified a files_to_change/read_only_files scope conflict for a single existing file.",
                    path=path,
                    task_id=task_id,
                    change_log_entry=change_log_entry,
                    synthetic_finding=_synthetic_change_log_finding(
                        task_id,
                        "Allow deterministic scope-conflict change_log entry requested by reviewer.",
                    ),
                )
            )
    if log:
        plan["tasks"] = tasks
    return log


def _verify_only_paths_from_task_direction(task_direction: str) -> list[str]:
    text = _clean_text(task_direction).replace("\\", "/")
    if not text:
        return []
    lowered = text.lower()
    paths: list[str] = []
    for match in _PATH_MENTION_RE.finditer(text):
        before = lowered[max(0, match.start() - 80): match.start()]
        after = lowered[match.end(): match.end() + 100]
        keep_index = before.rfind("keep")
        if keep_index < 0:
            continue
        if any(before.rfind(verb) > keep_index for verb in _EDIT_VERBS):
            continue
        if not any(marker in after for marker in _PASS_MARKERS):
            continue
        normalized = normalize_planning_path(match.group(1))
        if normalized:
            paths.append(normalized)
    return list(dict.fromkeys(paths))


def _append_unique_path_field(task: dict[str, Any], field: str, paths: list[str]) -> None:
    existing = _string_list(task.get(field))
    merged = list(existing)
    seen = {item.lower() for item in existing}
    for path in paths:
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(path)
    task[field] = merged


def _path_aliases(path: str) -> list[str]:
    normalized = normalize_planning_path(path)
    name = Path(normalized).name
    stem = Path(normalized).stem
    aliases = [normalized, name, stem]
    match = re.match(r"(test_t\d+)", stem.lower())
    if match:
        aliases.append(match.group(1))
    return [item.lower() for item in aliases if item]


def _mentions_any_path_alias(text: str, paths: list[str]) -> bool:
    lowered = text.lower().replace("\\", "/")
    return any(alias in lowered for path in paths for alias in _path_aliases(path))


def _has_edit_verb(text: str) -> bool:
    lowered = text.lower()
    return any(verb in lowered for verb in _EDIT_VERBS)


def _strip_verify_only_edit_sentences(text: str, protected_paths: list[str]) -> str:
    original = _clean_text(text)
    if not original or not _mentions_any_path_alias(original, protected_paths):
        return original
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", original) if part.strip()]
    kept = [
        part
        for part in parts
        if not (_mentions_any_path_alias(part, protected_paths) and _has_edit_verb(part))
    ]
    return " ".join(kept).strip() or original


def _strip_verify_only_edit_list_items(values: Any, protected_paths: list[str]) -> list[str]:
    items = _string_list(values)
    if not items:
        return []
    kept = [
        item
        for item in items
        if not (_mentions_any_path_alias(item, protected_paths) and _has_edit_verb(item))
    ]
    return kept or items


def _is_test_path(path: str) -> bool:
    normalized = normalize_planning_path(path).lower()
    name = Path(normalized).name.lower()
    return normalized.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")


def _plan_requires_bundled_tests(plan: dict[str, Any]) -> bool:
    constraints = plan.get("execution_constraints") if isinstance(plan.get("execution_constraints"), dict) else {}
    return bool(
        constraints.get("bundle_implementation_and_tests")
        or constraints.get("require_test_in_task")
        or constraints.get("must_bundle_source_and_test")
    )


def _task_declares_test_edit(task: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(task.get("task_name") or ""),
            str(task.get("approach") or ""),
            str(task.get("test_plan") or ""),
            " ".join(_string_list(task.get("coverage_hints"))),
        ]
    ).lower()
    if not text:
        return False
    test_markers = (
        "add test",
        "add unit test",
        "add integration test",
        "new test",
        "test case",
        "edge-case test",
        "coverage",
    )
    return any(marker in text for marker in test_markers) and _has_edit_verb(text)


def _should_keep_protected_test_editable(plan: dict[str, Any], task: dict[str, Any], path: str) -> bool:
    return _is_test_path(path) and _plan_requires_bundled_tests(plan) and _task_declares_test_edit(task)


def _protect_verify_only_task_direction_paths(
    plan: dict[str, Any],
    *,
    task_direction: str,
    project_root: Path | None,
) -> list[dict[str, Any]]:
    protected = _verify_only_paths_from_task_direction(task_direction)
    if not protected:
        return []
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    case_insensitive = path_comparison_is_case_insensitive(project_root)
    protected_by_key = {
        planning_path_key(path, case_insensitive=case_insensitive): path
        for path in protected
    }
    log: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks, start=1):
        before = _string_list(task.get("files_to_change"))
        if not before:
            continue
        demoted: list[str] = []
        after: list[str] = []
        for raw_path in before:
            normalized_raw = normalize_planning_path(raw_path)
            key = planning_path_key(normalized_raw, case_insensitive=case_insensitive)
            if key in protected_by_key:
                protected_path = protected_by_key[key]
                if _should_keep_protected_test_editable(plan, task, protected_path):
                    after.append(raw_path)
                else:
                    demoted.append(protected_path)
            else:
                after.append(raw_path)
        if not demoted or not after:
            continue
        task["files_to_change"] = after
        _append_unique_path_field(task, "related_existing_tests", demoted)
        _append_unique_path_field(task, "read_only_files", demoted)
        for field in ("approach", "test_plan"):
            value = task.get(field)
            if isinstance(value, str):
                task[field] = _strip_verify_only_edit_sentences(value, demoted)
        for field in ("coverage_hints",):
            value = task.get(field)
            if isinstance(value, list):
                task[field] = _strip_verify_only_edit_list_items(value, demoted)
        log.append(
            _log_entry(
                rule="protect_verify_only_task_direction_paths",
                location=f"tasks[{task_index}].files_to_change",
                before=before,
                after=after,
                reason="Paths mentioned as 'keep ... passing' in the task direction are verify-only unless explicitly requested as editable.",
                demoted_to_related_existing_tests=list(demoted),
            )
        )
    if log:
        plan["tasks"] = tasks
    return log


def _api_response_items(items: list[dict[str, Any]]) -> dict[tuple[str, str], list[int]]:
    by_endpoint: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(items):
        if _contract_kind(item) not in {"api", "api_response", "api_contract", "endpoint"}:
            continue
        method, endpoint = _parse_method_endpoint(item)
        if method and endpoint and _response_shape(item) is not None:
            by_endpoint.setdefault((method, endpoint), []).append(index)
    return by_endpoint


def _contract_items_by_endpoint(items: list[dict[str, Any]]) -> dict[tuple[str, str], list[int]]:
    by_endpoint: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(items):
        method, endpoint = _parse_method_endpoint(item)
        if method and endpoint and _response_shape(item) is not None:
            by_endpoint.setdefault((method, endpoint), []).append(index)
    return by_endpoint


def _normalize_conflicting_api_contracts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if not tasks:
        return []
    log: list[dict[str, Any]] = []
    for task_index, task in enumerate(tasks, start=1):
        api_contracts = _dict_list(task.get("api_contracts"))
        provides = _dict_list(task.get("provides"))
        api_by_endpoint = _contract_items_by_endpoint(api_contracts)
        provides_by_endpoint = _api_response_items(provides)
        keys = sorted(
            set(api_by_endpoint) | set(provides_by_endpoint),
            key=lambda item: max(api_by_endpoint.get(item, [-1])),
            reverse=True,
        )
        changed = False
        for key in keys:
            api_indexes = api_by_endpoint.get(key, [])
            provide_indexes = provides_by_endpoint.get(key, [])
            all_shapes = [
                _response_shape(api_contracts[index])
                for index in api_indexes
            ] + [
                _response_shape(provides[index])
                for index in provide_indexes
            ]
            unique_shapes = _unique_shapes(all_shapes)
            if len(unique_shapes) <= 1 and len(api_indexes) <= 1:
                continue
            merged_shape = _merged_response_shape(all_shapes)
            before = {
                "api_contracts": [api_contracts[index] for index in api_indexes],
                "provides": [provides[index] for index in provide_indexes],
            }
            if api_indexes:
                keep_index = api_indexes[0]
                api_contracts[keep_index] = _set_response_shape(api_contracts[keep_index], merged_shape)
                for drop_index in sorted(api_indexes[1:], reverse=True):
                    del api_contracts[drop_index]
            elif provide_indexes:
                method, endpoint = key
                api_contracts.append(
                    {
                        "method": method,
                        "endpoint": endpoint,
                        "response_shape": copy.deepcopy(merged_shape),
                    }
                )
            for provide_index in provide_indexes:
                if provide_index < len(provides):
                    provides[provide_index] = _set_response_shape(provides[provide_index], merged_shape)
            changed = True
            method, endpoint = key
            log.append(
                _log_entry(
                    rule="normalize_api_contract_response_shape",
                    location=f"tasks[{task_index}].api_contracts[{method} {endpoint}]",
                    before=before,
                    after={"method": method, "endpoint": endpoint, "response_shape": merged_shape},
                    reason="Collapse repeated method+endpoint declarations into one validator-compatible response_shape.",
                )
            )
        if changed:
            task["api_contracts"] = api_contracts
            task["provides"] = provides
    if log:
        plan["tasks"] = tasks
    return log


def _deps_by_id(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    task_ids = {_clean_text(task.get("task_id")) for task in tasks if _clean_text(task.get("task_id"))}
    deps: dict[str, set[str]] = {}
    for task in tasks:
        task_id = _clean_text(task.get("task_id"))
        if not task_id:
            continue
        deps[task_id] = {dep for dep in _string_list(task.get("depends_on")) if dep in task_ids}
    return deps


def _task_dependency_closure(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    deps = _deps_by_id(tasks)
    closure: dict[str, set[str]] = {task_id: set() for task_id in deps}
    for task_id in deps:
        stack = list(deps.get(task_id, set()))
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            closure[task_id].add(current)
            stack.extend(deps.get(current, set()))
    return closure


def _files_by_key(task: dict[str, Any], *, case_insensitive: bool) -> dict[str, str]:
    return {
        planning_path_key(normalize_planning_path(path), case_insensitive=case_insensitive): normalize_planning_path(path)
        for path in list(task.get("files_to_change") or [])
        if _clean_text(path)
    }


def _serialize_parallel_file_conflicts(
    plan: dict[str, Any],
    project_root: Path | None,
) -> list[dict[str, Any]]:
    tasks = _plan_tasks(plan)
    if len(tasks) < 2:
        return []
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
            current_files = _files_by_key(current, case_insensitive=case_insensitive)
            for previous in tasks[:index]:
                previous_id = _clean_text(previous.get("task_id"))
                if not previous_id:
                    continue
                if previous_id in closure.get(current_id, set()) or current_id in closure.get(previous_id, set()):
                    continue
                previous_files = _files_by_key(previous, case_insensitive=case_insensitive)
                shared = sorted(set(current_files) & set(previous_files))
                if not shared:
                    continue
                before = _string_list(current.get("depends_on"))
                if previous_id not in before:
                    after = [*before, previous_id]
                    current["depends_on"] = after
                    log.append(
                        _log_entry(
                            rule="serialize_parallel_file_conflicts",
                            location=f"tasks[{index + 1}].depends_on",
                            before=before,
                            after=after,
                            reason="Tasks that write the same file must run serially before review.",
                            task_id=current_id,
                            depends_on_added=previous_id,
                            shared_files=[current_files[key] for key in shared],
                        )
                    )
                    changed = True
                    break
            if changed:
                break
    if log:
        plan["tasks"] = tasks
        existing = _dict_list(plan.get("taskgraph_resolution_log"))
        plan["taskgraph_resolution_log"] = [
            *existing,
            *[
                {
                    "resolution": "serialized_parallel_write_conflict",
                    "task_id": item["task_id"],
                    "depends_on_added": item["depends_on_added"],
                    "shared_files": list(item["shared_files"]),
                }
                for item in log
            ],
        ]
    return log


def previous_findings_with_deterministic_refs(
    previous_findings: list[dict[str, Any]] | None,
    repair_log: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    findings = [dict(item) for item in list(previous_findings or []) if isinstance(item, dict)]
    for item in repair_log:
        synthetic = item.get("synthetic_finding")
        if isinstance(synthetic, dict):
            findings.append(dict(synthetic))
    return findings


def apply_deterministic_repairs(
    plan: dict[str, Any],
    *,
    previous_plan: dict[str, Any] | None = None,
    previous_findings: list[dict[str, Any]] | None = None,
    project_root: Path | None = None,
    task_direction: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply machine-fixable repairs before structural review."""

    repaired = copy.deepcopy(plan)
    log: list[dict[str, Any]] = []
    log.extend(_truncate_invariants(repaired))
    log.extend(_fill_missing_layer_owner(repaired))
    log.extend(_ensure_verification_only_task_constraints(repaired, previous_plan))
    log.extend(_normalize_workspace_relative_verify_commands(repaired, project_root=project_root))
    log.extend(_demote_verification_only_write_anchors(repaired, previous_plan))
    log.extend(_remove_verification_only_unrequested_smoke_gates(repaired))
    log.extend(_normalize_verification_only_no_edit_contracts(repaired, previous_plan))
    log.extend(_add_verification_only_frontend_read_only_scope(repaired, task_direction=task_direction, project_root=project_root))
    log.extend(_add_verification_only_truth_docs_read_only_scope(repaired, project_root=project_root))
    log.extend(_add_verification_only_read_later_persistence_scope(repaired, task_direction=task_direction, project_root=project_root))
    log.extend(_demote_verification_only_implementation_canonical_truth(repaired))
    log.extend(_promote_verification_only_tests_to_truth(repaired, project_root))
    log.extend(_sync_verification_only_source_docs_read_only_scope(repaired, project_root=project_root))
    log.extend(_ensure_verification_only_work_all_approval(repaired, task_direction=task_direction))
    log.extend(_ensure_verification_only_report_approval(repaired, task_direction=task_direction))
    log.extend(_remove_owned_files_from_read_only(repaired, project_root))
    log.extend(_promote_review_requested_write_paths(repaired, previous_findings, project_root))
    log.extend(_protect_verify_only_task_direction_paths(repaired, task_direction=task_direction, project_root=project_root))
    log.extend(_add_missing_task_change_log_entries(repaired, previous_plan))
    log.extend(_normalize_change_log_refs(repaired, previous_plan, previous_findings))
    log.extend(_dedupe_verify_recipes(repaired, project_root))
    log.extend(_normalize_conflicting_api_contracts(repaired))
    log.extend(_serialize_parallel_file_conflicts(repaired, project_root))
    if log:
        repaired["plan_revision_log"] = [*list(repaired.get("plan_revision_log") or []), *log]
    return repaired, log


__all__ = [
    "apply_deterministic_repairs",
    "previous_findings_with_deterministic_refs",
]

"""Deterministic consistency checks for model-generated plans.

The planner is allowed to use natural language for approach details, but
cross-task contracts must be structured so workflow can verify them without
asking another model to notice drift.
"""

from __future__ import annotations

import json
import re
from typing import Any


_HTTP_METHODS: frozenset[str] = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_TASK_ID_RE = re.compile(r"\bT[0-9]+(?:[-_][0-9A-Za-z]+)*\b")

_TASK_CONTRACT_FIELDS: tuple[str, ...] = ("provides", "requires", "api_contracts")
_TASK_REVISION_FIELDS: tuple[str, ...] = (
    "task_name",
    "layer_owner",
    "surface",
    "files_to_change",
    "new_files",
    "coverage_hints",
    "approach",
    "invariants",
    "test_plan",
    "verify_cmd",
    "depends_on",
    "forbidden_changes",
    "provides",
    "requires",
    "api_contracts",
)
_PLAN_REVISION_FIELDS: tuple[str, ...] = (
    "summary",
    "business_outcome",
    "out_of_scope",
    "source_of_truth",
    "source_of_truth_canonical",
    "path_type",
    "layers",
    "coverage_hints",
    "module_boundaries",
    "verify_recipes",
    "approval_points",
    "execution_constraints",
    "confidence",
    "confidence_issues",
    "risks",
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


def _task_id(task: dict[str, Any], index: int) -> str:
    return _clean_text(task.get("task_id")) or f"tasks[{index}]"


def _task_index(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(tasks, start=1):
        task_id = _task_id(task, index)
        if task_id:
            indexed[task_id] = dict(task)
    return indexed


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _canonical_key(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


def _api_contracts_for_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    contracts = _dict_list(task.get("api_contracts"))
    for item in _dict_list(task.get("provides")):
        kind = _clean_text(item.get("kind") or item.get("type")).lower()
        if kind in {"api", "api_response", "api_contract", "endpoint"}:
            contracts.append(dict(item))
    return contracts


def _api_contract_error(
    *,
    label: str,
    method: str,
    endpoint: str,
    shape: Any,
) -> str:
    if not method or not endpoint:
        return f"{label} must declare method and endpoint"
    if shape is None or _clean_text(shape) == "":
        return f"{label} must declare response_shape or shape_id"
    return ""


def _field_name(item: dict[str, Any]) -> str:
    return _clean_text(item.get("field") or item.get("name") or item.get("path"))


def _field_contracts(task: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _dict_list(task.get(field_name)):
        kind = _clean_text(item.get("kind") or item.get("type")).lower()
        if kind == "field" or _clean_text(item.get("field")):
            out.append(dict(item))
    return out


def _is_existing_requirement(item: dict[str, Any]) -> bool:
    source = _clean_text(
        item.get("source") or item.get("provider") or item.get("provided_by")
    ).lower()
    return bool(item.get("existing")) or source in {
        "existing",
        "preexisting",
        "repo",
        "repository",
        "schema",
    }


def _dependency_closure(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    by_id = _task_index(tasks)
    memo: dict[str, set[str]] = {}

    def visit(task_id: str, stack: set[str]) -> set[str]:
        if task_id in memo:
            return set(memo[task_id])
        if task_id in stack:
            return set()
        stack.add(task_id)
        task = by_id.get(task_id, {})
        deps: set[str] = set()
        for dep in _string_list(task.get("depends_on")):
            deps.add(dep)
            deps.update(visit(dep, set(stack)))
        memo[task_id] = set(deps)
        return deps

    for task_id in by_id:
        visit(task_id, set())
    return memo


def validate_plan_consistency(plan: dict[str, Any]) -> list[str]:
    """Validate structured cross-task contracts declared by the planner."""

    errors: list[str] = []
    tasks = _dict_list(plan.get("tasks"))
    if not tasks:
        return errors

    for index, task in enumerate(tasks, start=1):
        task_id = _task_id(task, index)
        for field in _TASK_CONTRACT_FIELDS:
            if field not in task:
                errors.append(f"{task_id}.{field} must be present as a list; use [] when not applicable")
            elif not isinstance(task.get(field), list):
                errors.append(f"{task_id}.{field} must be a list")

    errors.extend(_validate_api_contract_consistency(tasks))
    errors.extend(_validate_field_producer_consumer(tasks))
    return errors


def _validate_api_contract_consistency(tasks: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    by_endpoint: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for index, task in enumerate(tasks, start=1):
        task_id = _task_id(task, index)
        for contract_index, contract in enumerate(_api_contracts_for_task(task), start=1):
            method, endpoint = _parse_method_endpoint(contract)
            shape = _response_shape(contract)
            label = f"{task_id}.api_contracts[{contract_index}]"
            contract_error = _api_contract_error(
                label=label,
                method=method,
                endpoint=endpoint,
                shape=shape,
            )
            if contract_error:
                errors.append(contract_error)
                continue
            key = (method, endpoint)
            by_endpoint.setdefault(key, []).append((task_id, _canonical_key(shape), label))

    for (method, endpoint), declared in sorted(by_endpoint.items()):
        shapes = {shape for _task_id, shape, _label in declared}
        if len(shapes) <= 1:
            continue
        labels = ", ".join(label for _task_id, _shape, label in declared)
        errors.append(
            f"api_contracts conflict for {method} {endpoint}: {labels} declare incompatible response shapes"
        )
    return errors


def _field_producers(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    producers: dict[str, set[str]] = {}
    for index, task in enumerate(tasks, start=1):
        task_id = _task_id(task, index)
        for item in _field_contracts(task, "provides"):
            name = _field_name(item)
            if name:
                producers.setdefault(name, set()).add(task_id)
    return producers


def _field_requirement_error(
    *,
    task_id: str,
    name: str,
    producers: set[str],
    reachable: set[str],
) -> str:
    if not producers:
        return f"{task_id} requires field '{name}' but no task provides it"
    if not producers.intersection(reachable):
        providers = ", ".join(sorted(producers))
        return f"{task_id} requires field '{name}' from {providers} but does not depend on a provider task"
    return ""


def _validate_field_producer_consumer(tasks: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    producers = _field_producers(tasks)

    closure = _dependency_closure(tasks)
    for index, task in enumerate(tasks, start=1):
        task_id = _task_id(task, index)
        reachable = set(closure.get(task_id, set()))
        reachable.add(task_id)
        for item in _field_contracts(task, "requires"):
            name = _field_name(item)
            if not name or _is_existing_requirement(item):
                continue
            error = _field_requirement_error(
                task_id=task_id,
                name=name,
                producers=producers.get(name, set()),
                reachable=reachable,
            )
            if error:
                errors.append(error)
    return errors


def _task_revision_signature(task: dict[str, Any]) -> dict[str, Any]:
    return {field: _canonical(task.get(field)) for field in _TASK_REVISION_FIELDS}


def _plan_revision_signature(plan: dict[str, Any]) -> dict[str, Any]:
    return {field: _canonical(plan.get(field)) for field in _PLAN_REVISION_FIELDS}


def _change_log_entries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return _dict_list(plan.get("change_log"))


def _declared_change_targets(plan: dict[str, Any]) -> set[str]:
    targets: set[str] = set()
    for entry in _change_log_entries(plan):
        target = _clean_text(entry.get("task_id") or entry.get("target"))
        if target:
            targets.add(target)
    return targets


def _changed_task_fields(
    previous_task: dict[str, Any],
    current_task: dict[str, Any],
) -> list[str]:
    before = _task_revision_signature(previous_task)
    after = _task_revision_signature(current_task)
    return [field for field in _TASK_REVISION_FIELDS if before.get(field) != after.get(field)]


def _change_target_declared(target: str, declared: set[str]) -> bool:
    return target in declared or "*" in declared


def _validate_task_revision_targets(
    *,
    previous_tasks: dict[str, dict[str, Any]],
    current_tasks: dict[str, dict[str, Any]],
    declared: set[str],
) -> list[str]:
    errors: list[str] = []
    previous_ids = set(previous_tasks)
    current_ids = set(current_tasks)
    for task_id in sorted(previous_ids - current_ids):
        if not _change_target_declared(task_id, declared):
            errors.append(f"change_log missing removed task {task_id}")
    for task_id in sorted(current_ids - previous_ids):
        if not _change_target_declared(task_id, declared):
            errors.append(f"change_log missing added task {task_id}")
    for task_id in sorted(previous_ids & current_ids):
        fields = _changed_task_fields(previous_tasks[task_id], current_tasks[task_id])
        if fields and not _change_target_declared(task_id, declared):
            errors.append(f"change_log missing modified task {task_id} fields={fields}")
    return errors


def _changed_plan_fields(
    *,
    previous_plan: dict[str, Any],
    current_plan: dict[str, Any],
) -> list[str]:
    before_plan = _plan_revision_signature(previous_plan)
    after_plan = _plan_revision_signature(current_plan)
    return [
        field
        for field in _PLAN_REVISION_FIELDS
        if before_plan.get(field) != after_plan.get(field)
    ]


def _validate_plan_level_changes(
    *,
    previous_plan: dict[str, Any],
    current_plan: dict[str, Any],
    declared: set[str],
) -> list[str]:
    changed_fields = _changed_plan_fields(
        previous_plan=previous_plan,
        current_plan=current_plan,
    )
    if changed_fields and not ({"plan", "*"} & declared):
        return [f"change_log missing plan-level fields={changed_fields}"]
    return []


def _validate_change_log_entry(
    *,
    entry_index: int,
    entry: dict[str, Any],
    allowed_from_findings: set[str],
    known_task_ids: set[str] | None = None,
    precondition_response_task_ids: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    target = _clean_text(entry.get("task_id") or entry.get("target"))
    reason = _clean_text(entry.get("reason"))
    fields = _string_list(entry.get("fields"))
    if not target:
        errors.append(f"change_log[{entry_index}].task_id is required")
    if not reason:
        errors.append(f"change_log[{entry_index}].reason is required")
    if not fields:
        errors.append(f"change_log[{entry_index}].fields must be non-empty")
    response_ids = precondition_response_task_ids or set()
    if (
        allowed_from_findings
        and target not in {"plan", "*"}
        and target not in allowed_from_findings
        and target not in response_ids
        and not _target_matches_finding_task(target, allowed_from_findings, known_task_ids or set())
    ):
        errors.append(
            f"change_log[{entry_index}] targets {target}, which was not explicitly referenced by previous findings {sorted(allowed_from_findings)}"
        )
    return errors


def _target_matches_finding_task(
    target: str,
    allowed_from_findings: set[str],
    known_task_ids: set[str],
) -> bool:
    if target not in known_task_ids:
        return False
    target_lower = target.lower()
    for allowed in allowed_from_findings:
        allowed_lower = allowed.lower()
        if target_lower.startswith(f"{allowed_lower}-") or target_lower.startswith(f"{allowed_lower}_"):
            return True
    return False


def _validate_change_log_entries(
    *,
    entries: list[dict[str, Any]],
    allowed_from_findings: set[str],
    known_task_ids: set[str] | None = None,
    precondition_response_task_ids: set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    for entry_index, entry in enumerate(entries, start=1):
        errors.extend(
            _validate_change_log_entry(
                entry_index=entry_index,
                entry=entry,
                allowed_from_findings=allowed_from_findings,
                known_task_ids=known_task_ids or set(),
                precondition_response_task_ids=precondition_response_task_ids,
            )
        )
    return errors


def _precondition_response_task_ids(
    *,
    previous_tasks: dict[str, dict[str, Any]],
    current_tasks: dict[str, dict[str, Any]],
    precondition_replan_hint: dict[str, Any] | None,
) -> set[str]:
    """Identify newly-inserted tasks that respond to a precondition replan hint.

    When ``WORKFLOW_AUTOPILOT_AUTO_REPLAN_ON_PRECONDITION`` triggers a replan,
    the planner is *expected* to insert prerequisite tasks (typically a T0_*
    schema migration) that fix the missing fields/symbols. The default
    ``change_log`` rule rejects any entry targeting a task not mentioned in
    prior reviewer findings, but reviewer findings are by definition silent
    about tasks the planner is supposed to invent.

    A new task counts as a precondition-response if (a) it is not in the
    previous plan and (b) its declared ``provides`` or its task_id text
    references one of the missing field/symbol names from the hint, or (c)
    its task_id starts with ``T0_`` (the canonical prereq prefix used by the
    hint template).
    """

    if not precondition_replan_hint:
        return set()
    missing_fields = list(precondition_replan_hint.get("missing_field_preconditions") or [])
    missing_symbols = list(precondition_replan_hint.get("missing_symbol_preconditions") or [])
    if not (missing_fields or missing_symbols):
        return set()
    new_task_ids = {tid for tid in current_tasks if tid not in previous_tasks}
    if not new_task_ids:
        return set()
    out: set[str] = set()
    needles = [str(item).lower() for item in (*missing_fields, *missing_symbols) if str(item).strip()]
    for task_id in new_task_ids:
        if task_id.upper().startswith("T0_"):
            out.add(task_id)
            continue
        task = current_tasks.get(task_id) or {}
        provides_text = " ".join(
            str(item.get("name") or item)
            for item in list(task.get("provides") or [])
            if isinstance(item, (dict, str))
        ).lower()
        haystack = f"{task_id} {provides_text}".lower()
        if any(needle in haystack for needle in needles):
            out.add(task_id)
    return out


def finding_task_ids(findings: list[dict[str, Any]] | None) -> set[str]:
    """Extract explicit task ids mentioned by reviewer findings."""

    task_ids: set[str] = set()
    for finding in list(findings or []):
        for key in ("task_id", "task"):
            value = _clean_text(finding.get(key))
            if value:
                task_ids.add(value)
        for key in ("task_ids", "affected_tasks", "affected_task_ids"):
            for value in _string_list(finding.get(key)):
                task_ids.add(value)
        text = " ".join(
            _clean_text(finding.get(key))
            for key in ("category", "description", "recommendation")
        )
        task_ids.update(match.group(0) for match in _TASK_ID_RE.finditer(text))
    return task_ids


def validate_plan_revision(
    *,
    previous_plan: dict[str, Any] | None,
    current_plan: dict[str, Any],
    previous_findings: list[dict[str, Any]] | None = None,
    precondition_replan_hint: dict[str, Any] | None = None,
) -> list[str]:
    """Require round N+1 plans to declare exactly what changed.

    This does not replace full-plan review. It prevents silent rewrites by
    forcing every task-level or plan-level diff to be represented in
    ``change_log`` before the reviewer sees the payload.

    When ``precondition_replan_hint`` is non-empty (auto-replan triggered by
    a readiness BLOCK), newly-inserted tasks that respond to the hint are
    exempted from the "must be referenced by previous_findings" rule. The
    hint is the upstream signal that the planner is expected to add such
    tasks; reviewer findings cannot pre-name task ids that did not exist.
    """

    if not previous_plan:
        return []
    errors: list[str] = []
    entries = _change_log_entries(current_plan)
    if not entries:
        errors.append("change_log must be non-empty when revising a previous plan")
    declared = _declared_change_targets(current_plan)
    allowed_from_findings = finding_task_ids(previous_findings)

    previous_tasks = _task_index(_dict_list(previous_plan.get("tasks")))
    current_tasks = _task_index(_dict_list(current_plan.get("tasks")))
    known_task_ids = set(previous_tasks) | set(current_tasks)
    response_ids = _precondition_response_task_ids(
        previous_tasks=previous_tasks,
        current_tasks=current_tasks,
        precondition_replan_hint=precondition_replan_hint,
    )
    errors.extend(
        _validate_task_revision_targets(
            previous_tasks=previous_tasks,
            current_tasks=current_tasks,
            declared=declared,
        )
    )
    errors.extend(
        _validate_plan_level_changes(
            previous_plan=previous_plan,
            current_plan=current_plan,
            declared=declared,
        )
    )
    errors.extend(
        _validate_change_log_entries(
            entries=entries,
            allowed_from_findings=allowed_from_findings,
            known_task_ids=known_task_ids,
            precondition_response_task_ids=response_ids,
        )
    )
    return errors


def _task_files(task: dict[str, Any]) -> list[str]:
    """Union of files this task touches: files_to_change ∪ new_files."""
    out = list(_string_list(task.get("files_to_change")))
    for path in _string_list(task.get("new_files")):
        if path not in out:
            out.append(path)
    return out


def _is_test_path(path: str) -> bool:
    """Mirror review_precheck.is_test_file's path-shape rules without
    importing across packages (planning_consistency is consumed by both
    planning_agent and planning_orchestrator at module load time)."""
    lowered = str(path or "").strip().replace("\\", "/").lower()
    if not lowered:
        return False
    name = lowered.rsplit("/", 1)[-1]
    if "/tests/" in lowered or lowered.startswith("tests/") or "/__tests__/" in lowered:
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name.endswith((".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx")):
        return True
    if name.endswith((".spec.js", ".spec.jsx", ".test.js", ".test.jsx")):
        return True
    return False


_DOCS_ADVISORY_EXTENSIONS: tuple[str, ...] = (".md", ".rst", ".adoc")


def _is_docs_path(path: str) -> bool:
    lowered = str(path or "").strip().replace("\\", "/").lower()
    if not lowered or _is_test_path(lowered):
        return False
    name = lowered.rsplit("/", 1)[-1]
    if name.startswith("requirements") and name.endswith(".txt"):
        return False
    if "/fixtures/" in lowered:
        return False
    if lowered.startswith("docs/"):
        return True
    return any(name.endswith(ext) for ext in _DOCS_ADVISORY_EXTENSIONS)


def detect_docs_only_without_test_coverage(plan_payload: dict[str, Any]) -> list[str]:
    """Advisory: planner produced a docs-only task in a plan whose other
    tasks include no test files. The gate short-circuit (v5 P0) lets the
    docs task proceed, but a plan with no downstream test-bearing task is
    a smell — likely the planner forgot to schedule the implementation
    follow-up.

    Returns one advisory string per docs-only task lacking peer test
    coverage. Empty list when there is no problem (no docs-only tasks,
    or some other task in the plan does carry test files).
    """
    tasks = _dict_list(plan_payload.get("tasks"))
    if not tasks:
        return []
    docs_only_task_ids: list[str] = []
    plan_has_test_task = False
    for index, task in enumerate(tasks, start=1):
        files = _task_files(task)
        if not files:
            continue
        if any(_is_test_path(path) for path in files):
            plan_has_test_task = True
            continue
        if all(_is_docs_path(path) for path in files):
            docs_only_task_ids.append(_task_id(task, index))
    if plan_has_test_task or not docs_only_task_ids:
        return []
    advisories = [
        (
            f"docs_only_task_without_test_partner: task {tid} only changes docs/markdown, "
            "and no other task in this plan declares test files. Add a downstream "
            "implementation+test task before execution, or confirm the docs-only scope "
            "is intentional (the deterministic guard will let it through but downstream "
            "test coverage will not be enforced)."
        )
        for tid in docs_only_task_ids
    ]
    return advisories


__all__ = [
    "detect_docs_only_without_test_coverage",
    "finding_task_ids",
    "validate_plan_consistency",
    "validate_plan_revision",
]

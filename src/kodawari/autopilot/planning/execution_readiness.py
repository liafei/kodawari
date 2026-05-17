"""Execution-readiness checks for contract-first task cards."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

READINESS_SCHEMA_VERSION = "execution.readiness.v1"
READINESS_FILENAME = ".execution_readiness.json"

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_ALTER_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().replace("\\", "/") for item in value if str(item).strip()]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _requirement_name(item: dict[str, Any]) -> str:
    return _clean(item.get("name") or item.get("field") or item.get("path"))


def _is_existing_field_requirement(item: dict[str, Any]) -> bool:
    kind = _clean(item.get("kind") or item.get("type")).lower()
    if kind != "field" and not _clean(item.get("field")):
        return False
    source = _clean(item.get("source") or item.get("provider") or item.get("provided_by")).lower()
    return bool(item.get("existing")) or source in {"existing", "preexisting", "repo", "repository", "schema"}


def _schema_like(path: str) -> bool:
    lowered = path.lower().replace("\\", "/")
    return (
        lowered.endswith(".sql")
        or "schema" in lowered
        or "migration" in lowered
        or lowered.endswith("models.py")
        or lowered.endswith("tables.py")
    )


def _schema_source_files(project_root: Path) -> list[Path]:
    ignore = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".workflow",
        ".workflow_runtime",
        ".python-packages",
        ".gradle",
        ".gradle-home",
        ".android-home",
        ".android-studio",
        "tmp",
    }
    files: list[Path] = []
    root = project_root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignore]
        current = Path(dirpath)
        try:
            rel_dir = current.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel_dir == "tests" or rel_dir.startswith("tests/"):
            dirnames[:] = []
            continue
        for filename in filenames:
            lowered = filename.lower()
            if not (
                lowered.endswith(".sql")
                or ("schema" in lowered and lowered.endswith(".py"))
                or ("model" in lowered and lowered.endswith(".py"))
                or ("table" in lowered and lowered.endswith(".py"))
            ):
                continue
            files.append(current / filename)
    return files


def _schema_texts(project_root: Path) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for path in _schema_source_files(project_root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        texts.append((path.relative_to(project_root).as_posix(), text))
    return texts


def _sql_table_columns(text: str) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    for match in _CREATE_TABLE_RE.finditer(text):
        table = match.group(1).strip().lower()
        body = match.group(2)
        columns: set[str] = set()
        for raw in body.splitlines():
            line = raw.strip().strip(",")
            if not line:
                continue
            first = line.split(maxsplit=1)[0].strip('"`[]').lower()
            if first and first not in {"primary", "foreign", "constraint", "unique", "check"}:
                columns.add(first)
        tables.setdefault(table, set()).update(columns)
    # ALTER TABLE <t> ADD COLUMN <c> additions are how Sqlite/Postgres
    # migrations layer new columns onto an existing table — readiness must
    # see those rows as part of the table's column set, otherwise a planner
    # that legitimately depends on a recently-added column gets blocked.
    for match in _ALTER_ADD_COLUMN_RE.finditer(text):
        table = match.group(1).strip().lower()
        column = match.group(2).strip().lower()
        tables.setdefault(table, set()).add(column)
    return tables


def collect_field_evidence(
    requirements: list[str],
    project_root: Path,
    *,
    code_match_limit: int = 5,
) -> dict[str, dict[str, Any]]:
    """For each ``table.column`` requirement, summarize structural DDL
    evidence and code-string mentions so a downstream planner hint can
    show the planner *why* readiness considers the column missing.

    Returns ``{requirement: {ddl_create_matches, ddl_alter_matches,
    code_string_files, conclusion}}``. The planner sees concrete numbers
    instead of just an instruction, which empirically helps it weigh
    "the column appears in some .py files" against "no DDL ever creates
    or alters it".
    """

    schema_texts = _schema_texts(project_root)
    code_texts = _python_source_files(project_root)
    out: dict[str, dict[str, Any]] = {}
    for raw in requirements:
        requirement = _clean(raw)
        if "." not in requirement:
            continue
        table, column = [part.strip().lower() for part in requirement.split(".", 1)]
        if not table or not column:
            continue
        create_matches = 0
        alter_matches = 0
        for _path, text in schema_texts:
            tables = _sql_table_columns_create_only(text)
            if column in tables.get(table, set()):
                create_matches += 1
            for match in _ALTER_ADD_COLUMN_RE.finditer(text):
                if match.group(1).lower() == table and match.group(2).lower() == column:
                    alter_matches += 1
        code_files: list[str] = []
        needle = column
        for source in code_texts:
            try:
                rel = source.relative_to(project_root.resolve()).as_posix()
            except ValueError:
                continue
            try:
                body = source.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if needle in body:
                code_files.append(rel)
                if len(code_files) >= code_match_limit:
                    break
        out[requirement] = {
            "ddl_create_matches": create_matches,
            "ddl_alter_matches": alter_matches,
            "code_string_files": code_files,
            "conclusion": (
                "column exists in DDL"
                if (create_matches + alter_matches) > 0
                else "column does NOT exist in any CREATE TABLE or ALTER TABLE; "
                "code-string mentions are JSON keys, not real columns"
            ),
        }
    return out


def _sql_table_columns_create_only(text: str) -> dict[str, set[str]]:
    """Same as ``_sql_table_columns`` but excludes ALTER additions, so the
    evidence collector can report the two contributions separately."""

    tables: dict[str, set[str]] = {}
    for match in _CREATE_TABLE_RE.finditer(text):
        table = match.group(1).strip().lower()
        body = match.group(2)
        columns: set[str] = set()
        for raw in body.splitlines():
            line = raw.strip().strip(",")
            if not line:
                continue
            first = line.split(maxsplit=1)[0].strip('"`[]').lower()
            if first and first not in {"primary", "foreign", "constraint", "unique", "check"}:
                columns.add(first)
        tables.setdefault(table, set()).update(columns)
    return tables


def _table_columns(table: str, texts: list[tuple[str, str]]) -> set[str]:
    target = table.strip().lower()
    columns: set[str] = set()
    if not target:
        return columns
    for _path, text in texts:
        columns.update(_sql_table_columns(text).get(target, set()))
    return columns


def _field_exists(requirement: str, texts: list[tuple[str, str]]) -> bool:
    """Strict structural check: the column must appear inside CREATE TABLE
    or ALTER TABLE ADD COLUMN for the named table. Loose substring matches
    on schema-like text are intentionally rejected — that heuristic was
    falsely passing requirements like ``social_thread_snapshots.crawl_provider_kind``
    because the column name appeared in unrelated service modules.
    """

    if "." not in requirement:
        # Bare-name requirements have no table to anchor against; require a
        # CREATE TABLE that defines the column under any table.
        needle = requirement.strip().lower()
        if not needle:
            return False
        return any(
            needle in columns
            for _path, text in texts
            for columns in _sql_table_columns(text).values()
        )
    table, column = [part.strip().lower() for part in requirement.split(".", 1)]
    if not table or not column:
        return False
    for _path, text in texts:
        tables = _sql_table_columns(text)
        if column in tables.get(table, set()):
            return True
    return False


def _looks_like_json_payload_column(column: str) -> bool:
    lowered = column.strip().lower()
    return lowered.endswith("_json") or lowered.endswith("json") or ("payload" in lowered and "json" in lowered)


def _json_payload_field_candidate(
    item: dict[str, Any],
    requirement: str,
    texts: list[tuple[str, str]],
) -> bool:
    """Return True only when the planner explicitly opted into the
    JSON-payload tolerance.

    The earlier behaviour tolerated *any* missing ``table.column`` whenever the
    table happened to own a JSON-shaped column. Real-run regression: a real
    schema gap (``social_thread_snapshots.crawl_provider_kind``) was masked
    because that table has ``snapshot_payload_json``. Now the requirement must
    declare ``accessor: "json_payload"`` (or set ``json_payload_key: true``)
    for the tolerance to apply — a planner that means "real column" is taken
    at its word.
    """

    accessor = _clean(item.get("accessor") or item.get("access_via")).lower()
    explicit_json_key = bool(item.get("json_payload_key"))
    if accessor not in {"json_payload", "json", "payload_json"} and not explicit_json_key:
        return False
    if "." not in requirement:
        return False
    table, column = [part.strip().lower() for part in requirement.split(".", 1)]
    if not table or not column:
        return False
    columns = _table_columns(table, texts)
    if column in columns:
        return False
    return any(_looks_like_json_payload_column(candidate) for candidate in columns)


def _schema_requirement_candidate(requirement: str, texts: list[tuple[str, str]]) -> bool:
    """Decide whether ``table.field`` should be schema-checked at all.

    Only treat the requirement as a schema candidate when the table name
    actually appears as a known table in some CREATE TABLE statement.
    Substring matches on free text (the old behaviour) routinely picked
    up ``schema``-mentioning Python comments and produced false candidates
    that then false-passed via the now-removed substring branch in
    ``_field_exists``.
    """

    if "." not in requirement:
        return False
    owner = requirement.split(".", 1)[0].strip().lower()
    if not owner:
        return False
    for _path, text in texts:
        if owner in _sql_table_columns(text):
            return True
    return False


def _schema_mutation_allowed(task_card: dict[str, Any]) -> bool:
    scoped = _string_list(task_card.get("files_to_change")) + _string_list(task_card.get("new_files"))
    return any(_schema_like(item) for item in scoped)


def _is_existing_symbol_requirement(item: dict[str, Any]) -> bool:
    kind = _clean(item.get("kind") or item.get("type")).lower()
    if kind != "symbol":
        return False
    source = _clean(item.get("source") or item.get("provider") or item.get("provided_by")).lower()
    return bool(item.get("existing")) or source in {"existing", "preexisting", "repo", "repository"}


def _split_symbol_reference(reference: str) -> tuple[str, str]:
    """Return ``(module_hint, symbol)`` from a requirement name.

    Accepts ``module.path:Symbol``, ``module/path.py:Symbol``, or just
    ``Symbol``. The module hint is optional — when present it constrains
    which files are scanned for the symbol.
    """

    raw = _clean(reference)
    if ":" in raw:
        module, symbol = raw.rsplit(":", 1)
        return _clean(module), _clean(symbol)
    return "", raw


_PYTHON_SYMBOL_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _python_symbol_pattern(symbol: str) -> "re.Pattern[str]":
    cached = _PYTHON_SYMBOL_RE_CACHE.get(symbol)
    if cached is not None:
        return cached
    escaped = re.escape(symbol)
    pattern = re.compile(
        rf"^(?:def\s+{escaped}\s*\(|class\s+{escaped}\s*[\(:]|{escaped}\s*[:=])",
        re.MULTILINE,
    )
    _PYTHON_SYMBOL_RE_CACHE[symbol] = pattern
    return pattern


def _python_source_files(project_root: Path) -> list[Path]:
    ignore = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".workflow",
        ".workflow_runtime",
        ".python-packages",
        ".gradle",
        ".gradle-home",
        ".android-home",
        ".android-studio",
        "tmp",
        "tests",
        "dist",
        "build",
    }
    files: list[Path] = []
    root = project_root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignore]
        current = Path(dirpath)
        for filename in filenames:
            if filename.endswith(".py"):
                files.append(current / filename)
    return files


def _module_hint_matches(path: Path, project_root: Path, hint: str) -> bool:
    if not hint:
        return True
    try:
        rel = path.relative_to(project_root).as_posix().lower()
    except ValueError:
        return False
    needle = hint.lower().replace("\\", "/").lstrip("/")
    if needle.endswith(".py"):
        return rel.endswith(needle)
    # Treat dotted module path as a path fragment.
    fragment = needle.replace(".", "/")
    return fragment in rel


def _python_symbol_exists(reference: str, project_root: Path) -> bool:
    module_hint, symbol = _split_symbol_reference(reference)
    if not symbol:
        return False
    pattern = _python_symbol_pattern(symbol)
    for path in _python_source_files(project_root):
        if not _module_hint_matches(path, project_root, module_hint):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            return True
    return False


def _python_module_creation_allowed(task_card: dict[str, Any]) -> bool:
    """Return True when the task is permitted to create the missing module.

    Mirrors the schema_mutation_allowed escape hatch: if the task explicitly
    declares it will create a Python file, missing-symbol preconditions are
    not blocking for that task.
    """

    scoped = _string_list(task_card.get("new_files"))
    return any(item.lower().endswith(".py") for item in scoped)


def evaluate_execution_readiness(*, project_root: Path, task_card: dict[str, Any]) -> dict[str, Any]:
    """Return PASS or BLOCKED for existing field and symbol preconditions."""

    texts = _schema_texts(project_root)
    requires = _dict_list(task_card.get("requires"))

    field_items: list[tuple[str, dict[str, Any]]] = [
        (_requirement_name(item), item)
        for item in requires
        if _is_existing_field_requirement(item) and _requirement_name(item)
    ]
    field_requirements = [name for name, _item in field_items]
    field_checked = [name for name in field_requirements if _schema_requirement_candidate(name, texts)]
    json_payload_fields: list[str] = []
    for name, item in field_items:
        if name not in field_checked:
            continue
        if _field_exists(name, texts):
            continue
        if _json_payload_field_candidate(item, name, texts):
            json_payload_fields.append(name)
    field_missing = [
        name
        for name in field_checked
        if not _field_exists(name, texts) and name not in json_payload_fields
    ]

    symbol_requirements = [
        _requirement_name(item)
        for item in requires
        if _is_existing_symbol_requirement(item) and _requirement_name(item)
    ]
    symbol_missing = [name for name in symbol_requirements if not _python_symbol_exists(name, project_root)]

    schema_mutation_allowed = _schema_mutation_allowed(task_card)
    module_creation_allowed = _python_module_creation_allowed(task_card)
    field_blocking = bool(field_missing) and not schema_mutation_allowed
    symbol_blocking = bool(symbol_missing) and not module_creation_allowed
    blocked = field_blocking or symbol_blocking
    status = "BLOCKED" if blocked else "PASS"

    missing = list(field_missing) + list(symbol_missing)
    checked = list(field_checked) + list(symbol_requirements)
    suggestions: list[str] = []
    if field_blocking:
        suggestions.append("Add or authorize a schema/migration task for: " + ", ".join(field_missing))
    if symbol_blocking:
        suggestions.append("Add or authorize a Python module/symbol task for: " + ", ".join(symbol_missing))

    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "reason": "missing_existing_preconditions" if blocked else "",
        "missing_preconditions": missing,
        "checked_preconditions": checked,
        "missing_field_preconditions": field_missing,
        "json_payload_field_preconditions": json_payload_fields,
        "missing_symbol_preconditions": symbol_missing,
        "schema_mutation_allowed": schema_mutation_allowed,
        "module_creation_allowed": module_creation_allowed,
        "suggested_next_task": "; ".join(suggestions),
    }


def _schema_mutating_task_ids(tasks: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for task in tasks:
        task_id = _clean(task.get("task_id"))
        if task_id and _schema_mutation_allowed(task):
            out.add(task_id)
    return out


def _transitive_dependency_ids(
    *,
    task: dict[str, Any],
    task_index: dict[str, dict[str, Any]],
) -> set[str]:
    """Walk ``depends_on`` edges from ``task`` and return every reachable id."""

    out: set[str] = set()
    pending = list(_string_list(task.get("depends_on")))
    while pending:
        dep_id = _clean(pending.pop())
        if not dep_id or dep_id in out:
            continue
        out.add(dep_id)
        downstream = task_index.get(dep_id)
        if downstream is None:
            continue
        pending.extend(_string_list(downstream.get("depends_on")))
    return out


def _task_can_assume_schema_mutation(
    *,
    task: dict[str, Any],
    schema_mutating_ids: set[str],
    task_index: dict[str, dict[str, Any]],
) -> bool:
    """A task may tolerate missing existing-field preconditions only when:

    (a) the task itself is the schema-mutating one (it adds the column /
        runs the migration), or
    (b) the task transitively depends on a schema-mutating task that will
        provide the missing fields before this task executes.

    The previous rule — "any task in the plan mutates schema, so every task
    is exempt" — was unsound: a plan with one untouched migration task and
    one ranking task would let the ranking task claim missing columns are
    fine even when the ranking task is ordered first or has no dependency
    edge to the migration. Mimo runs exploited this to slide through with
    ``source: existing`` lies. Codex runs declare honestly and were
    inconsistently treated depending on whether the plan contained a
    migration task that had already been pruned by review. This rule is
    now per-task and dependency-aware.
    """

    task_id = _clean(task.get("task_id"))
    if task_id and task_id in schema_mutating_ids:
        return True
    deps = _transitive_dependency_ids(task=task, task_index=task_index)
    return bool(deps & schema_mutating_ids)


def evaluate_plan_execution_readiness(*, project_root: Path, plan_payload: dict[str, Any]) -> dict[str, Any]:
    """Return PASS or BLOCKED for plan-level existing-field preconditions.

    A single task can be blocked when it requires an existing schema field that
    is absent and the task itself is not allowed to touch schema. At plan time
    we also account for a planned schema/migration task elsewhere in the graph:
    if THIS task or any of its declared dependencies has schema scope, the
    plan is allowed to make those fields real before dependent work executes.
    """

    tasks = _dict_list(dict(plan_payload or {}).get("tasks"))
    task_payloads: list[dict[str, Any]] = []
    schema_mutating_ids = _schema_mutating_task_ids(tasks)
    task_index = {_clean(task.get("task_id")): task for task in tasks if _clean(task.get("task_id"))}
    plan_schema_mutation_allowed = bool(schema_mutating_ids)
    missing: list[str] = []
    checked: list[str] = []
    blocked_tasks: list[str] = []
    seen_missing: set[str] = set()
    seen_checked: set[str] = set()

    for task in tasks:
        task_id = _clean(task.get("task_id"))
        task_readiness = evaluate_execution_readiness(project_root=project_root, task_card=task)
        task_readiness["task_id"] = task_id
        task_readiness["plan_schema_mutation_allowed"] = plan_schema_mutation_allowed
        task_missing = [item for item in _string_list(task_readiness.get("missing_preconditions")) if item]
        for item in _string_list(task_readiness.get("checked_preconditions")):
            key = item.lower()
            if key not in seen_checked:
                seen_checked.add(key)
                checked.append(item)
        for item in task_missing:
            key = item.lower()
            if key not in seen_missing:
                seen_missing.add(key)
                missing.append(item)
        task_can_assume = _task_can_assume_schema_mutation(
            task=task,
            schema_mutating_ids=schema_mutating_ids,
            task_index=task_index,
        )
        task_readiness["task_can_assume_schema_mutation"] = task_can_assume
        if task_readiness.get("status") == "BLOCKED" and task_can_assume:
            task_readiness["status"] = "PASS"
            task_readiness["reason"] = "schema_mutation_planned"
            task_readiness["suggested_next_task"] = ""
        if task_readiness.get("status") == "BLOCKED":
            blocked_tasks.append(task_id or f"task[{len(task_payloads) + 1}]")
        task_payloads.append(task_readiness)

    blocked = bool(blocked_tasks)
    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "plan",
        "status": "BLOCKED" if blocked else "PASS",
        "reason": "missing_existing_preconditions" if blocked else "",
        "missing_preconditions": missing,
        "checked_preconditions": checked,
        "schema_mutation_allowed": plan_schema_mutation_allowed,
        "blocked_tasks": blocked_tasks,
        "task_readiness": task_payloads,
        "suggested_next_task": (
            "Add or authorize a schema/migration task for: " + ", ".join(missing)
            if blocked and missing
            else ""
        ),
    }


def write_execution_readiness(planning_dir: Path, payload: dict[str, Any]) -> Path:
    path = (Path(planning_dir) / READINESS_FILENAME).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = [
    "READINESS_FILENAME",
    "READINESS_SCHEMA_VERSION",
    "evaluate_execution_readiness",
    "evaluate_plan_execution_readiness",
    "write_execution_readiness",
]

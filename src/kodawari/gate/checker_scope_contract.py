"""Scope, boundary, schema discovery, and source-of-truth helpers."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from typing import Any

from kodawari.gate.ast_checker import check_source_of_truth_conflict_ast
from kodawari.source_of_truth import canonicalize_source_of_truth, source_of_truth_allows_target

_SQL_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def discover_project_schema_files(project_root: Path) -> list[str]:
    ignore_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
    schema_files: list[str] = []
    for path in sorted(project_root.rglob("*.schema.json")):
        if not path.is_file():
            continue
        if any(part in ignore_dirs for part in path.parts):
            continue
        schema_files.append(str(path))
    return schema_files


def _normalize_path(path: str, *, project_root: Path | None = None) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if project_root is not None:
            try:
                return resolved.relative_to(project_root.resolve()).as_posix()
            except ValueError:
                return resolved.as_posix()
        return resolved.as_posix()
    if project_root is not None:
        root = project_root.resolve()
        resolved = (root / candidate).resolve()
        try:
            return resolved.relative_to(root).as_posix()
        except ValueError:
            return resolved.as_posix()
    while raw.startswith("./"):
        raw = raw[2:]
    parts: list[str] = []
    for token in raw.split("/"):
        if token in {"", "."}:
            continue
        if token == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        if token == "..":
            parts.append(token)
            continue
        parts.append(token)
    return "/".join(parts)


def _normalize_path_list(values: list[str], *, project_root: Path | None = None) -> list[str]:
    normalized: list[str] = []
    for item in values:
        text = _normalize_path(item, project_root=project_root)
        if text:
            normalized.append(text)
    return normalized


def _path_matches_scope(path: str, scope: str) -> bool:
    if path == scope:
        return True
    if scope.endswith("/"):
        return path.startswith(scope)
    return path.startswith(scope + "/")


def _allowed_scope_with_tests(allowed_files: list[str]) -> list[str]:
    allowed: list[str] = []
    for raw in allowed_files:
        normalized = _normalize_path(raw)
        if not normalized:
            continue
        if normalized not in allowed:
            allowed.append(normalized)
        stem = Path(normalized).stem
        test_name = f"tests/test_{stem}.py"
        if test_name not in allowed:
            allowed.append(test_name)
        if normalized.startswith("app/") or normalized.endswith("/main.py"):
            if "tests/test_api.py" not in allowed:
                allowed.append("tests/test_api.py")
    return allowed


def _task_graph_allowed_files(task_graph: dict[str, Any] | None) -> list[str]:
    graph = dict(task_graph or {})
    allowed: list[str] = []
    for task in list(graph.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        for raw in list(task.get("core_files") or []):
            normalized = _normalize_path(str(raw))
            if normalized and normalized not in allowed:
                allowed.append(normalized)
    return allowed


def check_scope_drift(
    changed_files: list[str],
    allowed_files: list[str],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    changed = _normalize_path_list(changed_files, project_root=project_root)
    allowed = _allowed_scope_with_tests(_normalize_path_list(allowed_files, project_root=project_root))
    out_of_scope: list[str] = []
    if not allowed:
        return {
            "status": "WARN",
            "drifted": False,
            "allowed_files": [],
            "changed_files": changed,
            "out_of_scope_files": [],
            "details": "No allowed_files configured; scope drift check skipped.",
        }
    for path in changed:
        if any(_path_matches_scope(path, scope) for scope in allowed):
            continue
        out_of_scope.append(path)
    return {
        "status": "FAIL" if out_of_scope else "PASS",
        "drifted": bool(out_of_scope),
        "allowed_files": allowed,
        "changed_files": changed,
        "out_of_scope_files": out_of_scope,
        "details": "Detected files outside task scope." if out_of_scope else "All changed files are within scope.",
    }


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError):
        return ""


def _resolve_changed_python_files(changed_files: list[str], project_root: Path) -> list[tuple[str, Path]]:
    root = Path(project_root).resolve()
    resolved: list[tuple[str, Path]] = []
    for raw in changed_files:
        rel = _normalize_path(raw)
        if not rel.endswith(".py"):
            continue
        resolved.append((rel, (root / rel).resolve()))
    return resolved


def check_layer_boundary_simple(changed_files: list[str], project_root: Path) -> list[str]:
    violations: list[str] = []
    root = Path(project_root).resolve()
    for rel, file_path in _resolve_changed_python_files(changed_files, project_root):
        lowered = rel.lower()
        if "route" not in lowered and "routes" not in lowered:
            continue
        content = _layer_boundary_content(root, rel, file_path)
        if not content:
            continue
        if _layer_boundary_scan_text(content):
            violations.append(f"{rel}: route layer cannot import repository directly")
    return sorted(set(violations))


def _sql_write_targets(content: str) -> list[str]:
    return [f"db.{str(item).lower()}" for item in _SQL_WRITE_RE.findall(content or "")]


def _decode_process_output(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")
    return str(value or "")


def _git_added_lines_impl(project_root: Path, rel_path: str) -> list[str] | None:
    command = [
        "git",
        "-C",
        str(project_root),
        "diff",
        "--unified=0",
        "--no-color",
        "--",
        rel_path,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    stdout = _decode_process_output(getattr(result, "stdout", b""))
    added: list[str] = []
    for line in stdout.splitlines():
        if not line.startswith("+"):
            continue
        if line.startswith("+++"):
            continue
        added.append(line[1:])
    return added


def _git_path_tracked(project_root: Path, rel_path: str) -> bool | None:
    command = ["git", "-C", str(project_root), "ls-files", "--error-unmatch", rel_path]
    try:
        result = subprocess.run(command, check=False, capture_output=True)
    except FileNotFoundError:
        return None
    return result.returncode == 0


def _layer_boundary_scan_text(content: str) -> bool:
    if re.search(r"from\s+.*repository(?:\.[A-Za-z0-9_]+)*\s+import\s+", content):
        return True
    return bool(re.search(r"import\s+.*repository", content))


def _layer_boundary_content(project_root: Path, rel_path: str, file_path: Path) -> str:
    added_lines = _git_added_lines_impl(project_root, rel_path)
    if added_lines:
        return "\n".join(added_lines)
    if added_lines == [] and _git_path_tracked(project_root, rel_path):
        return ""
    return _read_text(file_path)


def run_source_of_truth_conflict_check(
    changed_files: list[str],
    project_root: Path,
    declared_sot: list[str],
    *,
    declared_sot_canonical: list[str] | None = None,
    git_added_lines_fn: Any = None,
    ast_checker_fn: Any = None,
) -> list[str]:
    declared_raw = _string_list(declared_sot)
    declared_canonical = _string_list(declared_sot_canonical) or canonicalize_source_of_truth(declared_raw)
    git_added_lines = git_added_lines_fn or _git_added_lines_impl
    ast_checker = ast_checker_fn or check_source_of_truth_conflict_ast
    violations: list[str] = []
    for rel, file_path in _resolve_changed_python_files(changed_files, project_root):
        content = _read_text(file_path)
        added_lines = git_added_lines(project_root, rel)
        scan_source = "\n".join(added_lines) if added_lines is not None else content
        targets = set(_sql_write_targets(scan_source))
        unknown = sorted(
            item
            for item in targets
            if not source_of_truth_allows_target(
                target=item,
                declared_raw=declared_raw,
                declared_canonical=declared_canonical,
            )
        )
        if unknown:
            violations.append(f"{rel}: writes undeclared source_of_truth targets {unknown}")
    ast_violations = ast_checker(
        changed_files,
        project_root,
        declared_raw,
        declared_sot_canonical=declared_canonical,
    )
    return sorted(set(violations + list(ast_violations)))


def check_source_of_truth_conflict(
    changed_files: list[str],
    project_root: Path,
    declared_sot: list[str],
    declared_sot_canonical: list[str] | None = None,
) -> list[str]:
    return run_source_of_truth_conflict_check(
        changed_files,
        project_root,
        declared_sot,
        declared_sot_canonical=declared_sot_canonical,
    )


__all__ = [
    "check_layer_boundary_simple",
    "check_scope_drift",
    "check_source_of_truth_conflict",
    "discover_project_schema_files",
    "run_source_of_truth_conflict_check",
    "_decode_process_output",
    "_git_added_lines_impl",
    "_normalize_path",
    "_normalize_path_list",
    "_read_text",
    "_resolve_changed_python_files",
    "_sql_write_targets",
    "_string_list",
    "_task_graph_allowed_files",
]

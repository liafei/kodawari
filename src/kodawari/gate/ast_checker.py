"""AST-based heuristic checkers for contract-first compliance."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
from typing import Any

from kodawari.source_of_truth import canonicalize_source_of_truth, source_of_truth_allows_target


SQL_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
SQL_WRITE_TOKEN_RE = re.compile(r"\b(?:insert|update|delete|upsert)\b", re.IGNORECASE)
SESSION_WRITE_MODEL_RE = re.compile(
    r"\bsession\.(?:add|delete|merge|update)\(\s*([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

_WRITE_CALL_HINTS = (
    "insert",
    "update",
    "delete",
    "upsert",
    "save",
    "merge",
    "commit",
    "flush",
    "execute",
    "bulk_create",
    "bulk_update",
    "session.add",
    "session.delete",
    "session.merge",
    "session.update",
)
_INVALIDATE_HINTS = (
    "invalidate",
    "expire",
    "evict",
    "clear_cache",
    "cache.delete",
    "cache.clear",
    "cache.invalidate",
    "cache.expire",
)
_CACHE_HINTS = ("cache", "redis", "memcache")


@dataclass
class _CacheFnFacts:
    name: str
    line: int
    calls: set[str] = field(default_factory=set)
    write_hits: list[str] = field(default_factory=list)
    invalidation_hits: list[str] = field(default_factory=list)
    cache_hits: list[str] = field(default_factory=list)


def _normalize_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _resolve_file(project_root: Path, relative_path: str) -> Path:
    return (project_root / _normalize_path(relative_path)).resolve()


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (UnicodeDecodeError, OSError):
        return ""


def _parse_tree(source: str) -> ast.AST | None:
    if not source:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _decode_process_output(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.decode("utf-8", errors="replace")
    return str(value or "")


def _git_added_lines(project_root: Path, rel_path: str) -> list[str] | None:
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


def _route_layer_file(path: str) -> bool:
    normalized = _normalize_path(path).lower()
    return "/route" in normalized or "/routes" in normalized or normalized.endswith("route.py")


def _repository_reference_in_import(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if "repository" in str(alias.name).lower():
                return True
    if isinstance(node, ast.ImportFrom):
        module = str(node.module or "").lower()
        if "repository" in module:
            return True
        for alias in node.names:
            if "repository" in str(alias.name).lower():
                return True
    return False


def _repository_reference_in_lines(lines: list[str]) -> bool:
    source = "\n".join(lines)
    if re.search(r"from\s+.*repository(?:\.[A-Za-z0-9_]+)*\s+import\s+", source):
        return True
    if re.search(r"import\s+.*repository", source):
        return True
    return bool(re.search(r"\brepository\b|[A-Za-z0-9_]+_repository\b", source, re.IGNORECASE))


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return str(func.id)
    if isinstance(func, ast.Attribute):
        return str(func.attr)
    return ""


def _attribute_chain(value: ast.AST) -> str:
    if isinstance(value, ast.Name):
        return str(value.id)
    if isinstance(value, ast.Attribute):
        root = _attribute_chain(value.value)
        if root:
            return f"{root}.{value.attr}"
        return str(value.attr)
    return ""


def _call_chain(node: ast.Call) -> str:
    return _attribute_chain(node.func)


def _call_indicates_write(chain: str, node: ast.Call) -> bool:
    lowered = chain.lower()
    if any(hint in lowered for hint in _WRITE_CALL_HINTS):
        return True
    for arg in list(node.args):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if SQL_WRITE_TOKEN_RE.search(arg.value):
                return True
    return False


def _call_indicates_invalidation(chain: str) -> bool:
    lowered = chain.lower()
    return any(hint in lowered for hint in _INVALIDATE_HINTS)


def _call_indicates_cache(chain: str) -> bool:
    lowered = chain.lower()
    return any(hint in lowered for hint in _CACHE_HINTS)


def _callee_symbol(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return str(node.func.id)
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name) and str(node.func.value.id) in {"self", "cls"}:
            return str(node.func.attr)
        chain = _attribute_chain(node.func)
        if chain:
            return chain
    return ""


class _CacheFactsCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scope: list[str] = []
        self.facts: dict[str, _CacheFnFacts] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._capture(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._capture(node)

    def _capture(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = ".".join(self._scope + [node.name]) if self._scope else node.name
        facts = _CacheFnFacts(name=name, line=int(getattr(node, "lineno", 1) or 1))
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                chain = _call_chain(child)
                if _call_indicates_write(chain, child):
                    facts.write_hits.append(chain or _call_name(child))
                if _call_indicates_invalidation(chain):
                    facts.invalidation_hits.append(chain or _call_name(child))
                if _call_indicates_cache(chain):
                    facts.cache_hits.append(chain or _call_name(child))
                callee = _callee_symbol(child).strip()
                if callee:
                    facts.calls.add(callee)
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if SQL_WRITE_TOKEN_RE.search(child.value):
                    facts.write_hits.append("sql-string")
        self.facts[name] = facts
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()


def _build_call_edges(facts_by_name: dict[str, _CacheFnFacts]) -> dict[str, set[str]]:
    by_short_name: dict[str, set[str]] = {}
    for name in facts_by_name:
        short = name.split(".")[-1]
        by_short_name.setdefault(short, set()).add(name)

    edges: dict[str, set[str]] = {name: set() for name in facts_by_name}
    for name, facts in facts_by_name.items():
        for target in facts.calls:
            if target in facts_by_name:
                edges[name].add(target)
                continue
            short = target.split(".")[-1]
            edges[name].update(by_short_name.get(short, set()))
    return edges


def _can_reach_invalidation(
    start: str,
    *,
    facts_by_name: dict[str, _CacheFnFacts],
    edges: dict[str, set[str]],
) -> bool:
    stack = [start]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        facts = facts_by_name.get(current)
        if facts is None:
            continue
        if facts.invalidation_hits:
            return True
        stack.extend(edges.get(current, set()))
    return False


def check_layer_boundary_ast(changed_files: list[str], project_root: Path) -> list[str]:
    """Heuristic AST check for route->repository direct coupling."""

    violations: list[str] = []
    root = Path(project_root).resolve()
    for relative in changed_files:
        rel = _normalize_path(relative)
        if not rel.endswith(".py") or not _route_layer_file(rel):
            continue
        added_lines = _git_added_lines(root, rel)
        if added_lines is not None:
            if _repository_reference_in_lines(added_lines):
                violations.append(f"{rel}: route layer references repository directly (repository reference detected)")
                continue
            if added_lines or _git_path_tracked(root, rel):
                continue
        file_path = _resolve_file(root, rel)
        source = _read_source(file_path)
        tree = _parse_tree(source)
        if tree is None:
            continue
        file_violations: list[str] = []
        for node in ast.walk(tree):
            if _repository_reference_in_import(node):
                file_violations.append("repository import detected")
                continue
            if isinstance(node, ast.Call):
                call_name = _call_name(node).lower()
                chain = _attribute_chain(node.func).lower()
                if "repository" in call_name or "repository" in chain:
                    file_violations.append("repository call detected")
        if file_violations:
            unique = sorted(set(file_violations))
            violations.append(f"{rel}: route layer references repository directly ({', '.join(unique)})")
    return violations


def _extract_sql_tables(source: str) -> list[str]:
    return [str(item).lower() for item in SQL_WRITE_RE.findall(source or "")]


def _extract_session_models(tree: ast.AST) -> list[str]:
    models: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _attribute_chain(node.func).lower()
        if not any(marker in chain for marker in ("session.add", "session.delete", "session.merge", "session.update")):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name):
            models.append(str(first.id).lower())
        elif isinstance(first, ast.Call):
            call_name = _call_name(first)
            if call_name:
                models.append(call_name.lower())
    return models


def _extract_session_models_from_lines(lines: list[str]) -> list[str]:
    models: list[str] = []
    for line in lines:
        for match in SESSION_WRITE_MODEL_RE.findall(line or ""):
            text = str(match).strip().lower()
            if text:
                models.append(text)
    return models


def check_source_of_truth_conflict_ast(
    changed_files: list[str],
    project_root: Path,
    declared_sot: list[str],
    declared_sot_canonical: list[str] | None = None,
) -> list[str]:
    """Heuristic AST/SQL check for writes outside declared source of truth."""

    violations: list[str] = []
    declared_raw = [str(item).strip() for item in declared_sot if str(item).strip()]
    declared_canonical = [str(item).strip() for item in list(declared_sot_canonical or []) if str(item).strip()] or canonicalize_source_of_truth(declared_raw)
    root = Path(project_root).resolve()
    if not changed_files:
        return violations
    for relative in changed_files:
        rel = _normalize_path(relative)
        if not rel.endswith(".py"):
            continue
        file_path = _resolve_file(root, rel)
        found_targets: set[str] = set()
        added_lines = _git_added_lines(root, rel)
        if added_lines is not None:
            added_source = "\n".join(added_lines)
            for table in _extract_sql_tables(added_source):
                found_targets.add(f"db.{table}")
            for model in _extract_session_models_from_lines(added_lines):
                found_targets.add(f"db.{model}")
        else:
            source = _read_source(file_path)
            tree = _parse_tree(source)
            for table in _extract_sql_tables(source):
                found_targets.add(f"db.{table}")
            if tree is not None:
                for model in _extract_session_models(tree):
                    found_targets.add(f"db.{model}")
        unknown = sorted(
            item
            for item in found_targets
            if not source_of_truth_allows_target(
                target=item,
                declared_raw=declared_raw,
                declared_canonical=declared_canonical,
            )
        )
        if unknown:
            violations.append(f"{rel}: write targets not declared in source_of_truth: {unknown}")
    return violations


def check_cache_consistency_ast(changed_files: list[str], project_root: Path) -> dict[str, Any]:
    """AST association check for write-path and cache invalidation linkage."""

    root = Path(project_root).resolve()
    file_results: list[dict[str, Any]] = []
    fail_files: list[str] = []
    warn_files: list[str] = []
    pass_files: list[str] = []
    evidence: list[dict[str, Any]] = []

    for relative in changed_files:
        rel = _normalize_path(relative)
        if not rel.endswith(".py"):
            continue
        source = _read_source(_resolve_file(root, rel))
        tree = _parse_tree(source)
        if tree is None:
            warn_files.append(rel)
            file_results.append(
                {
                    "file": rel,
                    "status": "WARN",
                    "reason": "parse_error",
                    "unlinked_write_functions": [],
                    "linked_write_functions": [],
                }
            )
            evidence.append(
                {
                    "file": rel,
                    "rule": "cache_consistency.ast.parse_error",
                    "hit": "File could not be parsed; cache consistency analysis degraded.",
                    "confidence": 0.25,
                }
            )
            continue

        collector = _CacheFactsCollector()
        collector.visit(tree)
        facts_by_name = collector.facts
        write_functions = [name for name, item in facts_by_name.items() if item.write_hits]
        if not write_functions:
            pass_files.append(rel)
            file_results.append(
                {
                    "file": rel,
                    "status": "PASS",
                    "reason": "no_write_path",
                    "unlinked_write_functions": [],
                    "linked_write_functions": [],
                }
            )
            continue

        edges = _build_call_edges(facts_by_name)
        linked: list[str] = []
        unlinked: list[str] = []
        for name in write_functions:
            if _can_reach_invalidation(name, facts_by_name=facts_by_name, edges=edges):
                linked.append(name)
            else:
                unlinked.append(name)

        has_invalidation_signals = any(item.invalidation_hits for item in facts_by_name.values())
        has_cache_signals = any(item.cache_hits for item in facts_by_name.values())

        if not unlinked:
            pass_files.append(rel)
            file_results.append(
                {
                    "file": rel,
                    "status": "PASS",
                    "reason": "all_writes_linked",
                    "unlinked_write_functions": [],
                    "linked_write_functions": sorted(linked),
                }
            )
            continue

        if has_invalidation_signals:
            file_status = "FAIL"
            fail_files.append(rel)
            reason = "write_path_without_invalidation_link"
            base_confidence = 0.9
        elif has_cache_signals:
            # This checker receives file-level changes, not changed line/function
            # ranges. Treat generic cache usage without an invalidation hook as
            # a warning because the write path may predate the changed hunk.
            file_status = "WARN"
            warn_files.append(rel)
            reason = "write_path_without_invalidation_link_unattributed"
            base_confidence = 0.65
        else:
            pass_files.append(rel)
            file_results.append(
                {
                    "file": rel,
                    "status": "PASS",
                    "reason": "no_cache_semantics_detected",
                    "unlinked_write_functions": sorted(unlinked),
                    "linked_write_functions": sorted(linked),
                }
            )
            continue

        for fn_name in unlinked:
            line = facts_by_name.get(fn_name).line if fn_name in facts_by_name else None
            evidence_item: dict[str, Any] = {
                "file": rel,
                "rule": "cache_consistency.ast.write_invalidation_link",
                "hit": f"write function '{fn_name}' has no invalidate/expire/evict association",
                "confidence": base_confidence,
            }
            if line is not None:
                evidence_item["line"] = int(line)
            evidence.append(evidence_item)

        file_results.append(
            {
                "file": rel,
                "status": file_status,
                "reason": reason,
                "unlinked_write_functions": sorted(unlinked),
                "linked_write_functions": sorted(linked),
            }
        )

    if fail_files:
        status = "FAIL"
    elif warn_files:
        status = "WARN"
    else:
        status = "PASS"

    details = "All write paths link to cache invalidation hooks."
    if status == "WARN":
        details = "Potential cache consistency risk found, but evidence confidence is medium."
    if status == "FAIL":
        details = "Detected write paths without invalidate/expire/evict linkage."
    return {
        "status": status,
        "mode": "ast_association_v2",
        "details": details,
        "fail_files": sorted(set(fail_files)),
        "warn_files": sorted(set(warn_files)),
        "pass_files": sorted(set(pass_files)),
        "analysis": file_results,
        "evidence": evidence,
    }

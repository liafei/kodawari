"""Module ownership and import rule checks."""

from __future__ import annotations

import ast
from fnmatch import fnmatch
import json
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None


def _read_ownership_payload(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return {}
    stripped = text.strip()
    if not stripped:
        return {}
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        if yaml is None:
            return {}
        try:
            payload = yaml.safe_load(stripped)
        except Exception:
            return {}
    return payload if isinstance(payload, dict) else {}


def find_module_ownership_file(project_root: Path) -> Path | None:
    for name in ("module_ownership.yaml", "module_ownership.yml", "module_ownership.json"):
        candidate = (project_root / name).resolve()
        if candidate.exists():
            return candidate
    return None


def load_module_ownership_modules(
    *,
    project_root: Path,
    ownership_path: Path | None = None,
) -> list[dict[str, Any]]:
    path = ownership_path or find_module_ownership_file(project_root)
    if path is None:
        return []
    payload = _read_ownership_payload(path)
    return _iter_modules(payload)


def _normalize_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _module_import_path(path: str) -> str:
    normalized = _normalize_path(path)
    if normalized.endswith("/__init__.py"):
        normalized = normalized[: -len("/__init__.py")]
    elif normalized.endswith(".py"):
        normalized = normalized[:-3]
    return normalized.replace("/", ".")


def _str_list(items: Any) -> list[str]:
    return [str(item).strip() for item in list(items or []) if str(item).strip()]


def _module_item(module_name: str, module_payload: dict[str, Any], path: str) -> dict[str, Any]:
    return {
        "module": str(module_name).strip(),
        "owner": str(module_payload.get("owner") or "").strip(),
        "path": path,
        "description": str(module_payload.get("description") or "").strip(),
        "public_api": _str_list(module_payload.get("public_api")),
        "forbidden_imports": _str_list(module_payload.get("forbidden_imports")),
        "canonical_for": _str_list(module_payload.get("canonical_for")),
        "import_path": _module_import_path(path),
    }


def _iter_modules(payload: dict[str, Any]) -> list[dict[str, Any]]:
    modules = payload.get("modules")
    if not isinstance(modules, dict):
        return []
    items: list[dict[str, Any]] = []
    for module_name, module_payload in modules.items():
        if not isinstance(module_payload, dict):
            continue
        path = _normalize_path(str(module_payload.get("path") or ""))
        if not path:
            continue
        items.append(_module_item(module_name, module_payload, path))
    return items


def relevant_ownership_context(
    *,
    project_root: Path,
    changed_files: list[str],
    ownership_path: Path | None = None,
) -> list[dict[str, Any]]:
    path = ownership_path or find_module_ownership_file(project_root)
    if path is None:
        return []
    modules = load_module_ownership_modules(project_root=project_root, ownership_path=path)
    selected: list[dict[str, Any]] = []
    for module in modules:
        module_path = str(module.get("path") or "")
        for changed in changed_files:
            normalized_changed = _normalize_path(changed)
            if normalized_changed == module_path:
                if module not in selected:
                    selected.append(module)
                break
    return selected


def _parse_file(path: Path) -> ast.AST | None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _matching_module(import_name: str, modules: list[dict[str, Any]]) -> dict[str, Any] | None:
    for module in modules:
        if str(module.get("import_path") or "") == import_name:
            return module
    return None


def _node_lineno(node: ast.AST) -> int:
    return int(getattr(node, "lineno", 1) or 1)


def _check_forbidden_imports(
    changed: str,
    node: ast.ImportFrom,
    import_name: str,
    owning_module: dict[str, Any] | None,
    violations: list[str],
    evidence: list[dict[str, Any]],
) -> None:
    if owning_module is None:
        return
    for pattern in list(owning_module.get("forbidden_imports") or []):
        if fnmatch(import_name, pattern):
            message = f"{changed}: forbidden import '{import_name}' matched '{pattern}'"
            violations.append(message)
            evidence.append({
                "file": changed,
                "rule": "import_rules.forbidden_import",
                "hit": message,
                "confidence": 0.95,
                "line": _node_lineno(node),
                "metadata": {"pattern": pattern, "import": import_name},
            })


def _check_public_api_imports(
    changed: str,
    node: ast.ImportFrom,
    import_name: str,
    imported_module: dict[str, Any],
    violations: list[str],
    evidence: list[dict[str, Any]],
) -> None:
    public_api = set(str(item) for item in list(imported_module.get("public_api") or []))
    for alias in list(node.names or []):
        imported_name = str(getattr(alias, "name", "") or "").strip()
        if not imported_name or imported_name == "*":
            continue
        if imported_name in public_api:
            continue
        message = (
            f"{changed}: imported non-public symbol '{imported_name}' from "
            f"{import_name}; allowed={sorted(public_api)}"
        )
        violations.append(message)
        evidence.append({
            "file": changed,
            "rule": "import_rules.non_public_api",
            "hit": message,
            "confidence": 0.95,
            "line": _node_lineno(node),
            "metadata": {"import": import_name, "symbol": imported_name},
        })


def _process_import_node(
    changed: str,
    node: ast.ImportFrom,
    owning_module: dict[str, Any] | None,
    modules: list[dict[str, Any]],
    violations: list[str],
    evidence: list[dict[str, Any]],
) -> None:
    import_name = str(node.module or "").strip()
    if not import_name:
        return
    _check_forbidden_imports(changed, node, import_name, owning_module, violations, evidence)
    imported_module = _matching_module(import_name, modules)
    if imported_module is None:
        return
    _check_public_api_imports(changed, node, import_name, imported_module, violations, evidence)


def _process_changed_file(
    changed: str,
    project_root: Path,
    modules: list[dict[str, Any]],
    violations: list[str],
    evidence: list[dict[str, Any]],
) -> None:
    file_path = (project_root / changed).resolve()
    tree = _parse_file(file_path)
    if tree is None:
        return
    owning_module = next((m for m in modules if str(m.get("path") or "") == changed), None)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            _process_import_node(changed, node, owning_module, modules, violations, evidence)


def run_import_rules_checker(
    changed_files: list[str],
    project_root: Path,
    *,
    ownership_path: Path | None = None,
) -> dict[str, Any]:
    path = ownership_path or find_module_ownership_file(project_root)
    if path is None:
        return {
            "status": "PASS",
            "details": "module ownership manifest not configured",
            "violations": [],
            "evidence": [],
        }
    modules = _iter_modules(_read_ownership_payload(path))
    if not modules:
        return {
            "status": "WARN",
            "details": "module ownership manifest is unreadable or empty",
            "violations": [],
            "evidence": [{
                "file": str(path),
                "rule": "import_rules.ownership_unavailable",
                "hit": "module ownership manifest is unreadable or empty",
                "confidence": 0.0,
            }],
        }
    violations: list[str] = []
    evidence: list[dict[str, Any]] = []
    for raw_changed in changed_files:
        _process_changed_file(_normalize_path(raw_changed), project_root, modules, violations, evidence)
    return {
        "status": "FAIL" if violations else "PASS",
        "details": "Import ownership violations detected." if violations else "No import ownership violations detected.",
        "violations": violations,
        "evidence": evidence,
        "ownership_path": str(path),
    }


__all__ = [
    "find_module_ownership_file",
    "load_module_ownership_modules",
    "relevant_ownership_context",
    "run_import_rules_checker",
]

"""Changed-file inference helpers for the local adapter."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from kodawari.autopilot.core.action_semantics import normalize_requested_action


def infer_changed_files(
    context: dict[str, Any],
    *,
    cwd: Path | None,
    default_changed_files: list[str],
) -> list[str]:
    explicit = explicit_changed_files(context)
    if explicit:
        return explicit
    from_scope = changed_files_from_scope_text(context)
    if from_scope:
        return from_scope
    if is_fix_action(context):
        return ["src/app.py", "tests/test_app.py"]
    hinted = changed_files_from_hints(context)
    if hinted:
        return hinted
    workspace_default = workspace_default_changed_files(cwd)
    if workspace_default:
        return workspace_default
    return list(default_changed_files)


def explicit_changed_files(context: dict[str, Any]) -> list[str]:
    explicit = context.get("simulate_changed_files")
    if isinstance(explicit, list) and explicit:
        return [str(item) for item in explicit]
    return []


def is_fix_action(context: dict[str, Any]) -> bool:
    return normalize_requested_action(context.get("requested_action")) == "fix_round"


def changed_files_from_hints(context: dict[str, Any]) -> list[str]:
    hints = {
        str(item.get("pattern_id", ""))
        for item in context.get("pattern_hints", [])
        if isinstance(item, dict)
    }
    mapping = {
        "ranking-rules": ["src/ranking_rules.py", "tests/test_ranking_rules.py"],
        "schema-migration": ["src/migrations/001_add_field.sql", "tests/test_schema_migration.py"],
        "api-endpoint": ["src/api/endpoint.py", "tests/test_api_endpoint.py"],
        "crud": ["src/crud/service.py", "tests/test_crud_service.py"],
    }
    for pattern_id, files in mapping.items():
        if pattern_id in hints:
            return list(files)
    return []


def changed_files_from_scope_text(context: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for key in ("task_scope", "scope", "requirements"):
        raw = str(context.get(key) or "")
        if not raw:
            continue
        tokens.extend(extract_path_tokens(raw))
    if not tokens:
        return []
    deduped: list[str] = []
    for token in tokens:
        if token not in deduped:
            deduped.append(token)
    return deduped[:8]


def extract_path_tokens(text: str) -> list[str]:
    pattern = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+")
    candidates: list[str] = []
    for match in pattern.findall(str(text or "")):
        normalized = str(match).strip().strip(",;:'\"()[]{}").replace("\\", "/")
        if not normalized:
            continue
        if "/" not in normalized and "." not in normalized:
            continue
        if normalized.startswith(("http://", "https://")):
            continue
        if normalized not in candidates:
            candidates.append(normalized)
    return candidates


def workspace_default_changed_files(cwd: Path | None) -> list[str]:
    root = Path(cwd or Path.cwd()).resolve()
    candidates = [
        ["app/main.py", "tests/test_api.py"],
        ["src/app.py", "tests/test_app.py"],
    ]
    for source, test in candidates:
        source_path = (root / source).resolve()
        if source_path.exists():
            test_path = (root / test).resolve()
            if test_path.exists():
                return [source, test]
            return [source]
    return []

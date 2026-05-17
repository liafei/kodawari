"""Changed-files reconciliation helpers for autopilot command runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def normalize_changed_path(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def dedupe_changed_files(values: list[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = normalize_changed_path(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def existing_changed_files(*, project_root: Path, values: list[str]) -> list[str]:
    existing: list[str] = []
    for item in values:
        candidate = (project_root / item).resolve()
        if candidate.exists():
            existing.append(item)
    return existing


def subtask_changed_files(state: Any) -> list[str]:
    subtasks = getattr(state, "subtasks", {}) or {}
    values: list[Any] = []
    if isinstance(subtasks, dict):
        for subtask in subtasks.values():
            changed = getattr(subtask, "changed_files", []) if subtask is not None else []
            if isinstance(changed, list):
                values.extend(changed)
    return dedupe_changed_files(values)


def resolve_reliable_changed_files(
    *,
    project_root: Path,
    state: Any,
    run_result: dict[str, Any],
) -> tuple[list[str], str]:
    task_delta_changed = dedupe_changed_files(list(run_result.get("task_delta_changed_files") or []))
    execution_changed = dedupe_changed_files(list(dict(run_result.get("execution_result") or {}).get("changed_files") or []))
    runtime_changed = dedupe_changed_files(list(run_result.get("changed_files") or []))
    state_changed = dedupe_changed_files(list(getattr(state, "changed_files", []) or []))
    subtask_changed = subtask_changed_files(state)

    for source, values in (
        ("task_delta_changed_files", task_delta_changed),
        ("execution_result_changed_files", execution_changed),
        ("runtime_changed_files", runtime_changed),
        ("state_changed_files", state_changed),
        ("subtask_changed_files", subtask_changed),
    ):
        existing = existing_changed_files(project_root=project_root, values=values)
        if existing:
            return existing, f"{source}:existing"

    for source, values in (
        ("task_delta_changed_files", task_delta_changed),
        ("execution_result_changed_files", execution_changed),
        ("runtime_changed_files", runtime_changed),
        ("state_changed_files", state_changed),
        ("subtask_changed_files", subtask_changed),
    ):
        if values:
            return values, f"{source}:raw"

    return [], "none"

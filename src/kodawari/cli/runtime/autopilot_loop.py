"""Loop persistence helpers for the autopilot command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.cli.evidence.changed_files_truth import capture_worktree_baseline
from kodawari.cli.io_atomic import append_jsonl_atomic


def append_round_records(rounds_path: Path, rounds: list[dict[str, Any]]) -> None:
    if not rounds:
        return
    for record in rounds:
        append_jsonl_atomic(rounds_path, record)


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


def capture_autopilot_worktree_preflight(*, project_root: Path, planning_dir: Path, feature: str) -> dict[str, Any]:
    try:
        return capture_worktree_baseline(
            project_root=project_root,
            planning_dir=planning_dir,
            feature=feature,
            command="autopilot",
            mode="warn",
            allowed_files=[],
        )
    except ValueError as exc:
        return {
            "schema_version": "",
            "captured_at": "",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "command": "autopilot",
            "mode": "warn",
            "status": "WARN",
            "dirty_files": [],
            "tracked_dirty_files": [],
            "untracked_files": [],
            "allowed_files": [],
            "core_dirty_files": [],
            "details": f"worktree baseline unavailable: {exc}",
        }


def persist_command_runtime(
    *,
    rounds_path: Path,
    run_result: dict[str, Any],
    task_cycle_rounds: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rounds = list(run_result.get("rounds", [])) + list(task_cycle_rounds)
    append_round_records(rounds_path, rounds)
    return rounds


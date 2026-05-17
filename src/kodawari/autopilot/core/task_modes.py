"""Task mode helpers shared by planning and execution.

These helpers intentionally key off explicit planner/user declarations only.
Read-path work can still require code changes, so a plain ``path_type=read``
must never become an implicit no-op.
"""

from __future__ import annotations

from typing import Any


_NO_WRITE_KEYS = {
    "verification_only_noop",
    "verification_only",
    "executor_must_not_edit",
    "no_code_changes",
    "no_code_edits",
    "no_changes_required",
    "read_only_validation",
    "validation_only",
    "no_write",
    "no_writes",
}
_NO_WRITE_MODES = {
    "verification_only",
    "verification_only_noop",
    "read_only_validation",
    "noop_verify",
    "verify_only",
}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    return text in {"1", "true", "yes", "on"}


def _dicts(*items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            out.append(item)
    return out


def _containers(plan_or_request: dict[str, Any], task: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    task_card = plan_or_request.get("task_card")
    containers = _dicts(plan_or_request, plan_or_request.get("execution_constraints"), task)
    if isinstance(task, dict):
        containers.extend(_dicts(task.get("execution_constraints")))
    if isinstance(task_card, dict):
        containers.extend(_dicts(task_card, task_card.get("execution_constraints")))
    return containers


def is_verification_only_task(
    plan_or_request: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> bool:
    """Return True when a task explicitly declares no-code verification mode."""

    for container in _containers(plan_or_request, task):
        if any(_truthy(container.get(key)) for key in _NO_WRITE_KEYS):
            return True
        mode = _clean_text(container.get("execution_mode") or container.get("mode")).lower()
        if mode in _NO_WRITE_MODES:
            return True
    return False


def _verify_command(plan_or_request: dict[str, Any], task: dict[str, Any] | None = None) -> str:
    for container in _containers(plan_or_request, task):
        command = _clean_text(container.get("verify_cmd") or container.get("test_plan"))
        if command:
            return command
    for recipe in list(plan_or_request.get("verify_recipes") or []):
        if not isinstance(recipe, dict):
            continue
        command = _clean_text(recipe.get("command"))
        if command:
            return command
    return ""


def verification_only_allows_empty_files(
    plan_or_request: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> bool:
    """True when an explicit verification-only task has an executable check."""

    return is_verification_only_task(plan_or_request, task) and bool(_verify_command(plan_or_request, task))


__all__ = [
    "is_verification_only_task",
    "verification_only_allows_empty_files",
]

"""Outcome helpers for workflow-chain runtime."""

from __future__ import annotations

from typing import Any

from kodawari.cli.delivery.workflow_chain_review import task_cycle_blocking_reason


def status_from_bool(passed: bool) -> str:
    return "PASS" if bool(passed) else "BLOCKED"


def task_entry_passed(
    *,
    autopilot_passed: bool,
    approvals: dict[str, Any],
    blocked: bool,
) -> bool:
    return bool(autopilot_passed and bool(approvals.get("all_passed")) and not blocked)


def final_review_phase_status(status: str) -> str:
    if str(status).upper() == "PASS":
        return "passed"
    return "blocked"


def final_blocked_outcome(
    *,
    peer_review_enabled: bool,
    upstream: dict[str, Any],
    task_cycle: dict[str, Any],
    final_review: dict[str, Any],
) -> dict[str, Any] | None:
    if not bool(upstream.get("passed")):
        return blocked_outcome(
            peer_review_enabled=peer_review_enabled,
            task_cycle=task_cycle,
            reason="UPSTREAM_BLOCKED",
            blocking_reason=str(final_review.get("blocking_reason") or "Upstream stage did not pass"),
        )
    if bool(task_cycle.get("blocked")):
        return blocked_outcome(
            peer_review_enabled=peer_review_enabled,
            task_cycle=task_cycle,
            reason="TASK_BLOCKED",
            blocking_reason=str(final_review.get("blocking_reason") or task_cycle_blocking_reason(task_cycle)),
        )
    return None


def task_cycle_empty(task_cycle: dict[str, Any]) -> bool:
    return int(task_cycle.get("tasks_total", 0) or 0) <= 0


def blocked_outcome(
    *,
    peer_review_enabled: bool,
    task_cycle: dict[str, Any],
    reason: str,
    blocking_reason: str,
) -> dict[str, Any]:
    return _outcome(
        status="BLOCKED",
        reason=reason,
        peer_review_enabled=peer_review_enabled,
        task_cycle=task_cycle,
        blocking_reason=blocking_reason,
    )


def pass_outcome(
    *,
    peer_review_enabled: bool,
    task_cycle: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return _outcome(
        status="PASS",
        reason=reason,
        peer_review_enabled=peer_review_enabled,
        task_cycle=task_cycle,
        blocking_reason="",
    )


def _outcome(
    *,
    status: str,
    reason: str,
    peer_review_enabled: bool,
    task_cycle: dict[str, Any],
    blocking_reason: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "blocking_reason": str(blocking_reason or ""),
        "peer_review_enabled": bool(peer_review_enabled),
        "task_cycle_entered": bool(task_cycle.get("entered")),
        "tasks_completed": int(task_cycle.get("tasks_completed", 0) or 0),
        "tasks_total": int(task_cycle.get("tasks_total", 0) or 0),
    }


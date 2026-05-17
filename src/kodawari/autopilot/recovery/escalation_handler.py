"""Executor recovery escalation to Planner redesign.

When executor recovery is exhausted on a gate-complexity failure,
this module handles the escalation request and response for passing
the problem up to the Planner for redesign options.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NamedTuple

from kodawari.autopilot.recovery.failure_event import FailureEvent


REDESIGN_REQUEST = ".executor_redesign_request.json"
REDESIGN_RESPONSE = ".executor_redesign_response.json"
REDESIGN_CONTEXT = ".executor_redesign_context.json"


class DesignChoice(NamedTuple):
    """User's choice from the redesign dialog."""
    action: str  # "skip", "accept", "custom"
    option_index: int | None = None  # for "accept", which option
    custom_text: str = ""  # for "custom", user's custom description


def escalation_count_from_context(planning_dir: Path | str) -> int:
    """Read escalation_count from .executor_redesign_context.json.

    Returns 0 if the file does not exist.
    """
    planning_dir = Path(planning_dir)
    ctx_file = planning_dir / REDESIGN_CONTEXT
    if not ctx_file.exists():
        return 0
    try:
        data = json.loads(ctx_file.read_text())
        return int(data.get("escalation_count", 0))
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0


def write_redesign_request(
    planning_dir: Path | str,
    failure_event: FailureEvent,
    completed_task_ids: list[str],
    active_task_id: str,
) -> None:
    """Write escalation request and update context with incremented count.

    Writes two files:
    - .executor_redesign_context.json: escalation_count (persisted across restarts)
    - .executor_redesign_request.json: request payload for the Planner
    """
    planning_dir = Path(planning_dir)
    planning_dir.mkdir(parents=True, exist_ok=True)

    # Increment and persist escalation count
    count = escalation_count_from_context(planning_dir) + 1
    ctx_data = {"escalation_count": count}
    (planning_dir / REDESIGN_CONTEXT).write_text(json.dumps(ctx_data, indent=2))

    # Write redesign request for the CLI handler
    request_data = {
        "schema_version": "execution.redesign_request.v1",
        "task_id": active_task_id,
        "failure_summary": failure_event.evidence or failure_event.error_code,
        "detector_hint": failure_event.detector_hint,
        "completed_task_ids": completed_task_ids,
        "escalation_count": count,
    }
    (planning_dir / REDESIGN_REQUEST).write_text(json.dumps(request_data, indent=2))


def read_redesign_response(planning_dir: Path | str) -> DesignChoice | None:
    """Read the user's redesign decision from .executor_redesign_response.json.

    Returns None if the file does not exist or is malformed.
    """
    planning_dir = Path(planning_dir)
    response_file = planning_dir / REDESIGN_RESPONSE
    if not response_file.exists():
        return None
    try:
        data = json.loads(response_file.read_text())
        action = str(data.get("action", "")).strip().lower()
        if action == "skip":
            return DesignChoice(action="skip")
        elif action == "accept":
            return DesignChoice(action="accept", option_index=int(data.get("option_index", 0)))
        elif action == "custom":
            return DesignChoice(action="custom", custom_text=str(data.get("description", "")))
        else:
            return None
    except (json.JSONDecodeError, ValueError, TypeError):
        return None


def is_gate_complexity_exhausted(failure_event: FailureEvent) -> bool:
    """Check if this failure is a gate-complexity violation."""
    return (
        failure_event.error_code == "GATE_BLOCKED"
        and failure_event.detector_hint == "gate_complexity"
    )


__all__ = [
    "REDESIGN_REQUEST",
    "REDESIGN_RESPONSE",
    "REDESIGN_CONTEXT",
    "DesignChoice",
    "escalation_count_from_context",
    "write_redesign_request",
    "read_redesign_response",
    "is_gate_complexity_exhausted",
]
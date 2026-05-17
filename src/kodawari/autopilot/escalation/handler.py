"""Unified escalation handler: decision file write/read + counters.

This is the central entry point that recovery/planning/gate code calls
when a failure is escalatable. It:

1. Classifies the failure into an EscalationKind (via kinds.classify)
2. Writes ``.{phase}_decision_request.json`` atomically + under file lock
3. Increments per-phase ``.{phase}_decision_context.json`` count
4. On second call when count is already at max, returns ``False`` to
   indicate the caller should fall through to final BLOCKED.

The user runs ``kodawari decide`` which:
- Reads the request, classifies kind, picks the right Planner prompt
- Surfaces options (GUI or CLI)
- Writes ``.{phase}_decision_response.json``

Autopilot resume reads the response and applies the user's choice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.autopilot.escalation.kinds import EscalationKind, classify, is_escalatable
from kodawari.infra.io_atomic import atomic_write_json, path_lock


_MAX_ESCALATIONS_PER_PHASE = 2


# --- File naming convention -----------------------------------------------

def request_filename(phase: str) -> str:
    return f".{phase.lower()}_decision_request.json"


def response_filename(phase: str) -> str:
    return f".{phase.lower()}_decision_response.json"


def context_filename(phase: str) -> str:
    return f".{phase.lower()}_decision_context.json"


# --- Data classes ---------------------------------------------------------

@dataclass
class DecisionRequest:
    schema_version: str = "workflow.decision_request.v1"
    escalation_kind: str = ""
    failure_code: str = ""
    phase: str = ""
    feature: str = ""
    task_id: str = ""
    failure_summary: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    completed_task_ids: list[str] = field(default_factory=list)
    escalation_count: int = 0
    max_escalations: int = _MAX_ESCALATIONS_PER_PHASE
    max_split_depth: int = 2
    current_split_depth: int = 0
    issued_at: str = ""
    consumed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "escalation_kind": self.escalation_kind,
            "failure_code": self.failure_code,
            "phase": self.phase,
            "feature": self.feature,
            "task_id": self.task_id,
            "failure_summary": self.failure_summary,
            "context": dict(self.context),
            "completed_task_ids": list(self.completed_task_ids),
            "escalation_count": int(self.escalation_count),
            "max_escalations": int(self.max_escalations),
            "max_split_depth": int(self.max_split_depth),
            "current_split_depth": int(self.current_split_depth),
            "issued_at": self.issued_at,
            "consumed_at": self.consumed_at,
        }


@dataclass
class DecisionResponse:
    schema_version: str = "workflow.decision_response.v1"
    phase: str = ""
    escalation_kind: str = ""
    action: str = "skip"  # accept / skip / custom / abort
    option_index: int | None = None
    option: dict[str, Any] | None = None
    description: str = ""
    consumed_at: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DecisionResponse:
        return cls(
            schema_version=str(data.get("schema_version") or "workflow.decision_response.v1"),
            phase=str(data.get("phase") or ""),
            escalation_kind=str(data.get("escalation_kind") or ""),
            action=str(data.get("action") or "skip"),
            option_index=data.get("option_index"),
            option=data.get("option"),
            description=str(data.get("description") or ""),
            consumed_at=data.get("consumed_at"),
        )


# --- Counter persistence --------------------------------------------------

def escalation_count(planning_dir: Path | str, phase: str) -> int:
    """Read current ``escalation_count`` for a phase."""
    path = Path(planning_dir) / context_filename(phase)
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("escalation_count", 0))
    except (json.JSONDecodeError, ValueError, OSError):
        return 0


def _bump_count(planning_dir: Path, phase: str) -> int:
    """Atomic increment of the per-phase counter."""
    path = planning_dir / context_filename(phase)
    with path_lock(path):
        count = 0
        if path.exists():
            try:
                count = int(json.loads(path.read_text(encoding="utf-8")).get("escalation_count", 0))
            except (json.JSONDecodeError, ValueError, OSError):
                count = 0
        count += 1
        atomic_write_json(path, {"escalation_count": count, "phase": phase}, use_lock=False)
        return count


# --- Public entry: maybe_escalate ----------------------------------------

def maybe_escalate(
    *,
    planning_dir: Path | str,
    phase: str,
    failure_event: Any = None,
    planning_diagnostics: dict[str, Any] | None = None,
    gate_check: dict[str, Any] | None = None,
    feature: str = "",
    task_id: str = "",
    failure_summary: str = "",
    completed_task_ids: list[str] | None = None,
    extra_context: dict[str, Any] | None = None,
) -> tuple[bool, EscalationKind | None]:
    """Classify failure and (if escalatable) write the decision request.

    Returns:
        ``(True, kind)`` — request file written, caller should pause/finish
            current loop and wait for user to run ``kodawari decide``.
        ``(False, None)`` — failure is not escalatable, caller should fall
            through to its legacy BLOCKED path.
        ``(False, kind)`` — escalatable kind detected but ``max_escalations``
            already exhausted; caller should also fall through to BLOCKED.
    """
    kind = classify(
        failure_event=failure_event,
        planning_diagnostics=planning_diagnostics,
        gate_check=gate_check,
        phase=phase,
    )
    if not is_escalatable(kind):
        return False, None

    planning_dir = Path(planning_dir)
    planning_dir.mkdir(parents=True, exist_ok=True)

    # Determine count *before* writing — if at max, do not escalate further
    current = escalation_count(planning_dir, phase)
    if current >= _MAX_ESCALATIONS_PER_PHASE:
        return False, kind

    # Bump count and build request
    new_count = _bump_count(planning_dir, phase)

    # Pull failure_code from whichever input was given
    failure_code = ""
    if failure_event is not None:
        failure_code = str(getattr(failure_event, "error_code", "") or "")
    elif planning_diagnostics is not None:
        failure_code = str(planning_diagnostics.get("run_reason") or "").strip()
    elif gate_check is not None:
        failure_code = "GATE_BLOCKED"

    if not failure_summary:
        if failure_event is not None:
            failure_summary = str(getattr(failure_event, "evidence", "") or "") or failure_code
        elif planning_diagnostics is not None:
            failure_summary = str(planning_diagnostics.get("blocking_reason") or "") or failure_code

    request = DecisionRequest(
        escalation_kind=kind.value,
        failure_code=failure_code,
        phase=phase,
        feature=feature,
        task_id=task_id,
        failure_summary=failure_summary,
        context=dict(extra_context or {}),
        completed_task_ids=list(completed_task_ids or []),
        escalation_count=new_count,
        issued_at=datetime.now(timezone.utc).isoformat(),
    )
    req_path = planning_dir / request_filename(phase)
    with path_lock(req_path):
        atomic_write_json(req_path, request.to_dict(), use_lock=False)
    # Legacy compat: gate_complexity also writes the v1 redesign request
    # so existing tests/UIs that read .executor_redesign_request.json still see it
    if phase == "executor" and kind == EscalationKind.GATE_REFACTOR_NEEDED:
        _write_legacy_redesign_request(planning_dir, request)

    return True, kind


def _write_legacy_redesign_request(planning_dir: Path, req: DecisionRequest) -> None:
    """Mirror the new request to the legacy ``.executor_redesign_request.json``
    filename so existing consumers (tests, UIs, audit scripts) keep working.
    """
    legacy_path = planning_dir / ".executor_redesign_request.json"
    legacy_payload = {
        "schema_version": "execution.redesign_request.v1",
        "task_id": req.task_id,
        "failure_summary": req.failure_summary,
        "detector_hint": "gate_complexity",
        "completed_task_ids": list(req.completed_task_ids),
        "escalation_count": req.escalation_count,
    }
    try:
        atomic_write_json(legacy_path, legacy_payload)
    except OSError:
        pass


# --- Read response --------------------------------------------------------

def read_decision_response(
    planning_dir: Path | str, phase: str
) -> DecisionResponse | None:
    """Read the user's decision response for ``phase``.

    Returns ``None`` if the file does not exist or is malformed.
    """
    path = Path(planning_dir) / response_filename(phase)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return DecisionResponse.from_dict(data)


def write_decision_response(
    planning_dir: Path | str, phase: str, response: DecisionResponse
) -> None:
    """Write the user's decision response for ``phase`` atomically."""
    planning_dir = Path(planning_dir)
    planning_dir.mkdir(parents=True, exist_ok=True)
    if not response.consumed_at:
        response.consumed_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": response.schema_version,
        "phase": response.phase or phase,
        "escalation_kind": response.escalation_kind,
        "action": response.action,
        "option_index": response.option_index,
        "option": response.option,
        "description": response.description,
        "consumed_at": response.consumed_at,
    }
    target = planning_dir / response_filename(phase)
    with path_lock(target):
        atomic_write_json(target, payload, use_lock=False)


def find_pending_request(planning_dir: Path | str) -> tuple[str, DecisionRequest] | None:
    """Find the next pending decision request in priority order.

    Priority: planning > executor > gate. Returns ``(phase, request)`` or
    ``None`` if no requests are pending.
    """
    planning_dir = Path(planning_dir)
    for phase in ("planning", "executor", "gate"):
        req_path = planning_dir / request_filename(phase)
        if not req_path.exists():
            continue
        try:
            data = json.loads(req_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("consumed_at"):
            continue
        req = DecisionRequest(
            schema_version=str(data.get("schema_version") or "workflow.decision_request.v1"),
            escalation_kind=str(data.get("escalation_kind") or ""),
            failure_code=str(data.get("failure_code") or ""),
            phase=str(data.get("phase") or phase),
            feature=str(data.get("feature") or ""),
            task_id=str(data.get("task_id") or ""),
            failure_summary=str(data.get("failure_summary") or ""),
            context=dict(data.get("context") or {}),
            completed_task_ids=list(data.get("completed_task_ids") or []),
            escalation_count=int(data.get("escalation_count") or 0),
            max_escalations=int(data.get("max_escalations") or _MAX_ESCALATIONS_PER_PHASE),
            max_split_depth=int(data.get("max_split_depth") or 2),
            current_split_depth=int(data.get("current_split_depth") or 0),
            issued_at=str(data.get("issued_at") or ""),
            consumed_at=data.get("consumed_at"),
        )
        return phase, req
    return None


__all__ = [
    "DecisionRequest",
    "DecisionResponse",
    "context_filename",
    "escalation_count",
    "find_pending_request",
    "maybe_escalate",
    "read_decision_response",
    "request_filename",
    "response_filename",
    "write_decision_response",
]

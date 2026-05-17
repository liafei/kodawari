"""Decision artifact helpers for autopilot pause and resume semantics."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from kodawari.cli.io_atomic import atomic_write_json, load_json_dict


DECISION_REQUEST_FILENAME = ".decision_request.json"
DECISION_RESPONSE_FILENAME = ".decision_response.json"
DECISION_HISTORY_FILENAME = ".decision_history.json"
DECISION_REQUEST_SCHEMA_VERSION = "autopilot.decision_request.v1"
DECISION_RESPONSE_SCHEMA_VERSION = "autopilot.decision_response.v1"
DECISION_HISTORY_SCHEMA_VERSION = "autopilot.decision_history.v1"


class DecisionKind(str, Enum):
    INTENT_CLARIFICATION = "intent_clarification"
    ARCHITECTURE_FREEZE = "architecture_freeze"
    TASK_PLAN_FREEZE = "task_plan_freeze"
    PLANNING_APPROVAL = "planning_approval"
    PLANNING_ESCALATION = "planning_escalation"
    RELEASE_APPROVAL = "release_approval"
    VERIFICATION_PLAN_GAP = "verification_plan_gap"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_clean_text(item) for item in values if _clean_text(item)]


def _option_list(values: Any) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for item in list(values or []):
        if not isinstance(item, dict):
            continue
        option_id = _clean_text(item.get("option_id") or item.get("id"))
        label = _clean_text(item.get("label"))
        if not option_id or not label:
            continue
        options.append(
            {
                "option_id": option_id,
                "label": label,
                "details": _clean_text(item.get("details")),
            }
        )
    return options


def build_decision_request(
    *,
    decision_id: str,
    decision_kind: str | DecisionKind,
    question: str,
    context_summary: str,
    options: list[dict[str, Any]] | None = None,
    recommended_option: str = "",
    blocking_reason: str = "",
    generated_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": DECISION_REQUEST_SCHEMA_VERSION,
        "decision_id": _clean_text(decision_id),
        "decision_kind": _clean_text(getattr(decision_kind, "value", decision_kind)),
        "question": _clean_text(question),
        "context_summary": _clean_text(context_summary),
        "options": _option_list(options),
        "recommended_option": _clean_text(recommended_option),
        "blocking_reason": _clean_text(blocking_reason),
        "generated_at": _clean_text(generated_at) or _utc_now_iso(),
    }


def build_decision_response(
    *,
    decision_id: str,
    selected_option: str,
    rationale: str = "",
    responded_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": DECISION_RESPONSE_SCHEMA_VERSION,
        "decision_id": _clean_text(decision_id),
        "selected_option": _clean_text(selected_option),
        "rationale": _clean_text(rationale),
        "responded_at": _clean_text(responded_at) or _utc_now_iso(),
    }


def _artifact_path(planning_dir: Path, filename: str) -> Path:
    return planning_dir.resolve() / filename


def write_decision_request(planning_dir: Path, payload: dict[str, Any]) -> Path:
    path = _artifact_path(planning_dir, DECISION_REQUEST_FILENAME)
    atomic_write_json(path, dict(payload))
    return path


def write_decision_response(planning_dir: Path, payload: dict[str, Any]) -> Path:
    path = _artifact_path(planning_dir, DECISION_RESPONSE_FILENAME)
    atomic_write_json(path, dict(payload))
    return path


def load_decision_history(planning_dir: Path) -> list[str]:
    payload = load_json_dict(_artifact_path(planning_dir, DECISION_HISTORY_FILENAME), required=False) or {}
    return _string_list(payload.get("approved_decision_ids"))


def decision_already_approved(planning_dir: Path, decision_id: str) -> bool:
    normalized = _clean_text(decision_id)
    return bool(normalized and normalized in load_decision_history(planning_dir))


def record_approved_decision(planning_dir: Path, decision_id: str) -> Path | None:
    normalized = _clean_text(decision_id)
    if not normalized:
        return None
    approved = load_decision_history(planning_dir)
    if normalized in approved:
        return _artifact_path(planning_dir, DECISION_HISTORY_FILENAME)
    approved.append(normalized)
    path = _artifact_path(planning_dir, DECISION_HISTORY_FILENAME)
    atomic_write_json(
        path,
        {
            "schema_version": DECISION_HISTORY_SCHEMA_VERSION,
            "approved_decision_ids": approved,
        },
    )
    return path


def clear_consumed_decision_artifacts(planning_dir: Path) -> None:
    """Remove .decision_request.json and .decision_response.json once consumed.

    Called after a decision is approved so stale files do not block re-runs.
    Best-effort: OSError is silently ignored (file already removed or read-only).
    """
    for filename in (DECISION_REQUEST_FILENAME, DECISION_RESPONSE_FILENAME):
        target = _artifact_path(planning_dir, filename)
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass


def load_decision_request(planning_dir: Path) -> dict[str, Any] | None:
    return load_json_dict(_artifact_path(planning_dir, DECISION_REQUEST_FILENAME), required=False)


def load_decision_response(planning_dir: Path) -> dict[str, Any] | None:
    return load_json_dict(_artifact_path(planning_dir, DECISION_RESPONSE_FILENAME), required=False)


def response_matches_request(
    request_payload: dict[str, Any] | None,
    response_payload: dict[str, Any] | None,
) -> bool:
    request_id = _clean_text((request_payload or {}).get("decision_id"))
    response_id = _clean_text((response_payload or {}).get("decision_id"))
    return bool(request_id and response_id and request_id == response_id)


def valid_option_ids(request_payload: dict[str, Any]) -> list[str]:
    return _string_list(
        [item.get("option_id") for item in list(request_payload.get("options") or []) if isinstance(item, dict)]
    )


def decision_pending(planning_dir: Path) -> bool:
    request_payload = load_decision_request(planning_dir)
    if not isinstance(request_payload, dict):
        return False
    response_payload = load_decision_response(planning_dir)
    return not response_matches_request(request_payload, response_payload)


def decision_runtime_snapshot(planning_dir: Path) -> dict[str, Any]:
    request_payload = load_decision_request(planning_dir) or {}
    response_payload = load_decision_response(planning_dir) or {}
    pending = decision_pending(planning_dir)
    return {
        "decision_id": _clean_text(request_payload.get("decision_id")),
        "decision_kind": _clean_text(request_payload.get("decision_kind")),
        "decision_request_present": pending,
        "decision_response_present": bool(response_payload),
        "decision_pending": pending,
        "request_options": _string_list(
            [item.get("option_id") for item in list(request_payload.get("options") or []) if isinstance(item, dict)]
        ),
    }


__all__ = [
    "DECISION_REQUEST_FILENAME",
    "DECISION_REQUEST_SCHEMA_VERSION",
    "DECISION_RESPONSE_FILENAME",
    "DECISION_RESPONSE_SCHEMA_VERSION",
    "DECISION_HISTORY_FILENAME",
    "DECISION_HISTORY_SCHEMA_VERSION",
    "DecisionKind",
    "build_decision_request",
    "build_decision_response",
    "clear_consumed_decision_artifacts",
    "decision_already_approved",
    "decision_pending",
    "decision_runtime_snapshot",
    "load_decision_history",
    "load_decision_request",
    "load_decision_response",
    "record_approved_decision",
    "response_matches_request",
    "valid_option_ids",
    "write_decision_request",
    "write_decision_response",
]

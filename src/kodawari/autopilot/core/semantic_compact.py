"""Semantic compact artifact generation for P0 upgrades."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import logging

from kodawari.autopilot.core.model_advisor import compress_compact_fields as _model_compress
from kodawari.autopilot.core.model_advisor import (
    COMPACT_MUST_FIX_THRESHOLD,
    COMPACT_ERRORS_THRESHOLD,
)
from kodawari.autopilot.core.runtime_budget import build_token_budget_snapshot

logger = logging.getLogger(__name__)


SEMANTIC_COMPACT_VERSION = "semantic_compact.v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for raw in values:
        text = _clean_text(raw)
        if text:
            normalized.append(text)
    return normalized


def _decision_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        source = dict(item)
    elif hasattr(item, "to_dict"):
        payload = item.to_dict()
        source = dict(payload) if isinstance(payload, dict) else {}
    else:
        source = {
            "id": _clean_text(getattr(item, "decision_id", "")),
            "decision": _clean_text(getattr(item, "decision", "")),
            "rationale": _clean_text(getattr(item, "rationale", "")),
            "constraints": list(getattr(item, "constraints", [])),
        }
    return {
        "id": _clean_text(source.get("id") or source.get("decision_id")),
        "decision": _clean_text(source.get("decision")),
        "rationale": _clean_text(source.get("rationale")),
        "constraints": _string_list(source.get("constraints")),
    }


def _state_value(state: Any, key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _state_unified_status(state: Any) -> dict[str, Any]:
    if isinstance(state, dict):
        payload = state.get("unified_status")
        return dict(payload) if isinstance(payload, dict) else {}
    getter = getattr(state, "get_unified_status", None)
    if callable(getter):
        payload = getter()
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _review_feedback_value(context: Any, key: str, default: Any = None) -> Any:
    review = getattr(context, "review_feedback", None)
    if review is None:
        return default
    return getattr(review, key, default)


def _recent_error_payload(state: Any, *, limit: int = 5) -> list[dict[str, Any]]:
    raw_events = _state_value(state, "error_events", [])
    events = list(raw_events) if isinstance(raw_events, list) else []
    recent: list[dict[str, Any]] = []
    for event in events[-max(0, int(limit)) :]:
        if isinstance(event, dict):
            source = dict(event)
        elif hasattr(event, "to_dict"):
            payload = event.to_dict()
            source = dict(payload) if isinstance(payload, dict) else {}
        else:
            source = {
                "timestamp": _clean_text(getattr(event, "timestamp", "")),
                "phase": _clean_text(getattr(event, "phase", "")),
                "action": _clean_text(getattr(event, "action", "")),
                "category": _clean_text(getattr(event, "category", "")),
                "message": _clean_text(getattr(event, "message", "")),
            }
        recent.append(
            {
                "timestamp": _clean_text(source.get("timestamp")),
                "phase": _clean_text(source.get("phase")),
                "action": _clean_text(source.get("action")),
                "category": _clean_text(source.get("category"), default="runtime"),
                "message": _clean_text(source.get("message")),
            }
        )
    if recent:
        return recent
    fallback = _string_list(_state_value(state, "error_history", []))
    return [{"timestamp": "", "phase": "RUNTIME", "action": "", "category": "runtime", "message": item} for item in fallback[-limit:]]


def _token_budget_snapshot(tokens_used: Any, token_budget: Any) -> dict[str, Any]:
    return build_token_budget_snapshot(tokens_used, token_budget)


def _goal_payload(*, feature: str, task_label: str, task_scope: str) -> str:
    if task_scope:
        return task_scope
    if task_label:
        return task_label
    return f"Implement feature {feature}"


def _open_questions(context: Any) -> list[str]:
    return _string_list(_review_feedback_value(context, "should_fix", []))


def _must_fix(context: Any) -> list[str]:
    return _string_list(_review_feedback_value(context, "must_fix", []))


def _constraints(decisions: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for decision in decisions:
        values.extend(_string_list(decision.get("constraints")))
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def _verify_targets(verify_check: dict[str, Any] | None) -> list[str]:
    if not isinstance(verify_check, dict):
        return []
    return _string_list(verify_check.get("verify_targets"))


def _loop_outcome_payload(
    *,
    reason: str,
    unified_status: dict[str, Any],
) -> dict[str, Any]:
    return {
        "reason": _clean_text(reason),
        "stop_reason": _clean_text(unified_status.get("stop_reason")),
        "final_status": _clean_text(unified_status.get("final_status")),
        "is_blocked": bool(unified_status.get("is_blocked", False)),
        "blocking_reason": _clean_text(unified_status.get("blocking_reason")),
    }


def _semantic_compact_payload(
    *,
    feature: str,
    project_root: Path,
    state: Any,
    context: Any,
    task_label: str,
    task_scope: str,
    verify_check: dict[str, Any] | None,
    gate_check: dict[str, Any] | None,
    reason: str,
    token_budget: int | None,
    trigger_event: str,
    mode: str,
) -> dict[str, Any]:
    unified_status = _state_unified_status(state)
    decisions = [_decision_payload(item) for item in list(_state_value(state, "architecture_decisions", []))]
    payload = {
        "schema_version": SEMANTIC_COMPACT_VERSION,
        "feature": _clean_text(feature),
        "project_root": str(Path(project_root).resolve()),
        "generated_at": _utc_now_iso(),
        "trigger_event": _clean_text(trigger_event),
        "mode": _clean_text(mode, default="full"),
        "goal": _goal_payload(feature=feature, task_label=task_label, task_scope=task_scope),
        "constraints": _constraints(decisions),
        "decisions": decisions,
        "open_questions": _open_questions(context),
        "must_fix": _must_fix(context),
        "recent_errors": _recent_error_payload(state),
        "last_error": _clean_text(_state_value(state, "last_error")),
        "next_action": _clean_text(unified_status.get("next_action")),
        "verify_targets": _verify_targets(verify_check),
        "verify_target_source": _clean_text((verify_check or {}).get("verify_target_source")),
        "gate_recommendation": _clean_text(_review_feedback_value(context, "gate_recommendation")),
        "loop_outcome": _loop_outcome_payload(reason=reason, unified_status=unified_status),
        "token_budget_snapshot": _token_budget_snapshot(
            _state_value(state, "tokens_used", 0),
            token_budget,
        ),
        "verify_check_status": _clean_text((verify_check or {}).get("status")),
        "gate_status": _clean_text((gate_check or {}).get("total_status")),
    }
    return payload


def _merge_incremental(
    *,
    existing: dict[str, Any] | None,
    latest: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(existing, dict):
        return latest
    merged = dict(existing)
    for key in (
        "generated_at",
        "trigger_event",
        "mode",
        "must_fix",
        "recent_errors",
        "last_error",
        "next_action",
        "verify_targets",
        "verify_target_source",
        "gate_recommendation",
        "loop_outcome",
        "verify_check_status",
        "gate_status",
        "token_budget_snapshot",
    ):
        merged[key] = latest.get(key)
    return merged


def _markdown_payload(compact: dict[str, Any]) -> str:
    must_fix = _string_list(compact.get("must_fix"))
    verify_targets = _string_list(compact.get("verify_targets"))
    open_questions = _string_list(compact.get("open_questions"))
    errors = list(compact.get("recent_errors") or [])
    lines = [
        "# Semantic Compact",
        "",
        f"- feature: {_clean_text(compact.get('feature'))}",
        f"- goal: {_clean_text(compact.get('goal'))}",
        f"- trigger_event: {_clean_text(compact.get('trigger_event'))}",
        f"- mode: {_clean_text(compact.get('mode'))}",
        f"- next_action: {_clean_text(compact.get('next_action'))}",
        f"- last_error: {_clean_text(compact.get('last_error'))}",
        f"- gate_recommendation: {_clean_text(compact.get('gate_recommendation'))}",
        f"- verify_target_source: {_clean_text(compact.get('verify_target_source'))}",
        "",
        "## must-fix",
    ]
    if must_fix:
        lines.extend(f"- {item}" for item in must_fix)
    else:
        lines.append("- (none)")
    lines.extend(["", "## verify_targets"])
    if verify_targets:
        lines.extend(f"- {item}" for item in verify_targets)
    else:
        lines.append("- (none)")
    lines.extend(["", "## open_questions"])
    if open_questions:
        lines.extend(f"- {item}" for item in open_questions)
    else:
        lines.append("- (none)")
    lines.extend(["", "## recent_errors"])
    if errors:
        for item in errors:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"- [{_clean_text(item.get('category'))}] {_clean_text(item.get('message'))}"
            )
    else:
        lines.append("- (none)")
    return "\n".join(lines).strip() + "\n"


def _semantic_paths(planning_dir: Path) -> dict[str, Path]:
    return {
        "semantic_compact.json": (planning_dir / "semantic_compact.json").resolve(),
        "semantic_compact.md": (planning_dir / "semantic_compact.md").resolve(),
    }


def _load_existing_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


_COMPACT_DEDUP_KEYS = (
    # Loop-state fields
    "must_fix",
    "last_error",
    "next_action",
    "gate_recommendation",
    "gate_status",
    "verify_check_status",
    "recent_errors",
    "loop_outcome",
    # Structural content fields — omitting these caused false "unchanged" when
    # only verify_targets, decisions, etc. changed between compact cycles.
    "goal",
    "constraints",
    "decisions",
    "open_questions",
    "verify_targets",
    "verify_target_source",
)


def _compact_is_unchanged(existing: dict[str, Any] | None, latest: dict[str, Any]) -> bool:
    """Return True if all dynamic key fields are identical — no write needed."""
    if not isinstance(existing, dict):
        return False
    for key in _COMPACT_DEDUP_KEYS:
        if existing.get(key) != latest.get(key):
            return False
    return True


def materialize_semantic_compact(
    *,
    project_root: Path,
    feature: str,
    state: Any,
    context: Any = None,
    task_label: str = "",
    task_scope: str = "",
    verify_check: dict[str, Any] | None = None,
    gate_check: dict[str, Any] | None = None,
    reason: str = "",
    token_budget: int | None = None,
    trigger_event: str = "pre_compact",
    mode: str = "full",
    planning_dir: Path | None = None,
) -> dict[str, Any]:
    resolved_planning_dir = (
        Path(planning_dir).resolve()
        if planning_dir is not None
        else (Path(project_root).resolve() / "planning" / str(feature)).resolve()
    )
    paths = _semantic_paths(resolved_planning_dir)
    compact_payload = _semantic_compact_payload(
        feature=feature,
        project_root=project_root,
        state=state,
        context=context,
        task_label=task_label,
        task_scope=task_scope,
        verify_check=verify_check,
        gate_check=gate_check,
        reason=reason,
        token_budget=token_budget,
        trigger_event=trigger_event,
        mode=mode,
    )
    existing_json = _load_existing_json(paths["semantic_compact.json"])
    if _clean_text(mode).lower() == "incremental":
        compact_payload = _merge_incremental(existing=existing_json, latest=compact_payload)
    # When must_fix or recent_errors grow beyond budget, ask the model advisor to
    # compress them.  Falls back silently if advisor is not configured or fails.
    must_fix_list = list(compact_payload.get("must_fix") or [])
    errors_list = list(compact_payload.get("recent_errors") or [])
    if len(must_fix_list) > COMPACT_MUST_FIX_THRESHOLD or len(errors_list) > COMPACT_ERRORS_THRESHOLD:
        compressed = _model_compress(must_fix=must_fix_list, recent_errors=errors_list)
        if compressed:
            compact_payload = dict(compact_payload)
            if "must_fix" in compressed:
                compact_payload["must_fix"] = compressed["must_fix"]
            if "recent_errors_summary" in compressed:
                compact_payload["recent_errors_summary"] = compressed["recent_errors_summary"]
            compact_payload["compact_source"] = "model_compressed"
            logger.debug(
                "semantic_compact: model compression applied (must_fix %d→%d, errors %d→%d)",
                len(must_fix_list), len(compact_payload.get("must_fix") or []),
                len(errors_list), len(compressed.get("recent_errors_summary") or errors_list),
            )
    if _compact_is_unchanged(existing_json, compact_payload):
        return {
            "status": "unchanged",
            "trigger_event": _clean_text(trigger_event),
            "mode": _clean_text(mode, default="full"),
            "artifacts": {name: str(path) for name, path in paths.items()},
            "payload": compact_payload,
        }
    markdown = _markdown_payload(compact_payload)
    try:
        resolved_planning_dir.mkdir(parents=True, exist_ok=True)
        paths["semantic_compact.json"].write_text(
            json.dumps(compact_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        paths["semantic_compact.md"].write_text(markdown, encoding="utf-8")
    except OSError as exc:
        return {
            "status": "write_failed",
            "trigger_event": _clean_text(trigger_event),
            "mode": _clean_text(mode, default="full"),
            "error": str(exc),
            "artifacts": {},
            "payload": compact_payload,
        }
    return {
        "status": "written",
        "trigger_event": _clean_text(trigger_event),
        "mode": _clean_text(mode, default="full"),
        "artifacts": {name: str(path) for name, path in paths.items()},
        "payload": compact_payload,
    }


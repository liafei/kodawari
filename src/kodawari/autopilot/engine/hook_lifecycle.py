"""Hook lifecycle helpers absorbed from workflow-claude semantics."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


HOOK_PHASES: dict[str, str] = {
    "session_start": "SESSION",
    "session_stop": "SESSION",
    "pre_compact": "COMPACT",
    "pre_plan": "PLAN",
    "post_plan": "PLAN",
    "pre_implement": "IMPLEMENT",
    "post_implement": "IMPLEMENT",
    "pre_review": "REVIEW",
    "post_review": "REVIEW",
    "pre_gate": "GATE",
    "post_gate": "GATE",
    "auto_gate": "GATE",
}

HOOK_PHASE_ORDER: dict[str, int] = {
    "SESSION": 0,
    "COMPACT": 1,
    "PLAN": 2,
    "IMPLEMENT": 3,
    "REVIEW": 4,
    "GATE": 5,
    "UNKNOWN": 99,
}

HOOK_LIFECYCLE_VERSION = "ws114.v2"

HOOK_ACTOR_BOUNDARY: dict[str, str] = {
    "pre_plan": "opus",
    "post_plan": "opus",
    "pre_review": "opus",
    "post_review": "opus",
    "pre_implement": "codex",
    "post_implement": "codex",
    "pre_gate": "system",
    "post_gate": "system",
    "auto_gate": "system",
    "pre_compact": "system",
    "session_start": "system",
    "session_stop": "system",
}

MERGED_ABSORPTION_STATUS: dict[str, str] = {
    "planning_summary": "已吸收",
    "context_compact": "部分吸收",
    "instincts": "部分吸收",
}

logger = logging.getLogger(__name__)


def resolve_hook_phase(event: str) -> str:
    return HOOK_PHASES.get(str(event), "UNKNOWN")


def resolve_hook_phase_order(phase: str) -> int:
    return int(HOOK_PHASE_ORDER.get(str(phase), HOOK_PHASE_ORDER["UNKNOWN"]))


def resolve_hook_actor_boundary(
    *,
    event: str,
    role: str | None,
) -> str:
    if role:
        return str(role)
    return HOOK_ACTOR_BOUNDARY.get(str(event), "system")


def build_lifecycle_event(
    *,
    event: str,
    task_id: str,
    task_label: str,
    cycle: int,
    scope: str = "",
    status: str = "ok",
    action: str | None = None,
    role: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_event = str(event)
    phase = resolve_hook_phase(resolved_event)
    actor_boundary = resolve_hook_actor_boundary(event=resolved_event, role=role)
    payload: dict[str, Any] = {
        "event": resolved_event,
        "lifecycle_version": HOOK_LIFECYCLE_VERSION,
        "status": str(status),
        "task_id": str(task_id),
        "task_label": str(task_label),
        "scope": str(scope),
        "cycle": int(cycle),
        "phase": phase,
        "phase_order": resolve_hook_phase_order(phase),
        "actor_boundary": actor_boundary,
        # Keep lifecycle payload compatibility with workflow-claude default.
        "instincts_loaded": False,
        "source": "kodawari",
    }
    if action is not None:
        payload["action"] = str(action)
    if role is not None:
        payload["role"] = str(role)
    if details:
        payload["details"] = dict(details)
    return payload


def build_pre_compact_payload(
    *,
    project_root: Path,
    feature: str,
    include_instincts: bool = True,
    log_tail_lines: int = 20,
    instinct_hints_limit: int = 5,
    instinct_min_confidence: float = 0.5,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    include_instincts_flag = bool(include_instincts)
    log_tail = int(log_tail_lines)
    hints_limit = int(instinct_hints_limit)
    min_confidence = float(instinct_min_confidence)
    instinct_payload = resolve_instinct_payload(
        project_root=root,
        include_instincts=include_instincts_flag,
        hints_limit=hints_limit,
        min_confidence=min_confidence,
    )
    base = _base_compact_payload(
        feature=feature,
        root=root,
        include_instincts=include_instincts_flag,
        log_tail_lines=log_tail,
        instinct_hints_limit=hints_limit,
        instinct_min_confidence=min_confidence,
        instinct_payload=instinct_payload,
    )
    payload = {
        **base,
        "compact_markdown": _compact_markdown(
            feature=feature,
            root=root,
            include_instincts=include_instincts_flag,
            log_tail_lines=log_tail,
            instincts_status=str(base["instincts_status"]),
            instinct_hints_count=int(base["instinct_hints_count"]),
        ),
        "compact_json": dict(base),
    }
    _apply_instincts_error(payload, instinct_payload.get("instincts_error"))
    return payload


def _instinct_data_available(project_root: Path) -> bool:
    """Return True when either the project store (new or legacy path) or the
    cross-project global store has data we could surface as hints.

    Errors importing the global store, or filesystem hiccups, fall back to
    "data not available" so the rest of the pipeline keeps running.
    """
    from kodawari.instincts.storage import InstinctStore

    if InstinctStore(project_root).exists():
        return True
    try:
        from kodawari.instincts.global_store import GlobalInstinctStore
        return GlobalInstinctStore().exists()
    except Exception:  # noqa: BLE001
        return False


def _resolve_instinct_store_path(project_root: Path) -> Path:
    """Return the path of the *new* on-disk instincts store.

    The legacy ``.claude/memory/instincts.json`` is still readable through
    ``InstinctStore``, but the canonical "store path" surfaced in payloads is
    always the new ``.workflow/`` location so downstream consumers report the
    forward-compatible path.
    """
    from kodawari.instincts.storage import STORE_RELATIVE_PATH

    root = Path(project_root).resolve()
    return (root / STORE_RELATIVE_PATH).resolve()


def _normalize_instinct_hints(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            normalized.append(dict(item))
    return normalized


def resolve_instinct_payload(
    *,
    project_root: Path,
    include_instincts: bool,
    hints_limit: int = 5,
    min_confidence: float = 0.5,
) -> dict[str, Any]:
    from kodawari.instincts.storage import InstinctStore

    root = Path(project_root).resolve()
    store_path = _resolve_instinct_store_path(root)
    payload = _empty_instinct_payload(store_path)
    if not include_instincts:
        payload["instincts_status"] = "disabled_by_request"
        return payload
    # "store_not_found" only fires when BOTH the project store (incl. legacy
    # location for backward compat) and the cross-project global store are
    # absent. A global hit alone is enough to surface hints to the runtime.
    if not _instinct_data_available(root):
        payload["instincts_status"] = "store_not_found"
        return payload

    hints_payload = _load_instinct_hints(
        root=root,
        hints_limit=max(0, int(hints_limit)),
        min_confidence=float(min_confidence),
    )
    if hints_payload["status"] != "ok":
        payload["instincts_status"] = str(hints_payload["status"])
        if hints_payload.get("error"):
            payload["instincts_error"] = str(hints_payload["error"])
        return payload

    normalized = _normalize_instinct_hints(hints_payload.get("hints"))
    payload["instincts_loaded"] = True
    payload["instincts_status"] = "loaded" if normalized else "loaded_empty"
    payload["instinct_hints"] = normalized
    payload["instinct_hints_count"] = len(normalized)
    return payload


def resolve_compact_planning_dir(
    *,
    project_root: Path,
    feature: str,
    planning_dir: Path | None = None,
) -> Path:
    if planning_dir is not None:
        return Path(planning_dir).resolve()
    return (Path(project_root).resolve() / "planning" / str(feature)).resolve()


def _compact_artifact_paths(planning_dir: Path) -> dict[str, Path]:
    return {
        "COMPACT_CONTEXT.md": (planning_dir / "COMPACT_CONTEXT.md").resolve(),
        "compact_context.json": (planning_dir / "compact_context.json").resolve(),
    }


def _runtime_compact_json_payload(
    *,
    payload: dict[str, Any],
    trigger_event: str,
) -> dict[str, Any]:
    compact_json = dict(payload.get("compact_json", {}))
    compact_json.setdefault("feature", _string_or_default(payload, "feature"))
    compact_json.setdefault("project_root", _string_or_default(payload, "project_root"))
    compact_json["runtime_trigger_event"] = str(trigger_event)
    compact_json["runtime_status"] = "partial"
    compact_json["runtime_mode"] = "compat"
    compact_json["include_instincts_requested"] = bool(payload.get("include_instincts"))
    compact_json["instincts_loaded"] = bool(payload.get("instincts_loaded"))
    compact_json["instincts_status"] = _string_or_default(payload, "instincts_status", "placeholder_unloaded")
    compact_json["instincts_store_path"] = _string_or_default(payload, "instincts_store_path")
    compact_json["instinct_hints_count"] = int(payload.get("instinct_hints_count", 0) or 0)
    compact_json["instinct_hints"] = _normalize_instinct_hints(payload.get("instinct_hints", []))
    compact_json["merged_absorption_status"] = _merged_absorption_status_payload()
    _apply_optional_string(compact_json, "instincts_error", payload.get("instincts_error"))
    return compact_json


def materialize_runtime_compact(
    *,
    project_root: Path,
    feature: str,
    payload: dict[str, Any],
    trigger_event: str = "pre_compact",
    planning_dir: Path | None = None,
) -> dict[str, Any]:
    resolved_planning_dir = resolve_compact_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=planning_dir,
    )
    artifact_result = _write_runtime_compact_artifacts(
        planning_dir=resolved_planning_dir,
        payload=payload,
        trigger_event=trigger_event,
    )
    runtime_payload = _build_runtime_compact_payload(
        planning_dir=resolved_planning_dir,
        payload=payload,
        trigger_event=trigger_event,
        artifacts=artifact_result["artifacts"],
        artifact_state=str(artifact_result["artifact_state"]),
    )
    _apply_optional_string(runtime_payload, "artifact_write_error", artifact_result.get("artifact_write_error"))
    _apply_optional_string(runtime_payload, "instincts_error", payload.get("instincts_error"))
    return runtime_payload


def _base_compact_payload(
    *,
    feature: str,
    root: Path,
    include_instincts: bool,
    log_tail_lines: int,
    instinct_hints_limit: int,
    instinct_min_confidence: float,
    instinct_payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "feature": feature,
        "project_root": str(root),
        "include_instincts": include_instincts,
        "log_tail_lines": log_tail_lines,
        "instinct_hints_limit": instinct_hints_limit,
        "instinct_min_confidence": instinct_min_confidence,
        "compact_status": "partial",
        "compact_mode": "compat",
        "instincts_loaded": bool(instinct_payload["instincts_loaded"]),
        "instincts_status": str(instinct_payload["instincts_status"]),
        "instincts_source": str(instinct_payload["instincts_source"]),
        "instincts_store_path": str(instinct_payload["instincts_store_path"]),
        "instinct_hints_count": int(instinct_payload["instinct_hints_count"]),
        "instinct_hints": [dict(item) for item in instinct_payload["instinct_hints"]],
        "merged_absorption_status": _merged_absorption_status_payload(),
    }


def _compact_markdown(
    *,
    feature: str,
    root: Path,
    include_instincts: bool,
    log_tail_lines: int,
    instincts_status: str,
    instinct_hints_count: int,
) -> str:
    return (
        "# Compact Context\n\n"
        f"- feature: {feature}\n"
        f"- project_root: {root}\n"
        f"- include_instincts: {include_instincts}\n"
        f"- log_tail_lines: {log_tail_lines}\n"
        f"- instincts_status: {instincts_status}\n"
        f"- instinct_hints_count: {instinct_hints_count}\n"
    )


def _empty_instinct_payload(store_path: Path) -> dict[str, Any]:
    return {
        "instincts_loaded": False,
        "instincts_status": "placeholder_unloaded",
        "instincts_source": "kodawari.instincts",
        "instincts_store_path": str(store_path),
        "instinct_hints_count": 0,
        "instinct_hints": [],
    }


def _merged_absorption_status_payload() -> dict[str, str]:
    return dict(MERGED_ABSORPTION_STATUS)


def _load_instinct_hints(
    *,
    root: Path,
    hints_limit: int,
    min_confidence: float,
) -> dict[str, Any]:
    try:
        from kodawari.instincts import select_instinct_hints
    except Exception:
        logger.warning("instinct module unavailable while building compact payload", exc_info=True)
        return {"status": "module_unavailable", "hints": []}
    try:
        hints = select_instinct_hints(
            root,
            limit=hints_limit,
            min_confidence=min_confidence,
        )
    except Exception as exc:
        logger.warning("instinct hint load failed while building compact payload", exc_info=True)
        return {"status": "load_failed", "error": str(exc), "hints": []}
    return {"status": "ok", "hints": hints}


def _string_or_default(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key)
    if value is None:
        return default
    text = str(value)
    return text if text else default


def _apply_optional_string(target: dict[str, Any], key: str, value: Any) -> None:
    if value:
        target[key] = str(value)


def _apply_instincts_error(payload: dict[str, Any], instincts_error: Any) -> None:
    if not instincts_error:
        return
    payload["instincts_error"] = str(instincts_error)
    compact_json = payload.get("compact_json")
    if isinstance(compact_json, dict):
        compact_json["instincts_error"] = str(instincts_error)


def _write_runtime_compact_artifacts(
    *,
    planning_dir: Path,
    payload: dict[str, Any],
    trigger_event: str,
) -> dict[str, Any]:
    try:
        planning_dir.mkdir(parents=True, exist_ok=True)
        paths = _compact_artifact_paths(planning_dir)
        paths["COMPACT_CONTEXT.md"].write_text(
            str(payload.get("compact_markdown") or "# Compact Context\n"),
            encoding="utf-8",
        )
        compact_json_payload = _runtime_compact_json_payload(payload=payload, trigger_event=trigger_event)
        paths["compact_context.json"].write_text(
            json.dumps(compact_json_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "artifact_state": "written",
            "artifacts": {name: str(path) for name, path in paths.items()},
            "artifact_write_error": None,
        }
    except OSError as exc:
        logger.warning("runtime compact artifact write failed", exc_info=True)
        return {
            "artifact_state": "write_failed",
            "artifacts": {},
            "artifact_write_error": str(exc),
        }


def _build_runtime_compact_payload(
    *,
    planning_dir: Path,
    payload: dict[str, Any],
    trigger_event: str,
    artifacts: dict[str, str],
    artifact_state: str,
) -> dict[str, Any]:
    return {
        "runtime_version": HOOK_LIFECYCLE_VERSION,
        "status": "partial",
        "mode": "compat",
        "triggered": True,
        "trigger_event": str(trigger_event),
        "source": "autopilot_loop",
        "algorithm": "lightweight_payload_only",
        "planning_dir": str(planning_dir),
        "artifact_state": artifact_state,
        "artifacts": artifacts,
        "include_instincts_requested": bool(payload.get("include_instincts")),
        "instincts_loaded": bool(payload.get("instincts_loaded")),
        "instincts_status": _string_or_default(payload, "instincts_status", "placeholder_unloaded"),
        "instincts_store_path": _string_or_default(payload, "instincts_store_path"),
        "instinct_hints_count": int(payload.get("instinct_hints_count", 0) or 0),
        "instinct_hints": _normalize_instinct_hints(payload.get("instinct_hints", [])),
        "merged_absorption_status": _merged_absorption_status_payload(),
    }

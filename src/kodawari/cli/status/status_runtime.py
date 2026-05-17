"""Runtime status helpers for canonical truth alignment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.review_runtime_policy import classify_review_runtime


def review_truth_source(planning_dir: Path, review_truth: dict[str, Any] | None = None) -> str:
    if isinstance(review_truth, dict):
        source = str(review_truth.get("truth_source") or "").strip()
        if source:
            return source
    review_result = (planning_dir / ".review_result.json").exists()
    review_evidence = (planning_dir / ".review_evidence.json").exists()
    review_markdown = (planning_dir / "REVIEW.md").exists()
    if review_evidence:
        return ".review_evidence.json"
    if review_result:
        return ".review_result.json"
    if review_markdown:
        return "REVIEW.md"
    return "none"


def execution_truth_source(execution_check: dict[str, Any]) -> str:
    source = str(execution_check.get("source") or "").strip()
    return source or "none"


def verify_truth_source(verify_check: dict[str, Any], verify_truth: dict[str, Any] | None = None) -> str:
    if isinstance(verify_truth, dict):
        source = str(verify_truth.get("truth_source") or "").strip()
        if source:
            return source
    source = str(verify_check.get("source") or "").strip()
    return source or "none"


def review_runtime_summary(
    *,
    planning_dir: Path,
    review_payload: dict[str, Any] | None,
    workflow_chain: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(review_payload or {})
    if payload:
        runtime_classification = classify_review_runtime(
            {
                "mode": payload.get("review_runtime_mode_raw"),
                "real_requested": payload.get("real_review_requested"),
                "real_required": payload.get("real_review_required"),
                "fallback_used": payload.get("fallback_used"),
            },
            require_real_peer_review=_bool_value(payload.get("real_review_required")),
        )
        return {
            "review_mode": _clean_text(payload.get("review_mode")),
            "real_review_requested": _bool_value(payload.get("real_review_requested")),
            "real_review_required": _bool_value(payload.get("real_review_required")),
            "fallback_used": _bool_value(payload.get("fallback_used")),
            "review_quality": _clean_text(payload.get("review_quality")) or runtime_classification.review_quality,
            "semantic_review_performed": _bool_value(
                payload.get("semantic_review_performed", runtime_classification.semantic_review_performed)
            ),
        }
    chain_runtime = _workflow_chain_review_runtime(workflow_chain)
    if chain_runtime:
        return chain_runtime
    review_result_path = (planning_dir / ".review_result.json").resolve()
    artifact = _load_json_artifact(review_result_path)
    if artifact:
        runtime_classification = classify_review_runtime(
            {
                "mode": artifact.get("review_runtime_mode_raw"),
                "real_requested": artifact.get("real_review_requested"),
                "real_required": artifact.get("real_review_required"),
                "fallback_used": artifact.get("fallback_used"),
            },
            require_real_peer_review=_bool_value(artifact.get("real_review_required")),
        )
        return {
            "review_mode": _clean_text(artifact.get("review_mode")),
            "real_review_requested": _bool_value(artifact.get("real_review_requested")),
            "real_review_required": _bool_value(artifact.get("real_review_required")),
            "fallback_used": _bool_value(artifact.get("fallback_used")),
            "review_quality": _clean_text(artifact.get("review_quality")) or runtime_classification.review_quality,
            "semantic_review_performed": _bool_value(
                artifact.get("semantic_review_performed", runtime_classification.semantic_review_performed)
            ),
        }
    return {
        "review_mode": "",
        "real_review_requested": False,
        "real_review_required": False,
        "fallback_used": False,
        "review_quality": "simulated",
        "semantic_review_performed": False,
    }


def verify_runtime_summary(verify_check: dict[str, Any]) -> dict[str, Any]:
    surfaces = _verify_surfaces(verify_check)
    return {
        "verify_scope_mode": _clean_text(verify_check.get("verify_scope_mode")),
        "verify_surfaces": surfaces,
    }


def execution_runtime_summary(execution_check: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_backend": _clean_text(execution_check.get("backend")),
        "execution_backend_capabilities": dict(execution_check.get("backend_capabilities") or {}),
        "execution_backend_capability_truth": dict(execution_check.get("backend_capability_truth") or {}),
        "execution_host_probe": dict(execution_check.get("host_probe") or {}),
        "execution_guard": dict(execution_check.get("execution_guard") or {}),
    }


def budget_snapshot(
    *,
    state_payload: dict[str, Any] | None,
    semantic_compact: dict[str, Any] | None,
) -> dict[str, Any]:
    semantic_snapshot = dict((semantic_compact or {}).get("token_budget_snapshot") or {})
    tokens_used = _int_value(
        semantic_snapshot.get("tokens_used"),
        fallback=_int_value((state_payload or {}).get("tokens_used"), fallback=0),
    )
    token_budget = _int_value(semantic_snapshot.get("token_budget"), fallback=0)
    budget_exhausted = _bool_value(semantic_snapshot.get("budget_exhausted"))
    if not budget_exhausted and token_budget > 0:
        budget_exhausted = tokens_used > token_budget
    if not budget_exhausted:
        stop_reason = str(dict((state_payload or {}).get("unified_status") or {}).get("stop_reason") or "").upper()
        budget_exhausted = stop_reason == "TOKEN_BUDGET"
    return {
        "tokens_used": tokens_used,
        "token_budget": token_budget or None,
        "budget_exhausted": budget_exhausted,
    }


def _int_value(value: Any, *, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return False
    return text in {"1", "true", "yes", "on"}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _verify_surfaces(verify_check: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in list(verify_check.get("surface_results") or []):
        if not isinstance(item, dict):
            continue
        surface = _clean_text(item.get("surface"))
        if surface and surface not in names:
            names.append(surface)
    for item in list(dict(verify_check.get("surface_summary") or {}).get("required_surfaces") or []):
        surface = _clean_text(item)
        if surface and surface not in names:
            names.append(surface)
    return names


def _workflow_chain_review_runtime(workflow_chain: dict[str, Any] | None) -> dict[str, Any]:
    upstream = dict((workflow_chain or {}).get("upstream") or {})
    runtime = dict(upstream.get("peer_review_runtime") or {})
    if not runtime:
        return {}
    raw_mode = _clean_text(runtime.get("mode"))
    runtime_classification = classify_review_runtime(
        runtime,
        require_real_peer_review=_bool_value(runtime.get("real_required")),
    )
    review_mode = "real_peer_review" if runtime_classification.is_real_review else "simulated"
    return {
        "review_mode": review_mode,
        "real_review_requested": _bool_value(runtime.get("real_requested")),
        "real_review_required": _bool_value(runtime.get("real_required")),
        "fallback_used": _bool_value(runtime.get("fallback_used")),
        "review_quality": runtime_classification.review_quality,
        "semantic_review_performed": runtime_classification.semantic_review_performed,
    }


def _load_json_artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "budget_snapshot",
    "execution_runtime_summary",
    "execution_truth_source",
    "review_runtime_summary",
    "review_truth_source",
    "verify_runtime_summary",
    "verify_truth_source",
]

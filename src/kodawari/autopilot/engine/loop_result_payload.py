"""Helpers for building normalized loop result payloads."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.review.review_bridge import summarize_self_review
from kodawari.autopilot.core.runtime_budget import build_token_budget_snapshot
from kodawari.autopilot.core.collaboration import merge_loop_result_optionals

_REASON_STOP_REASON: dict[str, str] = {
    "PROCEED_TO_GATE": "PASS",
    "PIPELINE_FINISH": "PASS",
    "VERIFY_BLOCKED": "HARD_ERROR",
    "GATE_BLOCKED": "HARD_ERROR",
    "OPUS_REVIEW_BLOCKED": "HARD_ERROR",
    "SELF_REVIEW_BLOCKED": "HARD_ERROR",
    "EXECUTION_BACKEND_BLOCKED": "HARD_ERROR",
    "MAX_CYCLES_REACHED": "MAX_CYCLES",
    "COLLABORATION_ROUND_LIMIT": "STUCK",
    "PROTECTED_FILE_BLOCK": "HARD_ERROR",
    "IMPLEMENTATION_ERROR": "HARD_ERROR",
    "TOKEN_BUDGET_EXCEEDED": "TOKEN_BUDGET",
}


def build_loop_result_payload(
    *,
    feature: str,
    unified_status: dict[str, Any],
    stopped: bool,
    reason: str,
    task_label: str,
    context: Any,
    round_records: list[dict[str, Any]],
    hook_events: list[dict[str, Any]],
    peer_review_policy: dict[str, Any],
    peer_review_summary: dict[str, Any] | None = None,
    codex_self_reviews: list[dict[str, Any]] | None = None,
    post_execution_qa: dict[str, Any] | None = None,
    verify_check: dict[str, Any] | None = None,
    gate_check: dict[str, Any] | None = None,
    pre_compact: dict[str, Any] | None = None,
    last_error: str | None = None,
    execution_result: dict[str, Any] | None = None,
    execution_artifacts: dict[str, str] | None = None,
    tokens_used: int = 0,
    token_budget: int | None = None,
) -> dict[str, Any]:
    reviews, self_review_summary, normalized_unified_status = _loop_runtime_summaries(
        feature=feature, reason=reason, codex_self_reviews=codex_self_reviews, unified_status=unified_status
    )
    compact_payload = _finalize_runtime_compact_payload(
        pre_compact=pre_compact,
        reason=reason,
        unified_status=normalized_unified_status,
        peer_review_summary=peer_review_summary,
        self_review_summary=self_review_summary,
        verify_check=verify_check,
        gate_check=gate_check,
    )
    payload = _base_payload(
        feature=feature,
        unified_status=normalized_unified_status,
        stopped=stopped,
        reason=reason,
        task_label=task_label,
        context=context,
        round_records=round_records,
        hook_events=hook_events,
        peer_review_policy=peer_review_policy,
        peer_review_summary=peer_review_summary,
        codex_self_reviews=reviews,
        self_review_summary=self_review_summary,
        tokens_used=tokens_used,
        token_budget=token_budget,
    )
    optionals = _optional_runtime_fields(
        verify_check=verify_check,
        gate_check=gate_check,
        pre_compact=compact_payload,
        execution_result=execution_result,
        execution_artifacts=execution_artifacts,
    )
    payload.update(optionals)
    return merge_loop_result_optionals(payload, pre_compact=compact_payload, post_execution_qa=post_execution_qa, last_error=last_error)


def _loop_runtime_summaries(
    *,
    feature: str,
    reason: str,
    codex_self_reviews: list[dict[str, Any]] | None,
    unified_status: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    reviews = list(codex_self_reviews or [])
    self_review_summary = summarize_self_review(feature, codex_self_reviews)
    normalized_unified_status = _normalized_unified_status(reason=reason, unified_status=unified_status)
    return reviews, self_review_summary, normalized_unified_status


def _base_payload(
    *,
    feature: str,
    unified_status: dict[str, Any],
    stopped: bool,
    reason: str,
    task_label: str,
    context: Any,
    round_records: list[dict[str, Any]],
    hook_events: list[dict[str, Any]],
    peer_review_policy: dict[str, Any],
    peer_review_summary: dict[str, Any] | None,
    codex_self_reviews: list[dict[str, Any]],
    self_review_summary: dict[str, Any],
    tokens_used: int,
    token_budget: int | None,
) -> dict[str, Any]:
    budget = build_token_budget_snapshot(tokens_used, token_budget)
    return {
        "stopped": stopped,
        "reason": reason,
        "task": task_label,
        "rounds": round_records,
        "hook_events": hook_events,
        "peer_review_policy": dict(peer_review_policy),
        "peer_review_summary": dict(peer_review_summary or {}),
        "codex_self_reviews": codex_self_reviews,
        "self_review_summary": dict(self_review_summary),
        "collaboration_context": context.to_dict(),
        "review_rounds_used": context.review_feedback.review_iteration,
        "architecture_decisions": [item.to_dict() for item in context.architecture_decisions],
        "must_fix_open_items": list(context.review_feedback.must_fix),
        "gate_recommendation": context.review_feedback.gate_recommendation,
        "unified_status": dict(unified_status),
        "tokens_used": int(budget.get("tokens_used") or 0),
        "token_budget": budget.get("token_budget"),
        "budget_exhausted": bool(budget.get("budget_exhausted", False)),
    }


def _optional_runtime_fields(
    *,
    verify_check: dict[str, Any] | None,
    gate_check: dict[str, Any] | None,
    pre_compact: dict[str, Any] | None,
    execution_result: dict[str, Any] | None,
    execution_artifacts: dict[str, str] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if verify_check:
        payload["verify_check"] = dict(verify_check)
    if gate_check:
        payload["gate_check"] = dict(gate_check)
    if execution_result:
        payload["execution_result"] = dict(execution_result)
    if execution_artifacts:
        payload["execution_artifacts"] = dict(execution_artifacts)
    runtime_compact = _runtime_compact_payload(pre_compact)
    if runtime_compact:
        payload["context_compact_runtime"] = runtime_compact
    semantic_runtime = _semantic_runtime_payload(pre_compact)
    if semantic_runtime:
        payload["semantic_compact_runtime"] = semantic_runtime
    return payload


def _runtime_compact_payload(pre_compact: dict[str, Any] | None) -> dict[str, Any]:
    runtime = dict(pre_compact.get("runtime", {})) if isinstance(pre_compact, dict) else {}
    return runtime if runtime else {}


def _semantic_runtime_payload(pre_compact: dict[str, Any] | None) -> dict[str, Any]:
    runtime = (
        dict(pre_compact.get("semantic_compact_runtime", {}))
        if isinstance(pre_compact, dict)
        else {}
    )
    return runtime if runtime else {}


def _finalize_runtime_compact_payload(
    *,
    pre_compact: dict[str, Any] | None,
    reason: str,
    unified_status: dict[str, Any],
    peer_review_summary: dict[str, Any] | None,
    self_review_summary: dict[str, Any],
    verify_check: dict[str, Any] | None,
    gate_check: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(pre_compact, dict):
        return pre_compact
    compact_payload = dict(pre_compact)
    runtime_payload = compact_payload.get("runtime")
    if not isinstance(runtime_payload, dict):
        return compact_payload
    summary = _runtime_post_loop_summary(
        reason=reason,
        unified_status=unified_status,
        peer_review_summary=peer_review_summary,
        self_review_summary=self_review_summary,
        verify_check=verify_check,
        gate_check=gate_check,
    )
    compact_payload["runtime"] = _runtime_payload_with_post_loop(runtime_payload, summary)
    _write_compact_post_loop_summary(compact_payload["runtime"], summary)
    return compact_payload


def _runtime_post_loop_summary(
    *,
    reason: str,
    unified_status: dict[str, Any],
    peer_review_summary: dict[str, Any] | None,
    self_review_summary: dict[str, Any],
    verify_check: dict[str, Any] | None,
    gate_check: dict[str, Any] | None,
) -> dict[str, Any]:
    stop_reason = _resolved_stop_reason(reason=reason, unified_status=unified_status)
    return {
        "reason": str(reason or ""),
        "stop_reason": stop_reason,
        "blocked": False if stop_reason == "PASS" else bool(unified_status.get("is_blocked", False)),
        "gate_recommendation": _peer_gate_recommendation(peer_review_summary),
        "review_round": _peer_review_round(peer_review_summary),
        "peer_review_summary": _peer_review_counts(peer_review_summary),
        "self_review_summary": _self_review_counts(self_review_summary),
        "verify": _verify_summary(verify_check),
        "gate": _gate_summary(gate_check),
    }


def _peer_summary_payload(peer_review_summary: dict[str, Any] | None) -> dict[str, Any]:
    return dict(peer_review_summary or {})


def _peer_gate_recommendation(peer_review_summary: dict[str, Any] | None) -> str:
    peer = _peer_summary_payload(peer_review_summary)
    return str(peer.get("last_gate_recommendation") or "")


def _peer_review_round(peer_review_summary: dict[str, Any] | None) -> int:
    peer = _peer_summary_payload(peer_review_summary)
    return int(peer.get("review_round", 0) or 0)


def _peer_review_counts(peer_review_summary: dict[str, Any] | None) -> dict[str, Any]:
    peer = _peer_summary_payload(peer_review_summary)
    return {
        "review_count": int(peer.get("review_count", 0) or 0),
        "approved": bool(peer.get("approved", False)),
        "must_fix_remaining": int(peer.get("must_fix_remaining", 0) or 0),
    }


def _self_review_counts(self_review_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "review_count": int(self_review_summary.get("review_count", 0) or 0),
        "approved_count": int(self_review_summary.get("approved_count", 0) or 0),
    }


def _verify_summary(verify_check: dict[str, Any] | None) -> dict[str, Any]:
    verify = dict(verify_check or {})
    return {
        "status": str(verify.get("status") or ""),
        "target_source": str(verify.get("verify_target_source") or ""),
        "targets": _verify_targets(verify),
    }


def _verify_targets(verify: dict[str, Any]) -> list[str]:
    values = list(verify.get("verify_targets") or [])
    return [str(item) for item in values if str(item).strip()]


def _gate_summary(gate_check: dict[str, Any] | None) -> dict[str, Any]:
    gate = dict(gate_check or {})
    return {
        "status": str(gate.get("total_status") or ""),
        "blocking_violations": int(gate.get("blocking_violations", 0) or 0),
    }


def _resolved_stop_reason(*, reason: str, unified_status: dict[str, Any]) -> str:
    explicit = str(unified_status.get("stop_reason") or "").strip().upper()
    if explicit:
        return explicit
    return _REASON_STOP_REASON.get(str(reason or "").strip().upper(), "")


def _normalized_unified_status(*, reason: str, unified_status: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(unified_status)
    if not str(normalized.get("stop_reason") or "").strip():
        normalized["stop_reason"] = _resolved_stop_reason(reason=reason, unified_status=normalized)
    if str(normalized.get("stop_reason") or "").strip().upper() == "PASS":
        normalized.setdefault("final_status", "PASS")
        normalized["is_blocked"] = False
        normalized["blocking_reason"] = ""
    return normalized


def _runtime_payload_with_post_loop(runtime_payload: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    runtime = dict(runtime_payload)
    runtime["post_loop"] = dict(summary)
    runtime["loop_stop_reason"] = str(summary.get("stop_reason") or "")
    runtime["loop_blocked"] = bool(summary.get("blocked", False))
    runtime["loop_reason"] = str(summary.get("reason") or "")
    return runtime


def _write_compact_post_loop_summary(runtime_payload: dict[str, Any], summary: dict[str, Any]) -> None:
    compact_json_path = _compact_json_path(runtime_payload)
    if compact_json_path is None:
        return
    payload = _read_json_payload(compact_json_path)
    if payload is None:
        return
    _attach_post_loop_summary(payload, summary)
    _write_json_payload(compact_json_path, payload)


def _read_json_payload(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _attach_post_loop_summary(payload: dict[str, Any], summary: dict[str, Any]) -> None:
    payload["post_loop"] = dict(summary)
    payload["loop_stop_reason"] = str(summary.get("stop_reason") or "")
    payload["loop_blocked"] = bool(summary.get("blocked", False))
    payload["loop_reason"] = str(summary.get("reason") or "")


def _write_json_payload(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return


def _compact_json_path(runtime_payload: dict[str, Any]) -> Path | None:
    artifacts = runtime_payload.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    raw = artifacts.get("compact_context.json")
    if not raw:
        return None
    try:
        return Path(str(raw)).resolve()
    except OSError:
        return None


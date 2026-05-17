"""Implementation runtime helpers shared by the autopilot engine."""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.collaboration import (
    CollaborationAction,
    CollaborationRole,
    mark_codex_fix_applied,
)


def runtime_instinct_hints(pre_compact_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = pre_compact_payload.get("instinct_hints")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _hint_patterns_from_hints(hints: list[dict[str, Any]]) -> list[str]:
    patterns: list[str] = []
    for item in hints:
        pattern = str(item.get("pattern") or "").strip()
        if pattern:
            patterns.append(pattern)
    return patterns


def _instinct_usage_reason(
    *,
    status: str,
    hints_count: int,
    patterns: list[str],
) -> str:
    if hints_count <= 0:
        return "No instinct hints were applied."
    if patterns:
        preview = ", ".join(patterns[:3])
        return f"Applied {hints_count} instinct hint(s) from patterns: {preview}."
    return f"Applied {hints_count} instinct hint(s) from runtime compact context."


def attach_runtime_instinct_hints(
    impl_context: dict[str, Any],
    *,
    pre_compact_payload: dict[str, Any],
) -> None:
    hints = runtime_instinct_hints(pre_compact_payload)
    patterns = _hint_patterns_from_hints(hints)
    impl_context["instincts_loaded"] = bool(pre_compact_payload.get("instincts_loaded"))
    impl_context["instincts_status"] = str(pre_compact_payload.get("instincts_status") or "")
    impl_context["instinct_hints_count"] = len(hints)
    impl_context["instinct_hints"] = hints
    impl_context["instinct_hint_patterns"] = patterns
    impl_context["instinct_usage_reason"] = _instinct_usage_reason(
        status=str(impl_context["instincts_status"]),
        hints_count=len(hints),
        patterns=patterns,
    )
    warnings = list(impl_context.get("scope_risk_warnings") or [])
    if patterns and not warnings:
        warnings = [f"Verify scope should include learned instinct targets: {', '.join(patterns[:3])}"]
    impl_context["scope_risk_warnings"] = [str(item) for item in warnings if str(item).strip()]


def _hint_patterns(impl_context: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for item in list(impl_context.get("instinct_hints") or []):
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        if pattern:
            patterns.append(pattern)
    return patterns


def implementation_round_details(
    *,
    changed_files: list[str],
    protection: dict[str, Any],
    impl_context: dict[str, Any],
) -> dict[str, Any]:
    details = {
        "changes": changed_files,
        "protection": protection,
        "instincts_status": str(impl_context.get("instincts_status") or ""),
        "instinct_hints_count": int(impl_context.get("instinct_hints_count", 0) or 0),
        "instinct_usage_reason": str(impl_context.get("instinct_usage_reason") or ""),
        "scope_risk_warnings": [str(item) for item in list(impl_context.get("scope_risk_warnings") or []) if str(item).strip()],
    }
    patterns = _hint_patterns(impl_context)
    if patterns:
        details["instinct_hint_patterns"] = patterns
    return details


def post_implement_success_details(
    *,
    changed_files: list[str],
    impl_context: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "ok",
        "changes": changed_files,
        "instinct_hints_count": int(impl_context.get("instinct_hints_count", 0) or 0),
        "instincts_status": str(impl_context.get("instincts_status") or ""),
        "instinct_usage_reason": str(impl_context.get("instinct_usage_reason") or ""),
        "scope_risk_warnings": [str(item) for item in list(impl_context.get("scope_risk_warnings") or []) if str(item).strip()],
    }


def codex_stage_status(action: CollaborationAction) -> str:
    return "fix_applied" if action in {CollaborationAction.FIX_ROUND, CollaborationAction.CODEX_FIX} else "implemented"


def apply_codex_success_transition(
    *,
    context: Any,
    action: CollaborationAction,
    result: dict[str, Any],
) -> None:
    if action in {CollaborationAction.FIX_ROUND, CollaborationAction.CODEX_FIX}:
        mark_codex_fix_applied(
            context,
            resolution_summary=str(result.get("summary") or "Fixes applied"),
        )
        return
    context.assigned_role = CollaborationRole.OPUS
    context.verify_passed = False
    context.rules_gate_passed = False
    context.self_review_completed = False
    context.self_review_approved = False

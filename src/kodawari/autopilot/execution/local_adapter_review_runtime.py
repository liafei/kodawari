"""Review runtime and simulated-review helpers for the local adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.autopilot.core.task_modes import is_verification_only_task
from kodawari.autopilot.review.review_precheck import (
    is_docs_only_path,
    is_test_file,
    resolve_verified_test_evidence,
)


def override_review_config(
    config: Any,
    *,
    real_peer_review: bool | None = None,
    require_real_peer_review: bool | None = None,
) -> dict[str, bool]:
    original: dict[str, bool] = {
        "real_peer_review": bool(config.real_peer_review),
        "require_real_peer_review": bool(config.require_real_peer_review),
    }
    if real_peer_review is not None:
        config.real_peer_review = bool(real_peer_review)
    if require_real_peer_review is not None:
        config.require_real_peer_review = bool(require_real_peer_review)
    return original


def restore_review_config(config: Any, original: dict[str, bool]) -> None:
    config.real_peer_review = bool(original.get("real_peer_review", False))
    config.require_real_peer_review = bool(original.get("require_real_peer_review", False))


def normalized_review_mode(mode: str) -> str:
    normalized = str(mode or "").strip()
    return normalized if normalized else "simulate_local"


def review_runtime_payload(
    *,
    mode: str,
    real_requested: bool,
    real_required: bool,
    gateway: dict[str, str],
    fallback_used: bool = False,
    error: str = "",
) -> dict[str, Any]:
    runtime: dict[str, Any] = {
        "mode": normalized_review_mode(mode),
        "real_requested": bool(real_requested),
        "real_required": bool(real_required),
        "fallback_used": bool(fallback_used),
        "gateway": dict(gateway),
    }
    attach_runtime_error(runtime, error=error)
    return runtime


def attach_runtime_error(runtime: dict[str, Any], *, error: str) -> None:
    detail = str(error or "").strip()
    if detail:
        runtime["error"] = {"message": detail, "kind": "gateway_request_failed"}


def with_review_runtime(
    payload: dict[str, Any],
    *,
    mode: str,
    real_requested: bool,
    real_required: bool,
    gateway: dict[str, str],
    fallback_used: bool = False,
    error: str = "",
    bundle_path: str = "",
) -> dict[str, Any]:
    normalized = dict(payload or {})
    runtime = review_runtime_payload(
        mode=mode,
        real_requested=real_requested,
        real_required=real_required,
        gateway=gateway,
        fallback_used=fallback_used,
        error=error,
    )
    if bundle_path:
        runtime["review_bundle_path"] = bundle_path
    normalized["review_runtime"] = runtime
    if "source" not in normalized:
        normalized["source"] = "kodawari"
    # No-fake-run policy Fix 1: apply the P1.6 score-gap demote NOW that
    # the reviewer mode is known. The check used to live inline in
    # normalize_review_payload but was dead — review_runtime is attached
    # by THIS wrapper, after normalize ran. Doing the flip here puts the
    # REAL_REVIEW_MODES guard on real ground: simulated/fake reviewers
    # cannot silent-flip approved=false → true.
    from kodawari.autopilot.review.opus_gateway import apply_score_gap_demote_if_real
    normalized = apply_score_gap_demote_if_real(normalized, mode=mode)
    return normalized


def review_runtime_gateway(
    *,
    backend: str,
    reviewer_model: str,
    reviewer_base_url: str,
    opus_gateway_base_url: str,
    reviewer_api_format: str,
    opus_gateway_model: str,
    opus_gateway_api_format: str,
) -> dict[str, str]:
    if backend in {"cli", "mcp", "codex"}:
        return {"backend": backend, "model": str(reviewer_model or "").strip()}
    return {
        "backend": "api",
        "base_url": str(reviewer_base_url or opus_gateway_base_url or "").strip(),
        "model": str(reviewer_model or opus_gateway_model or "").strip(),
        "api_format": str(reviewer_api_format or opus_gateway_api_format or "").strip(),
    }


def failed_real_peer_review(
    *,
    error: str,
    real_required: bool,
    gateway: dict[str, str],
) -> dict[str, Any]:
    detail = str(error or "unknown gateway error")
    label = "required" if real_required else "requested"
    return with_review_runtime(
        {
            "approved": False,
            "summary": f"Real peer review {label} but failed: {detail}",
            "must_fix": [f"Real peer review {label} but unavailable: {detail}"],
            "should_fix": [],
            "blocking_items": [f"Real peer review {label} but unavailable: {detail}"],
            "severity": "critical",
            "score": 0,
            "target_score": 95,
            "min_dimension_score": 80,
            "gate_recommendation": "REVIEW_FIX_REQUIRED",
            "reviewer": "opus",
            "source": f"kodawari.real_peer_review_{label}",
        },
        mode=f"real_{label}_failed",
        real_requested=True,
        real_required=real_required,
        gateway=gateway,
        fallback_used=False,
        error=detail,
    )


def needs_test_updates(changed_files: list[str], review_iteration: int) -> bool:
    has_test_change = any(is_test_file(item) for item in changed_files)
    return review_iteration == 0 and not has_test_change


def has_verified_scoped_test_evidence(context: dict[str, Any]) -> bool:
    project_root_text = str(context.get("project_root") or "").strip()
    if not project_root_text:
        return False
    evidence = resolve_verified_test_evidence(
        project_root=Path(project_root_text),
        task_card_files=[str(item) for item in list(context.get("task_card_files") or []) if str(item).strip()],
        runtime_verify_check=dict(context.get("runtime_verify_check") or {}),
    )
    return bool(evidence)


def task_scope_allows_test_updates(context: dict[str, Any]) -> bool:
    task_card_files = [str(item) for item in list(context.get("task_card_files") or []) if str(item).strip()]
    if not task_card_files:
        return True
    return any(is_test_file(item) for item in task_card_files)


def static_review(
    *,
    approved: bool,
    summary: str,
    must_fix: list[str],
    should_fix: list[str] | None = None,
    severity: str,
    score: int,
    gate_recommendation: str,
) -> dict[str, Any]:
    return {
        "approved": approved,
        "summary": summary,
        "must_fix": must_fix,
        "should_fix": should_fix or [],
        "blocking_items": list(must_fix) if not approved else [],
        "severity": severity,
        "score": score,
        "target_score": 95,
        "min_dimension_score": 80,
        "gate_recommendation": gate_recommendation,
        "reviewer": "opus",
    }


def review_no_changes() -> dict[str, Any]:
    msg = "Must fix: produce concrete code changes before review"
    return static_review(
        approved=False,
        summary="No changed files detected.",
        must_fix=[msg],
        severity="critical",
        score=40,
        gate_recommendation="REVIEW_FIX_REQUIRED",
    )


def review_missing_tests() -> dict[str, Any]:
    msg = "Must fix: add scoped tests for changed files"
    return static_review(
        approved=False,
        summary="Missing test updates for first review pass.",
        must_fix=[msg],
        should_fix=["Document verify command used"],
        severity="high",
        score=76,
        gate_recommendation="REVIEW_FIX_REQUIRED",
    )


def review_test_scope_conflict(changed_files: list[str]) -> dict[str, Any]:
    files = [str(item) for item in changed_files if str(item).strip()]
    # Docs-first task split short-circuit (mirrors apply_deterministic_review_guard).
    # When every changed file is a docs/markdown artifact, the scoped-test
    # requirement is advisory and deferred to a downstream code task in the
    # same TASK_GRAPH. Use ``review_approved`` semantics with a marker so
    # observability can tell this approval apart from a regular pass.
    if files and all(is_docs_only_path(item) for item in files):
        payload = static_review(
            approved=True,
            summary="Docs-only task: scoped-test requirement deferred to a downstream code task.",
            must_fix=[],
            should_fix=[
                "Ensure the TASK_GRAPH includes a downstream task that adds the test coverage."
            ],
            severity="info",
            score=92,
            gate_recommendation="PROCEED_TO_GATE",
        )
        payload["docs_only_proceed"] = True
        return payload
    reason = (
        "scoped tests are required for "
        + ", ".join(files)
        + " but current task scope does not include any test files; widen files_to_change or add a follow-up test task"
    )
    payload = static_review(
        approved=False,
        summary="Review found missing scoped tests, but the current task scope cannot add them.",
        must_fix=[],
        should_fix=["Replan or widen the task scope before requiring scoped test updates."],
        severity="high",
        score=72,
        gate_recommendation="REVIEW_SCOPE_CONFLICT",
    )
    payload["blocking_reason"] = reason
    payload["blocking_items"] = [reason]
    return payload


def review_approved() -> dict[str, Any]:
    return static_review(
        approved=True,
        summary="Review approved for proceed-to-gate handoff.",
        must_fix=[],
        severity="low",
        score=98,
        gate_recommendation="PROCEED_TO_GATE",
    )


def review_verification_only_approved() -> dict[str, Any]:
    return static_review(
        approved=True,
        summary="Verification-only task approved after scoped verify with no code changes required.",
        must_fix=[],
        severity="low",
        score=98,
        gate_recommendation="PROCEED_TO_GATE",
    )


def simulate_review_payload(
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
) -> dict[str, Any]:
    if not changed_files:
        task_card = context.get("task_card")
        if is_verification_only_task(context, task_card if isinstance(task_card, dict) else None):
            return review_verification_only_approved()
        return review_no_changes()
    if needs_test_updates(changed_files, review_iteration) and not has_verified_scoped_test_evidence(context):
        if not task_scope_allows_test_updates(context):
            return review_test_scope_conflict(changed_files)
        return review_missing_tests()
    return review_approved()

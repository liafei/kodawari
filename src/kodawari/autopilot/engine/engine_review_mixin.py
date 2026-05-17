"""Review and proceed-gate helpers for the autopilot engine."""
from __future__ import annotations
import logging
import os
from typing import Any


class ReviewerUnavailableError(RuntimeError):
    """Raised when production code requires a real reviewer but no adapter
    is available. Lets the engine fail loudly instead of silently falling
    back to a simulated review payload (no-fake-run policy Fix 3)."""


def _bool_env(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _is_test_environment() -> bool:
    """Detect pytest/test-mode invocations so simulated review payloads
    remain available to unit tests. Drops the original ``"pytest" in
    sys.modules`` check (per sub-agent review): VS Code's Python test
    explorer, tox, nox, and coverage all leave pytest importable in
    long-lived shells, accidentally tripping the flag in production-like
    sessions. Now we trust only explicit signals: pytest's own current-test
    env var and the kodawari test-mode opt-in."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if _bool_env("WORKFLOW_SDK_TEST_MODE"):
        return True
    return False

logger = logging.getLogger(__name__)

from kodawari.autopilot.core.collaboration import (
    ArchitectureDecision,
    CollaborationAction,
    CollaborationContext,
    CollaborationRole,
    enforce_reviewer_boundary,
    normalize_reviewer_feedback,
    record_opus_review,
    update_round_record_outcome,
)
from kodawari.autopilot.review.review_contract import resolve_review_evidence_requirements
from kodawari.autopilot.review.review_precheck import is_test_file
from kodawari.autopilot.review_runtime_policy import (
    REAL_REVIEW_MODES,
    classify_review_runtime,
    review_quality_grading_enabled,
)
from kodawari.autopilot.engine.engine_support import _LoopRuntime
from kodawari.autopilot.engine.gate_round import run_proceed_round
from kodawari.autopilot.review.review_bridge import summarize_peer_review, validate_dual_review_evidence
from kodawari.autopilot.core.state import Stage, StopReason


def _append_review_deduped(reviews: list[dict[str, Any]], review: dict[str, Any]) -> None:
    """Append a peer-review entry, skipping exact duplicates by review_iteration.

    If a review with the same ``review_iteration`` already exists in the list,
    the new entry is dropped.  This prevents double-counting on loop retries.
    """
    iteration = review.get("review_iteration")
    if iteration is not None:
        for existing in reviews:
            if existing.get("review_iteration") == iteration:
                logger.debug(
                    "evidence dedup: skipping duplicate peer_review entry "
                    "for review_iteration=%s", iteration
                )
                return
    reviews.append(dict(review))


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _merge_unique_paths(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            normalized = str(item or "").replace("\\", "/").strip()
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)
    return merged


class EngineReviewMixin:
    def _review_evidence_enforced(self, runtime: _LoopRuntime | None = None) -> bool:
        override = getattr(runtime, "config_override", None) or {}
        if "enforce_dual_review" in override:
            return bool(override["enforce_dual_review"])
        if self._contract_first_mode() == "strict":
            return True
        if bool(getattr(self.config, "require_real_peer_review", False)):
            return True
        if bool(getattr(self.config, "real_peer_review", False)):
            return True
        return False

    def _validate_proceed_review_evidence(self, runtime: _LoopRuntime) -> dict[str, Any]:
        if not self._review_evidence_enforced(runtime=runtime):
            # No-fake-run policy Fix 6: in production-strict mode
            # (WORKFLOW_REVIEW_ENABLED=1 AND non-test env), an
            # un-enforced review-evidence check used to return SKIP
            # which the proceed gate treated as PASS — silently
            # bypassing review verification entirely. Flip to FAIL so
            # the gate actually surfaces "review evidence skipped" as
            # a blocker. Subscription-mode / dev / test runs (any one
            # of which makes _no_fake_run_strict()=False) keep the
            # legacy SKIP behavior so local iteration without
            # WORKFLOW_REVIEW_ENABLED still works.
            from kodawari.autopilot.core.runtime_checks import _no_fake_run_strict
            if _no_fake_run_strict():
                return {
                    "status": "FAIL",
                    "blocking_reason": (
                        "review evidence enforcement is skipped but "
                        "production strict mode is on "
                        "(WORKFLOW_REVIEW_ENABLED=1) — opt into peer "
                        "review or unset the env var."
                    ),
                    "issues": ["review_evidence_skipped_in_strict_mode"],
                }
            return {"status": "SKIP", "blocking_reason": "", "issues": []}
        # `real_peer_review=True` means "request real review if available";
        # only `require_real_peer_review=True` should hard-block on fallback.
        require_real_peer_review = bool(getattr(self.config, "require_real_peer_review", False))
        requirements = resolve_review_evidence_requirements(
            execution_backend=str(
                dict(runtime.execution_result or {}).get("backend")
                or getattr(self.config, "executor_backend", "")
                or ""
            ).strip(),
            self_review_count=len(runtime.codex_self_reviews),
            peer_review_summary=runtime.peer_review_summary,
            peer_review_enabled=runtime.peer_review_enabled,
            require_real_peer_review=require_real_peer_review,
            default_require_self_review=True,
        )
        return validate_dual_review_evidence(
            codex_self_reviews=runtime.codex_self_reviews,
            peer_reviews=runtime.peer_reviews,
            must_fix_items=list(runtime.context.review_feedback.must_fix),
            require_real_peer_review=require_real_peer_review,
            require_self_review=bool(requirements.get("require_self_review")),
            require_peer_review=bool(requirements.get("require_peer_review")),
        )

    def _review_with_adapter(
        self,
        *,
        context: CollaborationContext,
        task_label: str,
        changed_files: list[str],
        task_scope: str | None,
        peer_review_policy: dict[str, Any],
        review_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        review_iteration = context.review_feedback.review_iteration
        adapter_feedback = self._adapter_review_feedback(
            task_label=task_label,
            task_scope=task_scope,
            changed_files=changed_files,
            review_iteration=review_iteration,
            review_context=review_context,
        )
        if adapter_feedback is None:
            adapter_feedback = self._default_review_feedback(
                changed_files=changed_files,
                peer_review_policy=peer_review_policy,
            )
        feedback = self._normalize_review_feedback(
            adapter_feedback=adapter_feedback,
            review_iteration=review_iteration + 1,
            peer_review_policy=peer_review_policy,
        )
        return enforce_reviewer_boundary(
            feedback,
            expected_reviewer=CollaborationRole.OPUS,
        )

    def _adapter_review_feedback(
        self,
        *,
        task_label: str,
        task_scope: str | None,
        changed_files: list[str],
        review_iteration: int,
        review_context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        callback = getattr(self.adapter, "review", None)
        if not callable(callback):
            return None
        context_payload = (
            dict(review_context)
            if isinstance(review_context, dict)
            else self._build_implementation_context(task_label, task_scope)
        )
        payload = callback(
            task=task_label,
            context=context_payload,
            changed_files=list(changed_files),
            review_iteration=review_iteration,
        )
        if isinstance(payload, dict):
            return payload
        return None

    def _default_review_feedback(
        self,
        *,
        changed_files: list[str],
        peer_review_policy: dict[str, Any],
    ) -> dict[str, Any]:
        # No-fake-run policy Fix 3 (gated): refuse to fabricate a review
        # payload when the operator opted in to real peer review AND we
        # are not running under pytest. Three opt-in surfaces:
        #   - env var WORKFLOW_REVIEW_ENABLED=1 (the documented switch)
        #   - engine config require_real_peer_review=True
        #   - engine config real_peer_review=True
        # Any one of those means "production wants real LLM review",
        # so a missing adapter.review() must fail loud. Subscription-mode
        # users with none of the above keep the simulated_default path so
        # kodawari's documented default (Claude subscription, no API
        # key) still works.
        review_enabled = _bool_env("WORKFLOW_REVIEW_ENABLED")
        config_requires_real = bool(
            getattr(self.config, "require_real_peer_review", False)
            or getattr(self.config, "real_peer_review", False)
        )
        if (review_enabled or config_requires_real) and not _is_test_environment():
            raise ReviewerUnavailableError(
                "real peer review is enabled (WORKFLOW_REVIEW_ENABLED=1 or "
                "config require_real_peer_review/real_peer_review=True) but "
                "the engine adapter has no .review() callable — refusing to "
                "fall back to a simulated review payload. Configure a real "
                "reviewer backend or unset the real-review opt-in."
            )
        # Simulated (deterministic) review — no real model called.
        # Log a warning so operators can distinguish simulated from real reviews,
        # and attach an honest review_runtime block so downstream gates can
        # classify this as review_quality="simulated" rather than treating it
        # as a real reviewer pass.
        logger.warning(
            "review_override: no adapter.review() available — falling back to "
            "deterministic simulated review (test coverage check only). "
            "Set a real adapter to enable model-driven review."
        )
        simulated_runtime = {
            "mode": "simulate_local",
            "source": "engine_default_review_feedback",
            "real_requested": False,
            "real_required": False,
            "fallback_used": False,
            "error": "",
            "review_quality": "simulated",
            "semantic_review_performed": False,
        }
        has_test_change = any(is_test_file(item) for item in changed_files)
        if not has_test_change:
            return {
                "approved": False,
                "summary": "Tests are missing for changed files.",
                "must_fix": ["Must fix: add at least one scoped test file for modified behavior"],
                "should_fix": ["Document verification scope"],
                "severity": "high",
                "score": 70,
                "target_score": peer_review_policy.get("target_score", 95),
                "min_dimension_score": peer_review_policy.get("min_dimension_score", 80),
                "review_source": "simulated_default",
                "review_runtime": simulated_runtime,
            }
        return {
            "approved": True,
            "summary": "Review passed with scoped implementation and tests.",
            "must_fix": [],
            "should_fix": [],
            "severity": "low",
            "score": 97,
            "target_score": peer_review_policy.get("target_score", 95),
            "min_dimension_score": peer_review_policy.get("min_dimension_score", 80),
            "review_source": "simulated_default",
            "review_runtime": simulated_runtime,
        }

    def _normalize_review_feedback(
        self,
        *,
        adapter_feedback: dict[str, Any],
        review_iteration: int,
        peer_review_policy: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(adapter_feedback)
        payload["target_score"] = int(
            payload.get("target_score") or peer_review_policy.get("target_score", 95)
        )
        payload["min_dimension_score"] = int(
            payload.get("min_dimension_score")
            or peer_review_policy.get("min_dimension_score", 80)
        )
        feedback = normalize_reviewer_feedback(
            payload,
            review_iteration=review_iteration,
        ).to_dict()
        feedback["target_score"] = int(feedback.get("target_score") or payload["target_score"])
        feedback["min_dimension_score"] = int(
            feedback.get("min_dimension_score") or payload["min_dimension_score"]
        )
        review_runtime = adapter_feedback.get("review_runtime")
        if isinstance(review_runtime, dict):
            feedback["review_runtime"] = dict(review_runtime)
        blocking_reason = str(adapter_feedback.get("blocking_reason") or "").strip()
        if blocking_reason:
            feedback["blocking_reason"] = blocking_reason
        return feedback

    def _preflight_peer_review(self, runtime: _LoopRuntime) -> dict[str, Any] | None:
        callback = getattr(self.adapter, "peer_review_preflight", None)
        if not callable(callback):
            return None
        payload = callback(
            task=runtime.task_label,
            context=self._build_implementation_context(runtime.task_label, runtime.task_scope),
        )
        if not isinstance(payload, dict):
            return None
        if bool(payload.get("ready", True)):
            return None
        review = dict(payload.get("review") or {})
        blocking_error = str(
            payload.get("blocking_error")
            or self._review_blocking_error(review)
            or "Real peer review did not complete"
        ).strip()
        action = CollaborationAction.PEER_REVIEW
        round_record = self._new_round_record(runtime, action)
        self._maybe_emit_hook(
            runtime.hook_events,
            event="pre_review",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
        )
        self.state.current_stage = Stage.PLAN_REVIEW
        self._apply_opus_review(runtime.context, review)
        _append_review_deduped(runtime.peer_reviews, review)
        runtime.peer_review_summary = self._update_peer_review_summary(runtime, review)
        return self._blocked_opus_review_result(
            runtime=runtime,
            action=action,
            round_record=round_record,
            review=review,
            blocking_error=blocking_error,
        )

    def _run_opus_design_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        self._maybe_emit_hook(
            runtime.hook_events,
            event="pre_plan",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
        )
        self.state.current_stage = Stage.PLAN_REVIEW
        decision = self._generate_design_decision(runtime.context)
        runtime.context.architecture_decisions.append(decision)
        self.state.add_architecture_decision(decision)
        runtime.context.assigned_role = CollaborationRole.CODEX
        self.state.last_stage_status = "design_ready"
        round_record["stage_status"] = "ok"
        round_record["details"] = {"decision_id": decision.decision_id}
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        self._maybe_emit_hook(
            runtime.hook_events,
            event="post_plan",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={"decision_id": decision.decision_id},
        )
        return None

    def _run_proceed_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any]:
        # run_proceed_round performs the single authoritative evidence check and
        # emits the post_review hook on FAIL.  Do not re-validate here — that
        # caused _validate_proceed_review_evidence to be called twice on every
        # PASS path (once here, once inside run_proceed_round).
        return run_proceed_round(
            self,
            runtime=runtime,
            action=action,
            round_record=round_record,
        )

    def _run_opus_review_round(
        self,
        *,
        runtime: _LoopRuntime,
        action: CollaborationAction,
        round_record: dict[str, Any],
    ) -> dict[str, Any] | None:
        self._maybe_emit_hook(
            runtime.hook_events,
            event="pre_review",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
        )
        self.state.current_stage = Stage.PLAN_REVIEW
        review_context = self._build_implementation_context(runtime.task_label, runtime.task_scope)
        if isinstance(runtime.verify_check, dict):
            review_context["runtime_verify_check"] = dict(runtime.verify_check)
        if isinstance(runtime.gate_check, dict):
            review_context["runtime_gate_check"] = dict(runtime.gate_check)
        if isinstance(runtime.execution_result, dict):
            review_context["runtime_execution_result"] = dict(runtime.execution_result)
            self._apply_runtime_execution_scope_to_review_context(
                review_context,
                runtime.execution_result,
            )
        review = self._review_with_adapter(
            context=runtime.context,
            task_label=runtime.task_label,
            changed_files=runtime.last_changed_files,
            task_scope=runtime.task_scope,
            peer_review_policy=runtime.peer_review_policy,
            review_context=review_context,
        )
        self._apply_opus_review(runtime.context, review)
        _append_review_deduped(runtime.peer_reviews, review)
        runtime.peer_review_summary = self._update_peer_review_summary(runtime, review)
        blocking_error = self._review_blocking_error(review)
        if blocking_error:
            return self._blocked_opus_review_result(
                runtime,
                action=action,
                round_record=round_record,
                review=review,
                blocking_error=blocking_error,
            )
        approved = bool(review.get("approved", False))
        review_round = int(review.get("review_iteration", runtime.context.review_feedback.review_iteration))
        self.state.last_stage_status = {True: "review_passed", False: "changes_requested"}[approved]
        round_record["stage_status"] = {True: "pass", False: "changes_requested"}[approved]
        round_record["review_round"] = review_round
        round_record["details"] = {
            **review,
            "peer_review_summary": runtime.peer_review_summary,
        }
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        self._maybe_emit_hook(
            runtime.hook_events,
            event="post_review",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={"approved": approved, "severity": str(review.get("severity") or ""), "blocking_items": list(map(str, review.get("blocking_items", []))), "review_round": review_round, "must_fix_remaining": len(runtime.context.review_feedback.must_fix), "gate_recommendation": runtime.context.review_feedback.gate_recommendation},
        )
        if not approved and review_round >= 2:
            self._refresh_semantic_compact(
                runtime,
                reason="REVIEW_FAILED_THRESHOLD",
                trigger_event="review_fail_threshold",
                mode="incremental",
            )
        return None

    def _apply_runtime_execution_scope_to_review_context(
        self,
        review_context: dict[str, Any],
        execution_result: dict[str, Any],
    ) -> None:
        manifest = execution_result.get("tool_manifest")
        if not isinstance(manifest, dict):
            return
        allowed_files = _clean_string_list(manifest.get("allowed_files"))
        if not allowed_files:
            return
        read_only_tests = [path for path in _clean_string_list(manifest.get("read_only_files")) if is_test_file(path)]
        task_card = dict(review_context.get("task_card") or {})
        original_task_card = getattr(self, "_task_card_payload", None)
        original_scope_files = _clean_string_list(
            original_task_card.get("files_to_change") if isinstance(original_task_card, dict) else []
        )
        recovery = task_card.get("recovery")
        recovery_base_files = _clean_string_list(
            recovery.get("base_files_to_change") if isinstance(recovery, dict) else []
        )
        existing_scope_files = _merge_unique_paths(
            original_scope_files,
            _clean_string_list(review_context.get("task_card_files")),
            _clean_string_list(task_card.get("files_to_change")),
            recovery_base_files,
        )
        review_scope_files = _merge_unique_paths(existing_scope_files, allowed_files, read_only_tests)
        review_context["task_card_files"] = review_scope_files
        review_context["runtime_execution_scope_files"] = allowed_files
        if read_only_tests or review_scope_files != allowed_files:
            review_context["runtime_review_scope_files"] = review_scope_files
        task_card["files_to_change"] = _merge_unique_paths(
            original_scope_files,
            _clean_string_list(task_card.get("files_to_change")),
            recovery_base_files,
            allowed_files,
        )
        if read_only_tests:
            task_card["related_existing_tests"] = _merge_unique_paths(
                _clean_string_list(task_card.get("related_existing_tests")),
                read_only_tests,
            )
        task_card.setdefault("recovery", {})
        if isinstance(task_card["recovery"], dict):
            task_card["recovery"]["scope_source"] = "execution_tool_manifest.allowed_files"
        review_context["task_card"] = task_card

    def _apply_opus_review(self, context: CollaborationContext, review: dict[str, Any]) -> None:
        record_opus_review(
            context,
            approved=bool(review.get("approved", False)),
            summary=str(review.get("summary") or ""),
            must_fix=[str(item) for item in review.get("must_fix", [])],
            should_fix=[str(item) for item in review.get("should_fix", [])],
            blocking_items=[str(item) for item in review.get("blocking_items", [])],
            severity=str(review.get("severity") or ""),
            score=review.get("score"),
            target_score=review.get("target_score"),
            min_dimension_score=review.get("min_dimension_score"),
            dimension_scores=review.get("dimension_scores"),
            gate_recommendation=review.get("gate_recommendation"),
            global_consistency_verdict=review.get("global_consistency_verdict"),
            local_implementation_verdict=review.get("local_implementation_verdict"),
            global_failure_attribution=review.get("global_failure_attribution"),
            deterministic_finding_responses=review.get("deterministic_finding_responses"),
            evidence_refs=review.get("evidence_refs"),
        )

    def _update_peer_review_summary(
        self,
        runtime: _LoopRuntime,
        review: dict[str, Any],
    ) -> dict[str, Any]:
        summary = summarize_peer_review(
            self.config.feature,
            runtime.peer_reviews,
            require_real_peer_review=bool(getattr(self.config, "require_real_peer_review", False)),
        )
        feedback = runtime.context.review_feedback
        summary.update(
            {
                "approved": bool(feedback.approved),
                "review_round": int(feedback.review_iteration),
                "must_fix_remaining": len(feedback.must_fix),
                "last_gate_recommendation": str(feedback.gate_recommendation or review.get("gate_recommendation") or ""),
                "global_consistency_verdict": str(
                    feedback.global_consistency_verdict or review.get("global_consistency_verdict") or ""
                ),
                "local_implementation_verdict": str(
                    feedback.local_implementation_verdict or review.get("local_implementation_verdict") or ""
                ),
            }
        )
        return summary

    def _blocked_opus_review_result(
        self,
        runtime: _LoopRuntime,
        *,
        action: CollaborationAction,
        round_record: dict[str, Any],
        review: dict[str, Any],
        blocking_error: str,
    ) -> dict[str, Any]:
        approved = bool(review.get("approved", False))
        review_round = int(review.get("review_iteration", runtime.context.review_feedback.review_iteration))
        review_runtime = dict(review.get("review_runtime") or {})
        external_gateway = bool(review_runtime.get("real_requested"))
        self.state.add_error(
            blocking_error,
            phase=Stage.PLAN_REVIEW.value,
            action=action.value,
            category="external_gateway" if external_gateway else "review",
            error_code="REVIEW_GATEWAY_BLOCKED" if external_gateway else "",
        )
        self.state.mark_completed(StopReason.HARD_ERROR, "BLOCKED")
        round_record["stage_status"] = "blocked"
        round_record["last_error"] = blocking_error
        round_record["review_round"] = review_round
        round_record["details"] = {
            **review,
            "peer_review_summary": runtime.peer_review_summary,
        }
        runtime.round_records.append(update_round_record_outcome(round_record, runtime.context))
        self._maybe_emit_hook(
            runtime.hook_events,
            event="post_review",
            task_id=runtime.task_id,
            task_label=runtime.task_label,
            action=action,
            task_scope=runtime.task_scope,
            details={"approved": approved, "severity": str(review.get("severity") or ""), "blocking_items": list(map(str, review.get("blocking_items", []))), "review_round": review_round, "must_fix_remaining": len(runtime.context.review_feedback.must_fix), "gate_recommendation": runtime.context.review_feedback.gate_recommendation},
        )
        return self._finish_loop(
            runtime,
            reason="OPUS_REVIEW_BLOCKED",
            last_error=blocking_error,
            action=action,
        )

    def _review_blocking_error(self, review: dict[str, Any]) -> str:
        explicit = str(review.get("blocking_reason") or "").strip()
        if explicit:
            return explicit
        runtime = dict(review.get("review_runtime") or {})
        if not runtime:
            return ""
        require_real = bool(getattr(self.config, "require_real_peer_review", False))
        if not review_quality_grading_enabled():
            # Legacy fallback path (kept for emergency rollback via env flag).
            real_requested = bool(runtime.get("real_requested"))
            if not real_requested:
                return ""
            mode = str(runtime.get("mode") or "").strip()
            if mode in REAL_REVIEW_MODES:
                return ""
            if bool(runtime.get("fallback_used")) and not require_real:
                return ""
            return self._review_runtime_error_message(runtime) or "Real peer review did not complete"
        runtime_classification = classify_review_runtime(
            runtime,
            require_real_peer_review=require_real,
        )
        if not runtime_classification.real_requested:
            return ""
        if runtime_classification.is_real_review:
            return ""
        if not require_real and runtime_classification.review_quality == "degraded":
            # No-fake-run policy Fix 9: when require_real_peer_review=False,
            # historically we accepted the simulated fallback without blocking
            # (so transient HTTP/timeout errors didn't halt dev iteration).
            # In production strict mode (_no_fake_run_strict()=True), this
            # silent acceptance is a fake-pass slip — the orchestrator's
            # natural round loop already provides retry semantics (next
            # round re-invokes the reviewer), so blocking proceed here lets
            # the operator see the transient failure instead of silently
            # passing on degraded evidence. Dev / subscription / test runs
            # keep the legacy accept-fallback behavior.
            from kodawari.autopilot.core.runtime_checks import _no_fake_run_strict
            if _no_fake_run_strict():
                return self._review_runtime_error_message(runtime) or (
                    "Real peer review degraded to simulated fallback in "
                    "production strict mode — retry the reviewer or unset "
                    "WORKFLOW_REVIEW_ENABLED to opt out."
                )
            # Reviewer timed out / fell back to simulation but review is not
            # hard-required — accept the fallback result without blocking.
            return ""
        return self._review_runtime_error_message(runtime) or "Real peer review did not complete"

    def _review_runtime_error_message(self, runtime: dict[str, Any]) -> str:
        error = runtime.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                return message
        return str(error or "").strip()



__all__ = ["EngineReviewMixin"]


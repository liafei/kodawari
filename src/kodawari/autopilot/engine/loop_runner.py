"""Loop runner helpers extracted from the autopilot engine."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from kodawari.autopilot.core.collaboration import (
    CollaborationAction,
    CollaborationRole,
    review_round_limit_reached,
)
from kodawari.autopilot.core.state import Stage, StopReason
from kodawari.autopilot.core.task_modes import is_verification_only_task

logger = logging.getLogger(__name__)

UNPRODUCTIVE_FIX_ROUND_LIMIT = 2
UNPRODUCTIVE_FIX_ROUND_LIMIT_ENV = "WORKFLOW_UNPRODUCTIVE_FIX_ROUND_LIMIT"
UNPRODUCTIVE_FIX_ROUND_RUNTIME_CAP = "max_unproductive_fix_rounds"

# Reviewer-drift detector: bumping LITE_LANE.review_max_rounds gives the
# executor more chances to fix what reviewer flagged, but unbounded extra
# rounds risk the reviewer "moving the goalposts" — raising different
# must_fix items each round so the loop never converges. Bound that by
# tracking the must_fix signature across review rounds: if the reviewer
# raises a *different* signature for ``REVIEWER_DRIFT_LIMIT`` consecutive
# rounds (i.e. each round's blockers are new, not lingering from the prior
# round), terminate as REVIEWER_DRIFT_DETECTED rather than burning the
# round budget on a moving target.
REVIEWER_DRIFT_LIMIT = 2
REVIEWER_DRIFT_LIMIT_ENV = "WORKFLOW_REVIEWER_DRIFT_LIMIT"
REVIEWER_DRIFT_LIMIT_RUNTIME_CAP = "max_reviewer_drift_rounds"

_FIX_ROUND_ACTIONS = frozenset({CollaborationAction.FIX_ROUND, CollaborationAction.CODEX_FIX})
_PEER_REVIEW_ACTIONS = frozenset({CollaborationAction.PEER_REVIEW, CollaborationAction.OPUS_REVIEW})


def run_single_pass_loop(
    engine: Any,
    *,
    task_label: str,
    task_scope: str | None,
    max_rounds: int | None = None,
    actions_override: tuple[CollaborationAction, ...] | None = None,
) -> dict[str, Any]:
    runtime = _single_pass_runtime(
        engine,
        task_label=task_label,
        task_scope=task_scope,
        max_rounds=max_rounds,
    )
    actions = actions_override if actions_override is not None else _single_pass_actions()
    for action in actions:
        result = _dispatch_action(engine, runtime, action)
        if result is not None:
            return result
        recovery_result = _maybe_run_single_pass_executor_recovery(engine, runtime, action)
        if recovery_result is not None:
            return recovery_result
    return engine._finish_loop(runtime, reason="PROCEED_TO_GATE")


def run_peer_review_loop(
    engine: Any,
    *,
    task_label: str,
    task_scope: str | None,
    max_rounds: int | None = None,
    loop_config_override: Any | None = None,
) -> dict[str, Any]:
    runtime = engine._create_loop_runtime(
        task_label=task_label,
        task_scope=task_scope,
        max_rounds=max_rounds,
        enable_peer_review=True,
    )
    if loop_config_override is not None:
        _apply_loop_config_override(engine, runtime, loop_config_override)
    engine._start_loop_session(runtime)
    preflight_result = engine._preflight_peer_review(runtime)
    if preflight_result is not None:
        return preflight_result
    rounds_limit = _rounds_limit(runtime)
    unproductive_fix_round_limit = _unproductive_fix_round_limit(engine)
    reviewer_drift_limit = _reviewer_drift_limit(engine)
    consecutive_zero_write_fix_rounds = 0
    consecutive_drift_rounds = 0
    previous_must_fix_bag: frozenset[str] | None = None
    while True:
        action = runtime.context.next_action()
        watch_fix_round = action in _FIX_ROUND_ACTIONS
        watch_peer_review = action in _PEER_REVIEW_ACTIONS
        round_result = _dispatch_action(engine, runtime, action)
        if round_result is not None:
            return round_result
        if watch_fix_round:
            if _latest_round_requested_executor_recovery(runtime):
                consecutive_zero_write_fix_rounds = 0
            elif _latest_fix_round_had_write_event(runtime):
                consecutive_zero_write_fix_rounds = 0
            elif _is_verification_only_context(engine):
                # Verification-only tasks intentionally make no file changes;
                # zero write events are expected and must not trigger the limit.
                consecutive_zero_write_fix_rounds = 0
            else:
                consecutive_zero_write_fix_rounds += 1
                if consecutive_zero_write_fix_rounds >= unproductive_fix_round_limit:
                    return _unproductive_fix_round_result(engine, runtime, action, consecutive_zero_write_fix_rounds)
        if watch_peer_review:
            current_bag = _peer_review_must_fix_token_bag(runtime)
            if _must_fix_signatures_drifted(previous_must_fix_bag, current_bag):
                consecutive_drift_rounds += 1
            else:
                # Empty current bag (reviewer approved), or high overlap
                # with prior round (stuck on same topics, not drifting),
                # or first peer-review round → reset.
                consecutive_drift_rounds = 0
            previous_must_fix_bag = current_bag
            if consecutive_drift_rounds >= reviewer_drift_limit:
                return _reviewer_drift_result(engine, runtime, action, consecutive_drift_rounds)
        if _round_limit_reached(action=action, context=runtime.context, rounds_limit=rounds_limit):
            return _round_limit_result(engine, runtime, action, rounds_limit)


def _single_pass_runtime(
    engine: Any,
    *,
    task_label: str,
    task_scope: str | None,
    max_rounds: int | None,
) -> Any:
    runtime = engine._create_loop_runtime(
        task_label=task_label,
        task_scope=task_scope,
        max_rounds=max_rounds,
        enable_peer_review=False,
    )
    runtime.context.assigned_role = CollaborationRole.CODEX
    runtime.peer_review_summary.update({"enabled": False, "skipped": True})
    engine._start_loop_session(runtime)
    return runtime


def _single_pass_actions() -> tuple[CollaborationAction, ...]:
    return (
        CollaborationAction.DESIGN,
        CollaborationAction.IMPLEMENT,
        CollaborationAction.VERIFY,
        CollaborationAction.RULES_GATE,
        CollaborationAction.PROCEED_TO_GATE,
        # Legacy aliases so existing state files continue to work
        CollaborationAction.OPUS_DESIGN,
        CollaborationAction.CODEX_IMPLEMENT,
    )


def _maybe_run_single_pass_executor_recovery(
    engine: Any,
    runtime: Any,
    action: CollaborationAction,
) -> dict[str, Any] | None:
    if action not in {CollaborationAction.IMPLEMENT, CollaborationAction.CODEX_IMPLEMENT}:
        return None
    if not _single_pass_executor_recovery_pending(engine, runtime):
        return None
    max_attempts = _single_pass_recovery_attempt_limit(engine)
    attempts = 0
    while _single_pass_executor_recovery_pending(engine, runtime) and attempts < max_attempts:
        attempts += 1
        result = _dispatch_action(engine, runtime, CollaborationAction.CODEX_FIX)
        if result is not None:
            return result
    if _single_pass_executor_recovery_pending(engine, runtime):
        message = str(getattr(engine.state, "last_error", "") or "Executor recovery required")
        engine.state.mark_completed(StopReason.STUCK, "BLOCKED")
        engine.state.last_stage_status = "executor_recovery_required"
        return engine._finish_loop(
            runtime,
            reason="EXECUTOR_RECOVERY_REQUIRED",
            last_error=message,
            action=CollaborationAction.CODEX_FIX,
        )
    return None


def _single_pass_executor_recovery_pending(engine: Any, runtime: Any) -> bool:
    if str(getattr(engine.state, "last_stage_status", "") or "") != "executor_recovery_requested":
        return False
    feedback = getattr(runtime.context, "review_feedback", None)
    must_fix = getattr(feedback, "must_fix", []) if feedback is not None else []
    return any(str(item).strip() for item in list(must_fix or []))


def _single_pass_recovery_attempt_limit(engine: Any) -> int:
    callback = getattr(engine, "_executor_recovery_attempt_limit", None)
    if callable(callback):
        try:
            return max(1, int(callback()))
        except Exception:
            logger.debug("failed to resolve executor recovery attempt limit", exc_info=True)
    return 2


def _dispatch_action(engine: Any, runtime: Any, action: CollaborationAction) -> dict[str, Any] | None:
    engine.state.cycle += _cycle_cost(action)
    _prepare_action(runtime, action)
    round_record = engine._new_round_record(runtime, action)
    max_cycle_result = engine._handle_max_cycles(
        runtime,
        action=action,
        round_record=round_record,
    )
    if max_cycle_result is not None:
        return max_cycle_result
    return engine._dispatch_round_action(
        runtime,
        action=action,
        round_record=round_record,
    )


def _cycle_cost(action: CollaborationAction) -> int:
    if action in {
        CollaborationAction.DESIGN,
        CollaborationAction.PEER_REVIEW,
        CollaborationAction.SELF_REVIEW,
        CollaborationAction.VERIFY,
        CollaborationAction.RULES_GATE,
        CollaborationAction.PROCEED_TO_GATE,
        # Legacy aliases
        CollaborationAction.OPUS_DESIGN,
        CollaborationAction.OPUS_REVIEW,
        CollaborationAction.CODEX_SELF_REVIEW,
    }:
        return 0
    return 1


def _prepare_action(runtime: Any, action: CollaborationAction) -> None:
    if action != CollaborationAction.PROCEED_TO_GATE:
        return
    runtime.context.review_feedback.gate_recommendation = "PROCEED_TO_GATE"
    # No-fake-run policy Fix 4 interaction: summarize_peer_review now sets
    # approved=False explicitly when no peer reviews ran, so the historical
    # setdefault("approved", True) became a no-op and silently changed
    # PROCEED_TO_GATE semantics. Preserve the legacy force-True ONLY when
    # peer review was not enabled for this round (engine legitimately
    # advances without requiring reviewer consensus). When peer review IS
    # enabled, trust the summary's actual approved value.
    peer_review_enabled = bool(getattr(runtime, "peer_review_enabled", False))
    if not peer_review_enabled:
        runtime.peer_review_summary["approved"] = True


def _rounds_limit(runtime: Any) -> int:
    return max(1, int(runtime.peer_review_policy.get("max_rounds", 1) or 1))


def _round_limit_reached(*, action: CollaborationAction, context: Any, rounds_limit: int) -> bool:
    return review_round_limit_reached(
        action=action,
        context=context,
        rounds_limit=rounds_limit,
    )


def _round_limit_result(engine: Any, runtime: Any, action: CollaborationAction, rounds_limit: int) -> dict[str, Any]:
    logger.warning("peer review round limit reached: %s", rounds_limit)
    engine.state.add_error(
        f"Reached review round limit ({rounds_limit})",
        phase=Stage.PLAN_REVIEW.value,
        action=action.value,
        category="review",
    )
    engine.state.mark_completed(StopReason.STUCK, "BLOCKED")
    engine.state.last_stage_status = "round_limit"
    return engine._finish_loop(
        runtime,
        reason="COLLABORATION_ROUND_LIMIT",
        last_error=engine.state.last_error,
        action=action,
    )


def _latest_round_record(runtime: Any) -> dict[str, Any]:
    records = list(getattr(runtime, "round_records", None) or [])
    if not records:
        return {}
    latest = records[-1]
    return dict(latest) if isinstance(latest, dict) else {}


def _path_list_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return len([str(item).strip() for item in value if str(item).strip()])


def _latest_round_write_event_count(runtime: Any) -> int:
    latest = _latest_round_record(runtime)
    details = latest.get("details")
    if not isinstance(details, dict):
        return 0
    changes_count = _path_list_count(details.get("changes"))
    if changes_count:
        return changes_count
    execution_result = details.get("execution_result")
    if not isinstance(execution_result, dict):
        return 0
    return max(
        _path_list_count(execution_result.get("changed_files")),
        _path_list_count(execution_result.get("changes")),
    )


def _latest_fix_round_had_write_event(runtime: Any) -> bool:
    return _latest_round_write_event_count(runtime) > 0


def _is_verification_only_context(engine: Any) -> bool:
    card = getattr(engine, "_task_card_payload", None)
    if not isinstance(card, dict):
        return False
    return is_verification_only_task(card)


def _latest_round_requested_executor_recovery(runtime: Any) -> bool:
    latest = _latest_round_record(runtime)
    if not latest:
        return False
    if str(latest.get("stage_status") or "") == "needs_recovery":
        return True
    details = latest.get("details")
    if not isinstance(details, dict):
        return False
    recovery = details.get("recovery")
    return isinstance(recovery, dict) and bool(recovery.get("requested"))


def _unproductive_fix_round_limit(engine: Any) -> int:
    env_limit = _positive_int(os.getenv(UNPRODUCTIVE_FIX_ROUND_LIMIT_ENV))
    if env_limit is not None:
        return env_limit
    for runtime_caps in _runtime_cap_sources(engine):
        cap_limit = _positive_int(runtime_caps.get(UNPRODUCTIVE_FIX_ROUND_RUNTIME_CAP))
        if cap_limit is not None:
            return cap_limit
    return UNPRODUCTIVE_FIX_ROUND_LIMIT


def _reviewer_drift_limit(engine: Any) -> int:
    env_limit = _positive_int(os.getenv(REVIEWER_DRIFT_LIMIT_ENV))
    if env_limit is not None:
        return env_limit
    for runtime_caps in _runtime_cap_sources(engine):
        cap_limit = _positive_int(runtime_caps.get(REVIEWER_DRIFT_LIMIT_RUNTIME_CAP))
        if cap_limit is not None:
            return cap_limit
    return REVIEWER_DRIFT_LIMIT


_SIGNATURE_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "for", "to", "of", "in", "on", "at", "by", "with",
    "from", "as", "into", "than", "that", "this", "these", "those",
    "it", "its", "their", "your", "our", "you", "we", "they",
    "must", "should", "can", "may", "might", "shall", "will",
    "do", "does", "did", "have", "has", "had",
    "fix", "add", "make", "ensure", "use", "include",  # generic verbs
})

_SIGNATURE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _peer_review_must_fix_token_bag(runtime: Any) -> frozenset[str]:
    """Collapse the reviewer's must_fix list into a content-token bag.

    Two rounds whose must_fix entries describe the same blocking issues
    should produce overlapping token bags even if the reviewer rephrases.
    The drift detector compares bags via Jaccard similarity (see
    ``_must_fix_signatures_drifted``) so superficial rewording does not
    trip drift on its own — only a genuine topic shift does.
    Returns ``frozenset()`` when there is no must_fix this round, which
    the caller treats as "no drift signal" and uses to reset the streak.
    """
    feedback = getattr(runtime.context, "review_feedback", None)
    if feedback is None:
        return frozenset()
    raw = list(getattr(feedback, "must_fix", None) or [])
    tokens: set[str] = set()
    for item in raw:
        text = str(item or "").lower()
        for match in _SIGNATURE_TOKEN_RE.findall(text):
            if len(match) <= 2:
                continue
            if match in _SIGNATURE_STOPWORDS:
                continue
            tokens.add(match)
    return frozenset(tokens)


# Threshold for "same topic" — Jaccard similarity ≥ this counts as the
# reviewer revisiting the same issues (stuck), not goalpost-moving (drift).
# Sub-agent review (2026-05-10) settled on 0.5: empirically a one-word
# rephrase keeps overlap high (~0.8) while a wholesale topic switch
# drops to ~0.2. Configurable via env for tuning.
_DRIFT_SIMILARITY_THRESHOLD_DEFAULT = 0.5
DRIFT_SIMILARITY_THRESHOLD_ENV = "WORKFLOW_REVIEWER_DRIFT_SIMILARITY_THRESHOLD"


def _drift_similarity_threshold() -> float:
    raw = str(os.getenv(DRIFT_SIMILARITY_THRESHOLD_ENV, "")).strip()
    if not raw:
        return _DRIFT_SIMILARITY_THRESHOLD_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return _DRIFT_SIMILARITY_THRESHOLD_DEFAULT
    return max(0.0, min(1.0, value))


def _must_fix_signatures_drifted(
    previous: frozenset[str] | None,
    current: frozenset[str],
) -> bool:
    """Return True iff the two non-empty token bags represent a topic
    shift (Jaccard < threshold). Empty bags / first round / superset
    cases are NOT drift — the caller resets the streak.

    Pure-superset case (A,B → A,B,C) yields high Jaccard (overlap is the
    whole previous set) so it correctly counts as "stuck-with-creep"
    rather than goalpost-moving. Sub-agent review pinned this behavior
    explicitly to avoid false drift triggers when reviewers find
    additional issues alongside lingering ones.
    """
    if not current or previous is None or not previous:
        return False
    union = previous | current
    if not union:
        return False
    intersection = previous & current
    similarity = len(intersection) / len(union)
    return similarity < _drift_similarity_threshold()


def _reviewer_drift_result(
    engine: Any,
    runtime: Any,
    action: CollaborationAction,
    streak: int,
) -> dict[str, Any]:
    message = (
        f"Peer reviewer raised distinct must_fix items across {streak} consecutive "
        "rounds (signatures differ each round); the executor cannot converge on a "
        "moving target — terminating as REVIEWER_DRIFT_DETECTED instead of burning "
        "the round budget."
    )
    logger.warning("reviewer drift detected: %d consecutive distinct-signature rounds", streak)
    engine.state.add_error(
        message,
        phase=Stage.PLAN_REVIEW.value,
        action=action.value,
        category="review",
    )
    engine.state.mark_completed(StopReason.STUCK, "BLOCKED")
    engine.state.last_stage_status = "reviewer_drift_detected"
    return engine._finish_loop(
        runtime,
        reason="REVIEWER_DRIFT_DETECTED",
        last_error=engine.state.last_error,
        action=action,
    )


def _runtime_cap_sources(engine: Any) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    card = getattr(engine, "_task_card_payload", None)
    if isinstance(card, dict):
        card_caps = card.get("runtime_caps")
        if isinstance(card_caps, dict):
            sources.append(card_caps)
    adapter_config = getattr(getattr(engine, "adapter", None), "config", None)
    adapter_caps = getattr(adapter_config, "executor_runtime_caps", None)
    if isinstance(adapter_caps, dict):
        sources.append(adapter_caps)
    return sources


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _unproductive_fix_round_result(
    engine: Any,
    runtime: Any,
    action: CollaborationAction,
    streak: int,
) -> dict[str, Any]:
    message = f"Fix rounds made no new file changes for {streak} consecutive attempt(s)"
    logger.warning("unproductive fix-round limit reached: %s", streak)
    engine.state.add_error(
        message,
        phase=Stage.PLAN_REVIEW.value,
        action=action.value,
        category="executor",
    )
    engine.state.mark_completed(StopReason.STUCK, "BLOCKED")
    engine.state.last_stage_status = "executor_fix_round_unproductive"
    return engine._finish_loop(
        runtime,
        reason="EXECUTOR_FIX_ROUND_UNPRODUCTIVE",
        last_error=engine.state.last_error,
        action=action,
    )


def _apply_loop_config_override(engine: Any, runtime: Any, loop_config_override: Any) -> None:
    """Apply a loop-scoped pipeline config override to runtime without mutating engine.config."""
    # Store config_override on the runtime for review mixin to read
    if hasattr(loop_config_override, "preset"):
        preset = str(loop_config_override.preset or "")
        if preset == "strict_review":
            runtime.config_override = {"enforce_dual_review": True}
        else:
            runtime.config_override = {}
    elif isinstance(loop_config_override, dict):
        runtime.config_override = dict(loop_config_override)


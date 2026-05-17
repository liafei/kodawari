"""WorkflowPolicy: the resolved execution policy that all runtime consumers read.

Architecture principle (the whole point of this module):

    NO downstream module reads `lane.name` or `complexity_decision.tier`
    directly. They read `policy.<field>`.

This indirection means lane presets / detector logic can change without
sweeping the runtime. Consumers only see the resolved policy.

Resolution order (highest priority wins):
    1. UserPolicyOverrides (explicit user/CLI flags)
    2. ComplexityDecision adjustments (e.g. risk_flags promote thresholds)
    3. LaneConfig preset defaults
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from kodawari.autopilot.planning.lane_config import HEAVY_LANE, LaneConfig

TierName = Literal["lite", "standard", "heavy"]
ALL_TIERS: tuple[TierName, ...] = ("lite", "standard", "heavy")


@dataclass(frozen=True)
class ComplexityDecision:
    """Output of the complexity detector. 5-dimensional reasoning evidence.

    Consumers MUST NOT branch on `tier` directly — go through WorkflowPolicy.
    """

    tier: TierName
    confidence: float
    source: str
    static_score: int
    hard_rule: str
    reasons: tuple[str, ...]
    risk_flags: tuple[str, ...]
    llm_used: bool
    learned_adjustments: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "confidence": self.confidence,
            "source": self.source,
            "static_score": self.static_score,
            "hard_rule": self.hard_rule,
            "reasons": list(self.reasons),
            "risk_flags": list(self.risk_flags),
            "llm_used": self.llm_used,
            "learned_adjustments": list(self.learned_adjustments),
        }


@dataclass(frozen=True)
class UserPolicyOverrides:
    """Explicit user-provided overrides that beat both lane and decision.

    Each field is None to mean "not overridden".
    """

    review_max_rounds: int | None = None
    require_self_review: bool | None = None
    task_cycle_enabled: bool | None = None
    release_tail_enabled: bool | None = None
    release_approval_required: bool | None = None
    telemetry_enabled: bool | None = None
    eval_required: bool | None = None
    artifact_profile: str | None = None

    def overridden_field_names(self) -> tuple[str, ...]:
        return tuple(
            name for name, value in self.__dict__.items() if value is not None
        )


@dataclass(frozen=True)
class WorkflowPolicy:
    """Resolved execution policy for one autopilot run.

    Resolved from (lane preset, complexity decision, user overrides).
    Downstream consumers SHOULD read fields here, not lane.name.
    """

    effective_tier: TierName

    review_max_rounds: int
    review_blocking_threshold: frozenset[str]
    require_self_review: bool

    task_cycle_enabled: bool
    parallel_runtime_enabled: bool

    release_tail_enabled: bool
    release_approval_required: bool

    telemetry_enabled: bool
    eval_required: bool

    artifact_profile: str
    suppressed_artifacts: frozenset[str]

    compact_context_enabled: bool

    decision_policy: str

    resolved_from_lane: str
    user_overrides: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_tier": self.effective_tier,
            "review_max_rounds": self.review_max_rounds,
            "review_blocking_threshold": sorted(self.review_blocking_threshold),
            "require_self_review": self.require_self_review,
            "task_cycle_enabled": self.task_cycle_enabled,
            "parallel_runtime_enabled": self.parallel_runtime_enabled,
            "release_tail_enabled": self.release_tail_enabled,
            "release_approval_required": self.release_approval_required,
            "telemetry_enabled": self.telemetry_enabled,
            "eval_required": self.eval_required,
            "artifact_profile": self.artifact_profile,
            "suppressed_artifacts": sorted(self.suppressed_artifacts),
            "compact_context_enabled": self.compact_context_enabled,
            "decision_policy": self.decision_policy,
            "resolved_from_lane": self.resolved_from_lane,
            "user_overrides": list(self.user_overrides),
        }


def resolve_workflow_policy(
    *,
    decision: ComplexityDecision,
    lane: LaneConfig,
    overrides: UserPolicyOverrides | None = None,
) -> WorkflowPolicy:
    """Resolve a WorkflowPolicy from lane preset + complexity decision + overrides.

    Decision is currently consulted only for `effective_tier`. Future
    detector adjustments (e.g. risk_flags forcing self_review on) will be
    layered here as a middle priority.
    """
    overrides = overrides or UserPolicyOverrides()

    def pick(field_name: str, lane_value):
        user_value = getattr(overrides, field_name, None)
        if user_value is not None:
            return user_value
        return lane_value

    return WorkflowPolicy(
        effective_tier=decision.tier,
        review_max_rounds=pick("review_max_rounds", lane.review_max_rounds),
        review_blocking_threshold=lane.review_blocking_threshold,
        require_self_review=pick("require_self_review", lane.require_self_review),
        task_cycle_enabled=pick("task_cycle_enabled", lane.task_cycle_enabled),
        parallel_runtime_enabled=lane.parallel_runtime_enabled,
        release_tail_enabled=pick("release_tail_enabled", lane.release_tail_enabled),
        release_approval_required=pick(
            "release_approval_required", lane.release_approval_required
        ),
        telemetry_enabled=pick("telemetry_enabled", lane.telemetry_enabled),
        eval_required=pick("eval_required", lane.eval_required),
        artifact_profile=pick("artifact_profile", lane.artifact_profile),
        suppressed_artifacts=lane.suppressed_artifacts,
        compact_context_enabled=lane.compact_context_enabled,
        decision_policy=lane.decision_policy,
        resolved_from_lane=lane.name,
        user_overrides=overrides.overridden_field_names(),
    )


def heavy_compatible_policy() -> WorkflowPolicy:
    """The default policy for back-compat with pre-tier callers.

    Use when --tier was not passed or .autopilot_state.json lacks
    'effective_tier'. Behavior matches the pre-policy autopilot.
    """
    decision = ComplexityDecision(
        tier="heavy",
        confidence=1.0,
        source="fallback",
        static_score=100,
        hard_rule="",
        reasons=("back_compat: no tier provided",),
        risk_flags=(),
        llm_used=False,
        learned_adjustments=(),
    )
    return resolve_workflow_policy(decision=decision, lane=HEAVY_LANE)


def should_emit_artifact(artifact_name: str, policy: WorkflowPolicy | None) -> bool:
    """Return True if the artifact should be written under the given policy.

    Used by C4+ artifact writers to opt out of generating outputs that the
    active lane suppresses. When `policy` is None (legacy callers without
    policy) we default to True for back-compat.
    """
    if policy is None:
        return True
    return artifact_name not in policy.suppressed_artifacts


__all__ = [
    "TierName",
    "ALL_TIERS",
    "ComplexityDecision",
    "UserPolicyOverrides",
    "WorkflowPolicy",
    "resolve_workflow_policy",
    "heavy_compatible_policy",
    "should_emit_artifact",
]


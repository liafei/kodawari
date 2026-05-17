"""Tests for WorkflowPolicy resolver and lane configurations.

Covers C1: contract layer. No runtime consumers wired yet — those come in C3+.
"""

import json

import pytest

from kodawari.autopilot.lane_config import (
    HEAVY_LANE,
    LITE_LANE,
    STANDARD_LANE,
    LaneConfig,
    lane_for,
)
from kodawari.autopilot.workflow_policy import (
    ALL_TIERS,
    ComplexityDecision,
    UserPolicyOverrides,
    WorkflowPolicy,
    heavy_compatible_policy,
    resolve_workflow_policy,
)


def _decision(tier: str = "lite", **overrides) -> ComplexityDecision:
    base: dict = dict(
        tier=tier,
        confidence=1.0,
        source="explicit",
        static_score=10,
        hard_rule="",
        reasons=(),
        risk_flags=(),
        llm_used=False,
        learned_adjustments=(),
    )
    base.update(overrides)
    return ComplexityDecision(**base)


# ---------------------------------------------------------------------------
# lane_for() resolution
# ---------------------------------------------------------------------------


def test_lane_for_known_names_resolved():
    assert lane_for("lite").name == "lite"
    assert lane_for("standard").name == "standard"
    assert lane_for("heavy").name == "heavy"


def test_lane_for_is_case_and_whitespace_tolerant():
    assert lane_for("LITE").name == "lite"
    assert lane_for(" Heavy ").name == "heavy"


def test_lane_for_unknown_falls_back_to_heavy():
    """Back-compat: unknown / None / empty → HEAVY (preserves pre-tier behavior)."""
    assert lane_for("unknown").name == "heavy"
    assert lane_for(None).name == "heavy"
    assert lane_for("").name == "heavy"


# ---------------------------------------------------------------------------
# LITE preset
# ---------------------------------------------------------------------------


def test_lite_lane_kills_big_wastes():
    """LITE must skip release_tail, eval, task_cycle, telemetry."""
    assert LITE_LANE.release_tail_enabled is False
    assert LITE_LANE.task_cycle_enabled is False
    assert LITE_LANE.eval_required is False
    assert LITE_LANE.telemetry_enabled is False
    assert LITE_LANE.release_approval_required is False
    assert LITE_LANE.require_self_review is False
    assert LITE_LANE.review_max_rounds == 3
    assert LITE_LANE.decision_policy == "auto-skip"


def test_lite_blocking_threshold_only_blocking():
    assert LITE_LANE.review_blocking_threshold == frozenset({"blocking"})


def test_lite_suppresses_release_qa_artifacts():
    assert "RELEASE.md" in LITE_LANE.suppressed_artifacts
    assert "QA_REPORT.md" in LITE_LANE.suppressed_artifacts
    assert ".ship_readiness.json" in LITE_LANE.suppressed_artifacts
    assert "DESIGN.md" in LITE_LANE.suppressed_artifacts


# ---------------------------------------------------------------------------
# STANDARD preset (must NOT become "small HEAVY")
# ---------------------------------------------------------------------------


def test_standard_does_not_require_eval_or_release_approval():
    """Per agreement: STANDARD stays light. No eval/release_approval."""
    assert STANDARD_LANE.eval_required is False
    assert STANDARD_LANE.release_approval_required is False
    assert STANDARD_LANE.release_tail_enabled is False
    assert STANDARD_LANE.telemetry_enabled is False


def test_standard_review_more_rigorous_than_lite():
    assert STANDARD_LANE.review_max_rounds == 2
    assert STANDARD_LANE.require_self_review is True
    assert STANDARD_LANE.review_blocking_threshold == frozenset({"blocking", "critical"})
    assert STANDARD_LANE.decision_policy == "soft-gate"


def test_standard_suppresses_only_release_and_qa():
    """STANDARD keeps DESIGN.md available (when arch signal present)."""
    assert "DESIGN.md" not in STANDARD_LANE.suppressed_artifacts
    assert "QA_REPORT.md" in STANDARD_LANE.suppressed_artifacts
    assert "RELEASE.md" in STANDARD_LANE.suppressed_artifacts


# ---------------------------------------------------------------------------
# HEAVY preset
# ---------------------------------------------------------------------------


def test_heavy_keeps_full_pipeline():
    assert HEAVY_LANE.eval_required is True
    assert HEAVY_LANE.release_approval_required is True
    assert HEAVY_LANE.release_tail_enabled is True
    assert HEAVY_LANE.telemetry_enabled is True
    assert HEAVY_LANE.task_cycle_enabled is True
    assert HEAVY_LANE.compact_context_enabled is True
    assert HEAVY_LANE.review_max_rounds == 3
    assert HEAVY_LANE.suppressed_artifacts == frozenset()
    assert HEAVY_LANE.decision_policy == "approval-required"


def test_heavy_blocking_threshold_includes_high():
    assert "high" in HEAVY_LANE.review_blocking_threshold
    assert "blocking" in HEAVY_LANE.review_blocking_threshold
    assert "critical" in HEAVY_LANE.review_blocking_threshold


# ---------------------------------------------------------------------------
# resolve_workflow_policy()
# ---------------------------------------------------------------------------


def test_resolve_lite_policy_carries_lane_defaults():
    policy = resolve_workflow_policy(decision=_decision("lite"), lane=LITE_LANE)
    assert policy.effective_tier == "lite"
    assert policy.release_tail_enabled is False
    assert policy.eval_required is False
    assert policy.review_max_rounds == 3
    assert policy.resolved_from_lane == "lite"
    assert policy.user_overrides == ()


def test_resolve_standard_policy_carries_lane_defaults():
    policy = resolve_workflow_policy(decision=_decision("standard"), lane=STANDARD_LANE)
    assert policy.effective_tier == "standard"
    assert policy.eval_required is False
    assert policy.release_tail_enabled is False
    assert policy.review_max_rounds == 2
    assert policy.require_self_review is True


def test_resolve_heavy_policy_carries_lane_defaults():
    policy = resolve_workflow_policy(decision=_decision("heavy"), lane=HEAVY_LANE)
    assert policy.effective_tier == "heavy"
    assert policy.release_tail_enabled is True
    assert policy.eval_required is True
    assert policy.task_cycle_enabled is True


def test_user_override_beats_lane_default():
    """User explicit overrides are highest priority."""
    overrides = UserPolicyOverrides(eval_required=True)
    policy = resolve_workflow_policy(
        decision=_decision("lite"), lane=LITE_LANE, overrides=overrides,
    )
    assert policy.eval_required is True
    assert "eval_required" in policy.user_overrides


def test_user_override_partial_keeps_other_lane_defaults():
    overrides = UserPolicyOverrides(review_max_rounds=5)
    policy = resolve_workflow_policy(
        decision=_decision("lite"), lane=LITE_LANE, overrides=overrides,
    )
    assert policy.review_max_rounds == 5
    assert policy.eval_required is False


def test_user_override_can_force_release_tail_off_for_heavy():
    """Operator override to skip release_tail in HEAVY."""
    overrides = UserPolicyOverrides(release_tail_enabled=False)
    policy = resolve_workflow_policy(
        decision=_decision("heavy"), lane=HEAVY_LANE, overrides=overrides,
    )
    assert policy.release_tail_enabled is False
    assert policy.eval_required is True
    assert "release_tail_enabled" in policy.user_overrides


def test_overrides_with_no_explicit_fields_record_empty_tuple():
    policy = resolve_workflow_policy(
        decision=_decision("lite"),
        lane=LITE_LANE,
        overrides=UserPolicyOverrides(),
    )
    assert policy.user_overrides == ()


# ---------------------------------------------------------------------------
# heavy_compatible_policy() back-compat
# ---------------------------------------------------------------------------


def test_heavy_compatible_policy_matches_pre_tier_behavior():
    """Default policy keeps release_tail/eval/etc. ON for back-compat."""
    policy = heavy_compatible_policy()
    assert policy.effective_tier == "heavy"
    assert policy.release_tail_enabled is True
    assert policy.eval_required is True
    assert policy.task_cycle_enabled is True
    assert policy.review_max_rounds == 3
    assert policy.resolved_from_lane == "heavy"
    assert policy.compact_context_enabled is True


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_complexity_decision_to_dict_round_trip_keys():
    d = _decision("standard", reasons=("fallback gray",), risk_flags=("contract",))
    out = d.to_dict()
    assert out["tier"] == "standard"
    assert out["reasons"] == ["fallback gray"]
    assert out["risk_flags"] == ["contract"]
    assert out["llm_used"] is False


def test_workflow_policy_to_dict_json_serializable():
    policy = resolve_workflow_policy(decision=_decision("lite"), lane=LITE_LANE)
    encoded = json.dumps(policy.to_dict(), sort_keys=True)
    assert "effective_tier" in encoded
    assert '"lite"' in encoded
    assert "suppressed_artifacts" in encoded


def test_workflow_policy_to_dict_includes_user_overrides_list():
    overrides = UserPolicyOverrides(eval_required=True, telemetry_enabled=True)
    policy = resolve_workflow_policy(
        decision=_decision("lite"), lane=LITE_LANE, overrides=overrides,
    )
    out = policy.to_dict()
    assert isinstance(out["user_overrides"], list)
    assert set(out["user_overrides"]) == {"eval_required", "telemetry_enabled"}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_all_tiers_constant_matches_lane_names():
    lane_names = {LITE_LANE.name, STANDARD_LANE.name, HEAVY_LANE.name}
    assert set(ALL_TIERS) == lane_names


def test_lane_config_is_frozen():
    """LaneConfig is immutable — runtime cannot mutate presets."""
    with pytest.raises((AttributeError, Exception)):
        LITE_LANE.review_max_rounds = 5  # type: ignore[misc]


def test_workflow_policy_is_frozen():
    """WorkflowPolicy is immutable after resolution."""
    policy = resolve_workflow_policy(decision=_decision("lite"), lane=LITE_LANE)
    with pytest.raises((AttributeError, Exception)):
        policy.eval_required = True  # type: ignore[misc]

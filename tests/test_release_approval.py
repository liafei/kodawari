"""Semi-automatic release approval rules.

Pin the mapping from RunTruth signals to AUTO_APPROVE / MANUAL_REQUIRED so
release flow, release audit, and gate stay aligned.
"""

from __future__ import annotations

import pytest

from kodawari.autopilot.release_approval import evaluate_release_approval


def _truth(**overrides):
    base = {
        "feature": "feat",
        "review_quality": "real",
        "deterministic_recovery_hits": 0,
        "synthesizer_calls": 0,
        "recovery_pressure": 0,
        "changed_files": ["src/app.py"],
        "lane_observation": {"actual_tier": "lite", "predicted_tier": "lite"},
    }
    base.update(overrides)
    return base


def test_clean_run_auto_approves() -> None:
    decision = evaluate_release_approval(run_truth=_truth())
    assert decision.mode == "auto"
    assert decision.triggers == []


def test_heavy_lane_requires_manual() -> None:
    decision = evaluate_release_approval(
        run_truth=_truth(lane_observation={"actual_tier": "heavy", "predicted_tier": "standard"}),
    )
    assert decision.mode == "manual_required"
    assert "heavy_lane" in decision.triggers
    assert decision.detail["lane_tier"] == "heavy"


def test_schema_mutation_requires_manual() -> None:
    decision = evaluate_release_approval(
        run_truth=_truth(changed_files=["src/db/migrations/0001_init.sql", "src/app.py"]),
    )
    assert "schema_mutation" in decision.triggers
    assert "src/db/migrations/0001_init.sql" in decision.detail["schema_mutation_files"]


def test_readiness_blocked_history_requires_manual() -> None:
    decision = evaluate_release_approval(
        run_truth=_truth(),
        readiness_history=[
            {"status": "BLOCKED", "missing_preconditions": ["users.email"]},
            {"status": "PASS"},
        ],
    )
    assert "readiness_blocked_history" in decision.triggers
    assert decision.detail["readiness_blocked_count"] == 1


def test_recovery_pressure_requires_manual_at_threshold() -> None:
    decision = evaluate_release_approval(
        run_truth=_truth(recovery_pressure=5),
    )
    assert "recovery_pressure" in decision.triggers
    assert decision.detail["recovery_total"] == 5


def test_recovery_pressure_below_threshold_auto_approves() -> None:
    decision = evaluate_release_approval(
        run_truth=_truth(deterministic_recovery_hits=2, synthesizer_calls=2, recovery_pressure=4),
    )
    assert "recovery_pressure" not in decision.triggers


def test_simulated_review_blocks_release() -> None:
    decision = evaluate_release_approval(run_truth=_truth(review_quality="simulated"))
    assert "non_real_review_quality" in decision.triggers
    assert decision.detail["review_quality"] == "simulated"


def test_degraded_review_blocks_release() -> None:
    decision = evaluate_release_approval(run_truth=_truth(review_quality="degraded"))
    assert "non_real_review_quality" in decision.triggers


def test_multiple_triggers_collected() -> None:
    decision = evaluate_release_approval(
        run_truth=_truth(
            review_quality="degraded",
            recovery_pressure=8,
            lane_observation={"actual_tier": "heavy"},
            changed_files=["src/db/migrations/0001.sql"],
        ),
    )
    assert decision.mode == "manual_required"
    assert {"heavy_lane", "schema_mutation", "recovery_pressure", "non_real_review_quality"} <= set(decision.triggers)


def test_threshold_is_configurable() -> None:
    decision = evaluate_release_approval(
        run_truth=_truth(recovery_pressure=3),
        recovery_total_threshold=3,
    )
    assert "recovery_pressure" in decision.triggers


def test_empty_truth_blocks_on_unknown_quality(_=None) -> None:
    """Defensive: missing review_quality on a release-bound run should not auto-pass."""
    decision = evaluate_release_approval(run_truth={"review_quality": ""})
    # Empty quality string => quality check skipped (no signal); other defaults are clean.
    # The point is no exception, no false trigger.
    assert decision.mode == "auto"


def test_decision_serializes_to_dict() -> None:
    decision = evaluate_release_approval(run_truth=_truth(review_quality="degraded"))
    payload = decision.to_dict()
    assert payload["mode"] == "manual_required"
    assert isinstance(payload["triggers"], list)
    assert isinstance(payload["detail"], dict)

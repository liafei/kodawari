"""Tests for C5 — lane observation + instincts metadata.

Covers:
  - infer_actual_tier() correct for each input shape (heavy/standard/lite markers)
  - build_lane_observation() flags mismatch / under / over correctly
  - write_lane_observation() persists JSON to .lane_observation.json
  - to_learning_event() shape is suitable for instincts ingestion
  - LearningCandidate / LearnedInstinct round-trip with metadata
  - autopilot_cmd._collect_actual_signals extracts from payload structure
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodawari.autopilot.lane_observation import (
    LANE_OBSERVATION_FILENAME,
    LANE_OBSERVATION_SCHEMA_VERSION,
    ActualRunSignals,
    build_lane_observation,
    infer_actual_tier,
    to_learning_event,
    write_lane_observation,
)
from kodawari.autopilot.workflow_policy import ComplexityDecision
from kodawari.cli.autopilot_cmd import _collect_actual_signals, _estimate_diff_loc
from kodawari.instincts.models import LearnedInstinct, LearningCandidate


def _decision(tier: str = "lite", **overrides):
    base = dict(
        tier=tier, confidence=1.0, source="explicit", static_score=10,
        hard_rule="", reasons=(), risk_flags=(), llm_used=False,
        learned_adjustments=(),
    )
    base.update(overrides)
    return ComplexityDecision(**base)


# ---------------------------------------------------------------------------
# infer_actual_tier — boundary checks
# ---------------------------------------------------------------------------


def test_infer_lite_for_minimal_signals():
    s = ActualRunSignals(diff_loc=20, files_changed=1, rounds_used=1)
    assert infer_actual_tier(s) == "lite"


def test_infer_standard_for_medium_diff():
    s = ActualRunSignals(diff_loc=150, files_changed=4, rounds_used=2)
    assert infer_actual_tier(s) == "standard"


def test_infer_heavy_for_escalation():
    s = ActualRunSignals(diff_loc=20, files_changed=1, rounds_used=1, escalated=True)
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_for_3_rounds():
    s = ActualRunSignals(rounds_used=3)
    assert infer_actual_tier(s) == "heavy"


def test_infer_standard_for_many_runtime_rounds_when_effective_rounds_are_normal():
    s = ActualRunSignals(rounds_used=9, planning_rounds=1, review_rounds=2, review_must_fix_max=2)
    assert infer_actual_tier(s) == "standard"


def test_infer_heavy_when_recovery_pressure_exceeds_threshold():
    s = ActualRunSignals(planning_rounds=1, review_rounds=1, deterministic_recovery_hits=3, recovery_pressure=3)
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_when_review_must_fix_exceeds_normal_band():
    s = ActualRunSignals(planning_rounds=1, review_rounds=2, review_must_fix_max=4)
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_for_large_diff():
    s = ActualRunSignals(diff_loc=400, files_changed=2)
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_for_many_files():
    s = ActualRunSignals(diff_loc=50, files_changed=8)
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_for_blocking_findings():
    s = ActualRunSignals(blocking_findings=3)
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_for_gate_block():
    s = ActualRunSignals(gate_status="BLOCKED")
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_for_contract_touched():
    s = ActualRunSignals(contract_or_schema_touched=True)
    assert infer_actual_tier(s) == "heavy"


def test_infer_heavy_for_release_decision():
    s = ActualRunSignals(release_decision_required=True)
    assert infer_actual_tier(s) == "heavy"


def test_infer_standard_for_one_blocker():
    s = ActualRunSignals(blocking_findings=1)
    assert infer_actual_tier(s) == "standard"


# ---------------------------------------------------------------------------
# build_lane_observation — under/over/match flags
# ---------------------------------------------------------------------------


def test_observation_match_when_predicted_equals_actual():
    obs = build_lane_observation(
        feature="f1",
        predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=10, files_changed=1),
    )
    assert obs.actual_tier == "lite"
    assert obs.mismatch is False
    assert obs.underclassified is False
    assert obs.overclassified is False


def test_observation_underclassified_lite_predicted_heavy_actual():
    obs = build_lane_observation(
        feature="f1",
        predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=400, files_changed=8),
    )
    assert obs.predicted_tier == "lite"
    assert obs.actual_tier == "heavy"
    assert obs.underclassified is True
    assert obs.overclassified is False
    assert obs.mismatch is True


def test_observation_overclassified_heavy_predicted_lite_actual():
    obs = build_lane_observation(
        feature="f1",
        predicted=_decision("heavy"),
        signals=ActualRunSignals(diff_loc=10, files_changed=1),
    )
    assert obs.predicted_tier == "heavy"
    assert obs.actual_tier == "lite"
    assert obs.overclassified is True
    assert obs.mismatch is True


def test_observation_carries_predicted_metadata():
    decision = _decision("standard", source="static_score", static_score=42, llm_used=True, risk_flags=("contract",))
    obs = build_lane_observation(
        feature="f1", predicted=decision, signals=ActualRunSignals(diff_loc=100, files_changed=4),
    )
    assert obs.classification_source == "static_score"
    assert obs.static_score == 42
    assert obs.llm_used is True
    assert "contract" in obs.risk_flags


# ---------------------------------------------------------------------------
# write_lane_observation — persistence
# ---------------------------------------------------------------------------


def test_write_lane_observation_creates_json_file(tmp_path):
    obs = build_lane_observation(
        feature="f1", predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=15, files_changed=1),
    )
    path = write_lane_observation(tmp_path, obs)
    assert path == (tmp_path / LANE_OBSERVATION_FILENAME).resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == LANE_OBSERVATION_SCHEMA_VERSION
    assert payload["feature"] == "f1"
    assert payload["predicted_tier"] == "lite"
    assert payload["actual_tier"] == "lite"
    assert payload["mismatch"] is False


def test_write_lane_observation_creates_planning_dir(tmp_path):
    target = tmp_path / "deeply" / "nested" / "planning"
    obs = build_lane_observation(
        feature="f1", predicted=_decision("lite"),
        signals=ActualRunSignals(),
    )
    path = write_lane_observation(target, obs)
    assert path.exists()
    assert path.parent == target.resolve()


# ---------------------------------------------------------------------------
# to_learning_event — instincts contract shape
# ---------------------------------------------------------------------------


def test_to_learning_event_underclassified():
    obs = build_lane_observation(
        feature="f1", predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=400, files_changed=8),
    )
    event = to_learning_event(obs)
    assert event["category"] == "lane"
    assert event["phase"] == "COMPLEXITY_DETECTION"
    assert event["action"] == "underclassified"
    assert "predicted=lite" in event["message"]
    assert "actual=heavy" in event["message"]
    assert event["metadata"]["predicted_tier"] == "lite"


def test_to_learning_event_match_action():
    obs = build_lane_observation(
        feature="f1", predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=10, files_changed=1),
    )
    event = to_learning_event(obs)
    assert event["action"] == "match"


# ---------------------------------------------------------------------------
# LearningCandidate / LearnedInstinct metadata round-trip
# ---------------------------------------------------------------------------


def test_learning_candidate_metadata_default_empty():
    c = LearningCandidate(id="x", signature="sig", category="lane", phase="P")
    assert c.metadata == {}


def test_learning_candidate_metadata_round_trip_via_dict():
    c = LearningCandidate(
        id="x", signature="sig", category="lane", phase="P",
        metadata={"predicted_tier": "lite", "actual_tier": "heavy"},
    )
    payload = c.to_dict()
    assert payload["metadata"] == {"predicted_tier": "lite", "actual_tier": "heavy"}
    rebuilt = LearningCandidate.from_dict(payload)
    assert rebuilt.metadata == c.metadata


def test_learning_candidate_from_dict_handles_missing_metadata():
    """Pre-C5 payloads have no metadata field — must not crash."""
    payload = {
        "id": "old", "signature": "old", "category": "runtime", "phase": "RUNTIME",
    }
    c = LearningCandidate.from_dict(payload)
    assert c.metadata == {}


def test_learned_instinct_metadata_round_trip():
    inst = LearnedInstinct(
        id="i1", signature="sig", pattern="p", category="lane",
        metadata={"reason": "underclassified_3x"},
    )
    payload = inst.to_dict()
    assert payload["metadata"] == {"reason": "underclassified_3x"}
    rebuilt = LearnedInstinct.from_dict(payload)
    assert rebuilt.metadata == inst.metadata


def test_learned_instinct_from_dict_handles_missing_metadata():
    payload = {"id": "i1", "signature": "sig", "pattern": "p"}
    inst = LearnedInstinct.from_dict(payload)
    assert inst.metadata == {}


# ---------------------------------------------------------------------------
# _collect_actual_signals — autopilot_cmd integration
# ---------------------------------------------------------------------------


def test_collect_actual_signals_extracts_blocking_from_rounds():
    payload = {"final_outcome": {"gate_status": "PASS", "verify_status": "PASS"}}
    rounds = [
        {"blocking_findings_count": 1},
        {"blocking_findings_count": 2},
    ]
    signals = _collect_actual_signals(
        payload=payload, rounds=rounds, reliable_changed_files=("a.py",),
    )
    assert signals.blocking_findings == 3
    assert signals.rounds_used == 2
    assert signals.files_changed == 1
    assert signals.gate_status == "PASS"


def test_collect_actual_signals_prefers_run_truth_round_breakdown():
    payload = {
        "run_truth": {
            "runtime_rounds": 9,
            "planning_rounds": 1,
            "execution_rounds": 2,
            "review_rounds": 2,
            "executor_attempts": 2,
            "deterministic_recovery_hits": 1,
            "synthesizer_calls": 0,
            "recovery_pressure": 1,
            "review_must_fix_max": 2,
            "blocking_findings": 0,
            "verify_status": "PASS",
            "gate_status": "PASS",
        }
    }
    signals = _collect_actual_signals(
        payload=payload,
        rounds=[{"stage": "IMPLEMENT"} for _ in range(9)],
        reliable_changed_files=("tests/test_t107.py",),
    )
    assert signals.rounds_used == 9
    assert signals.planning_rounds == 1
    assert signals.review_rounds == 2
    assert signals.recovery_pressure == 1
    assert infer_actual_tier(signals) == "standard"


def test_collect_actual_signals_detects_release_decision():
    payload = {"final_outcome": {}, "interaction_state": "AWAITING_DECISION", "decision_kind": "release_approval"}
    signals = _collect_actual_signals(
        payload=payload, rounds=[], reliable_changed_files=(),
    )
    assert signals.release_decision_required is True


def test_collect_actual_signals_detects_escalation():
    payload = {"final_outcome": {}, "decision_kind": "planning_escalation", "interaction_state": "BLOCKED"}
    signals = _collect_actual_signals(
        payload=payload, rounds=[], reliable_changed_files=(),
    )
    assert signals.escalated is True


def test_collect_actual_signals_detects_contract_path_touched():
    signals = _collect_actual_signals(
        payload={"final_outcome": {}},
        rounds=[],
        reliable_changed_files=("backend/api/v1/routes/daily_routes.py",),
    )
    assert signals.contract_or_schema_touched is True


def test_collect_actual_signals_resilient_to_garbage_rounds():
    """Malformed rounds entries must not raise."""
    signals = _collect_actual_signals(
        payload={"final_outcome": {}},
        rounds=[None, "junk", {"blocking_findings_count": "abc"}, {"blocking_findings_count": 1}],
        reliable_changed_files=(),
    )
    assert signals.blocking_findings == 1


def test_collect_actual_signals_uses_diff_loc_estimator(monkeypatch):
    monkeypatch.setattr(
        "kodawari.cli.autopilot_cmd._estimate_diff_loc",
        lambda **_: 123,
    )
    signals = _collect_actual_signals(
        payload={"final_outcome": {}},
        rounds=[],
        reliable_changed_files=("backend/service.py",),
    )
    assert signals.diff_loc == 123


def test_estimate_diff_loc_parses_git_numstat(monkeypatch, tmp_path):
    class _Result:
        stdout = "10\t2\tbackend/service.py\n-\t-\tassets/logo.png\n"

    monkeypatch.setattr(
        "kodawari.cli.autopilot_cmd.subprocess.run",
        lambda *a, **kw: _Result(),
    )
    total = _estimate_diff_loc(
        project_root=tmp_path,
        changed_files=("backend/service.py", "assets/logo.png"),
    )
    assert total == 12

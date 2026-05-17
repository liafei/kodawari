"""Tests for C7 — lane observation → instincts ingest with metadata.

Covers:
  - ingest_error_event preserves metadata from event payload (new behavior)
  - ingest_error_event merges metadata on repeat events
  - ingest_error_event handles missing/invalid metadata gracefully
  - _ingest_lane_observation_to_instincts creates a lane-category candidate
  - full round-trip: build observation -> ingest -> candidate stored with metadata
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kodawari.autopilot.lane_observation import (
    ActualRunSignals,
    build_lane_observation,
)
from kodawari.autopilot.workflow_policy import ComplexityDecision
from kodawari.cli.autopilot_cmd import _ingest_lane_observation_to_instincts
from kodawari.instincts.engine import ingest_error_event
from kodawari.instincts.storage import InstinctStore


def _decision(tier: str = "lite", **overrides):
    base = dict(
        tier=tier, confidence=1.0, source="static_score", static_score=20,
        hard_rule="", reasons=(), risk_flags=(), llm_used=False, learned_adjustments=(),
    )
    base.update(overrides)
    return ComplexityDecision(**base)


# ---------------------------------------------------------------------------
# ingest_error_event metadata handling
# ---------------------------------------------------------------------------


def test_ingest_event_stores_metadata(tmp_path):
    event = {
        "category": "lane",
        "phase": "COMPLEXITY_DETECTION",
        "action": "underclassified",
        "message": "predicted=lite actual=heavy files=7",
        "metadata": {"predicted_tier": "lite", "actual_tier": "heavy", "files_changed": 7},
    }
    result = ingest_error_event(tmp_path, event)
    assert result["updated"] is True

    store = InstinctStore(tmp_path)
    data = store.load()
    matches = [c for c in data.learning_candidates if c.category == "lane"]
    assert len(matches) == 1
    assert matches[0].metadata == {
        "predicted_tier": "lite", "actual_tier": "heavy", "files_changed": 7,
    }


def test_ingest_event_merges_metadata_on_repeat(tmp_path):
    first = {
        "category": "lane", "phase": "COMPLEXITY_DETECTION", "action": "underclassified",
        "message": "same signature",
        "metadata": {"predicted_tier": "lite", "files_changed": 3},
    }
    second = {
        "category": "lane", "phase": "COMPLEXITY_DETECTION", "action": "underclassified",
        "message": "same signature",
        "metadata": {"actual_tier": "heavy", "files_changed": 5},
    }
    ingest_error_event(tmp_path, first)
    ingest_error_event(tmp_path, second)

    data = InstinctStore(tmp_path).load()
    lane = [c for c in data.learning_candidates if c.category == "lane"]
    assert len(lane) == 1
    # Second event fields override first; new keys added
    assert lane[0].metadata["predicted_tier"] == "lite"
    assert lane[0].metadata["actual_tier"] == "heavy"
    assert lane[0].metadata["files_changed"] == 5
    assert lane[0].count == 2


def test_ingest_event_handles_missing_metadata(tmp_path):
    """Pre-C5 events have no metadata key — must not crash."""
    event = {
        "category": "runtime",
        "phase": "IMPLEMENT",
        "message": "legacy error",
    }
    result = ingest_error_event(tmp_path, event)
    assert result["updated"] is True
    data = InstinctStore(tmp_path).load()
    assert any(c.metadata == {} for c in data.learning_candidates)


def test_ingest_event_handles_invalid_metadata_type(tmp_path):
    """Non-dict metadata is ignored, not crashed on."""
    event = {
        "category": "lane",
        "phase": "COMPLEXITY_DETECTION",
        "message": "bad metadata shape",
        "metadata": "not-a-dict",
    }
    result = ingest_error_event(tmp_path, event)
    assert result["updated"] is True
    data = InstinctStore(tmp_path).load()
    lane = [c for c in data.learning_candidates if c.category == "lane"]
    assert lane and lane[0].metadata == {}


# ---------------------------------------------------------------------------
# _ingest_lane_observation_to_instincts
# ---------------------------------------------------------------------------


def test_ingest_lane_observation_creates_lane_candidate(tmp_path):
    obs = build_lane_observation(
        feature="f1",
        predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=400, files_changed=7, rounds_used=2),
    )
    assert obs.mismatch is True
    _ingest_lane_observation_to_instincts(tmp_path, obs)

    data = InstinctStore(tmp_path).load()
    lane = [c for c in data.learning_candidates if c.category == "lane"]
    assert len(lane) == 1
    assert lane[0].phase == "COMPLEXITY_DETECTION"
    assert lane[0].metadata["predicted_tier"] == "lite"
    assert lane[0].metadata["actual_tier"] == "heavy"


def test_ingest_lane_observation_is_silent_on_failure(tmp_path):
    """A broken project_root must not raise (best-effort promise)."""
    obs = build_lane_observation(
        feature="f1", predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=500, files_changed=8),
    )
    # Pass a path that cannot be written; function must swallow silently
    _ingest_lane_observation_to_instincts(Path("/nonexistent/surely/missing/abcdef"), obs)
    # No exception = pass


def test_ingest_is_skipped_for_match_observation(tmp_path):
    """On matched prediction, no learning event should be created.

    We call through `_emit_lane_observation` via its helper: the caller
    only invokes ingest when `observation.mismatch is True`. Here we verify
    that a matched observation has mismatch=False so the caller skips.
    """
    obs = build_lane_observation(
        feature="f1",
        predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=10, files_changed=1),
    )
    assert obs.mismatch is False


def test_ingest_lane_observation_repeat_bumps_count(tmp_path):
    """Repeated mismatches for same signature increment count + update metadata."""
    for _ in range(3):
        obs = build_lane_observation(
            feature="f1",
            predicted=_decision("lite"),
            signals=ActualRunSignals(diff_loc=400, files_changed=7, rounds_used=2),
        )
        _ingest_lane_observation_to_instincts(tmp_path, obs)

    data = InstinctStore(tmp_path).load()
    lane = [c for c in data.learning_candidates if c.category == "lane"]
    assert len(lane) == 1
    assert lane[0].count == 3


def test_different_features_same_mismatch_shape_stay_separate(tmp_path):
    """Two features with identical mismatch shape must NOT merge into one candidate."""
    signals = ActualRunSignals(diff_loc=400, files_changed=7, rounds_used=2)
    for feature in ("auth-rewrite", "payment-refactor"):
        obs = build_lane_observation(
            feature=feature,
            predicted=_decision("lite"),
            signals=signals,
        )
        _ingest_lane_observation_to_instincts(tmp_path, obs)

    data = InstinctStore(tmp_path).load()
    lane = [c for c in data.learning_candidates if c.category == "lane"]
    assert len(lane) == 2, "each feature must have its own candidate"
    patterns = {c.suggested_pattern for c in lane}
    assert "auth-rewrite" in patterns
    assert "payment-refactor" in patterns


def test_same_feature_same_direction_merges_correctly(tmp_path):
    """Repeated mismatch for same feature+direction should accumulate in one candidate."""
    signals = ActualRunSignals(diff_loc=400, files_changed=7, rounds_used=2)
    for _ in range(3):
        obs = build_lane_observation(
            feature="auth-rewrite",
            predicted=_decision("lite"),
            signals=signals,
        )
        _ingest_lane_observation_to_instincts(tmp_path, obs)

    data = InstinctStore(tmp_path).load()
    lane = [c for c in data.learning_candidates if c.category == "lane"]
    assert len(lane) == 1
    assert lane[0].count == 3
    assert lane[0].suggested_pattern == "auth-rewrite"

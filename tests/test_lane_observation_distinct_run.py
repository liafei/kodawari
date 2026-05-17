"""Lane observations must flow through PR2.5 distinct-run promotion.

Why: lane mismatches were stored with empty run_id, so distinct_run_count
never grew. Three different features all underclassified across three runs
should produce three LearnedInstincts; before this fix they accumulated as
candidates that never crossed the threshold.

This module pins:
- _ingest_lane_observation_to_instincts reads run_id from
  .autopilot_state.json when planning_dir is supplied, and stamps it on the
  event so seen_run_ids / distinct_run_count grow as expected.
- ingest_lane_event uses _promotion_count (PR2.5) so a single noisy
  re-run cannot promote.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.autopilot.lane_observation import (
    ActualRunSignals,
    build_lane_observation,
)
from kodawari.autopilot.workflow_policy import ComplexityDecision
from kodawari.cli.runtime.autopilot_cmd import (
    _ingest_lane_observation_to_instincts,
    _read_autopilot_run_id,
)
from kodawari.instincts.engine import ingest_lane_event
from kodawari.instincts.storage import InstinctStore


def _decision(tier: str = "lite") -> ComplexityDecision:
    return ComplexityDecision(
        tier=tier,
        confidence=1.0,
        source="static_score",
        static_score=20,
        hard_rule="",
        reasons=(),
        risk_flags=(),
        llm_used=False,
        learned_adjustments=(),
    )


def _write_state(planning_dir: Path, run_id: str) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps({"run_id": run_id, "feature": "demo"}),
        encoding="utf-8",
    )


def test_read_autopilot_run_id_returns_value_when_state_exists(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    _write_state(planning_dir, "run_abc")
    assert _read_autopilot_run_id(planning_dir) == "run_abc"


def test_read_autopilot_run_id_silent_on_missing_state(tmp_path: Path) -> None:
    assert _read_autopilot_run_id(tmp_path / "no_planning") == ""
    assert _read_autopilot_run_id(None) == ""


def test_lane_ingest_stamps_run_id_when_planning_dir_supplied(tmp_path: Path) -> None:
    project_root = tmp_path / "proj"
    planning_dir = project_root / "planning" / "feat"
    _write_state(planning_dir, "run_xyz_42")

    obs = build_lane_observation(
        feature="feat",
        predicted=_decision("lite"),
        signals=ActualRunSignals(diff_loc=400, files_changed=7, rounds_used=2),
    )
    assert obs.mismatch is True
    _ingest_lane_observation_to_instincts(
        project_root, obs, planning_dir=planning_dir,
    )

    payload = InstinctStore(project_root).load()
    lane = [c for c in payload.learning_candidates if c.category == "lane"]
    assert len(lane) == 1
    assert lane[0].seen_run_ids == ["run_xyz_42"]
    assert lane[0].distinct_run_count == 1


def test_lane_promotes_after_distinct_runs_not_single_session_repeats(tmp_path: Path) -> None:
    """Three different run_ids on the same feature → promote. Three repeats
    in ONE run_id → do NOT promote (distinct_run_count stays at 1)."""
    project_root_repeats = tmp_path / "single_run"
    for _ in range(3):
        # Same run_id, three repeats — represents one session retrying.
        ingest_lane_event(
            project_root_repeats,
            {
                "category": "lane",
                "phase": "COMPLEXITY_DETECTION",
                "action": "underclassified",
                "message": "predicted=lite actual=heavy files=7",
                "metadata": {
                    "feature": "feat-repeat",
                    "underclassified": True,
                },
                "run_id": "run_only_one",
            },
        )
    repeats_payload = InstinctStore(project_root_repeats).load()
    repeats_learned = [
        li for li in repeats_payload.learned_instincts if li.category == "lane"
    ]
    # LANE_LEARNING_THRESHOLD == 2; with 3 same-run events, distinct_run_count
    # is 1 → _promotion_count returns 1 → below threshold → NOT promoted.
    assert repeats_learned == [], (
        "Single-run repeats must not be enough to promote; PR2.5 distinct-run "
        "semantics is the whole point of this fix."
    )

    # Now distinct runs on a separate project root.
    project_root_distinct = tmp_path / "distinct_runs"
    for run_id in ("run_a", "run_b", "run_c"):
        ingest_lane_event(
            project_root_distinct,
            {
                "category": "lane",
                "phase": "COMPLEXITY_DETECTION",
                "action": "underclassified",
                "message": "predicted=lite actual=heavy files=7",
                "metadata": {
                    "feature": "feat-distinct",
                    "underclassified": True,
                },
                "run_id": run_id,
            },
        )
    distinct_payload = InstinctStore(project_root_distinct).load()
    distinct_learned = [
        li for li in distinct_payload.learned_instincts if li.category == "lane"
    ]
    # 3 distinct run_ids, threshold 2 → promoted.
    assert len(distinct_learned) == 1
    assert distinct_learned[0].count == 3


def test_lane_legacy_events_without_run_id_still_progress_via_event_count(tmp_path: Path) -> None:
    """Backward compat: pre-fix lane events have no run_id. They must still
    promote via _promotion_count's legacy fallback, otherwise existing
    on-disk lane candidates would be permanently stuck."""
    for _ in range(3):
        ingest_lane_event(
            tmp_path,
            {
                "category": "lane",
                "phase": "COMPLEXITY_DETECTION",
                "action": "underclassified",
                "message": "legacy lane mismatch",
                "metadata": {
                    "feature": "legacy-feat",
                    "underclassified": True,
                },
                # No run_id — simulates pre-PR data shape.
            },
        )
    payload = InstinctStore(tmp_path).load()
    learned = [li for li in payload.learned_instincts if li.category == "lane"]
    assert len(learned) == 1

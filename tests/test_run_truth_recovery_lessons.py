"""RunTruth recovery lessons view.

Lessons derived from already-recorded recovery decisions; no separate
recovery_lessons.json file. Verifies the projection captures detector
identity, applies_to_future_runs gating on success, and excludes
non-recovery decisions.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.autopilot.recovery.executor_recovery import RECOVERY_DECISION_FILENAME
from kodawari.cli.evidence.artifact_truth import build_run_truth


def _seed_decisions(planning_dir: Path, decisions: list[dict]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    # Multiple decisions can be stored as a list under .execution_recovery_decision.json
    # via the existing append behavior. For tests we write the most recent to the
    # canonical filename and pre-seed the run_result to carry the sequence.
    if decisions:
        (planning_dir / RECOVERY_DECISION_FILENAME).write_text(
            json.dumps(decisions[-1]), encoding="utf-8"
        )


def test_lessons_attach_detector_metadata(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    decision = {
        "role": "deterministic_recovery",
        "action": "executor_no_write_stall_retry",
        "detector_name": "no_write_stall",
        "detector_priority": 10,
        "reason": "executor stalled on reads",
        "source": "kodawari.no_write_stall_recovery",
        "detector_evidence": {"error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"},
    }
    _seed_decisions(planning_dir, [decision])
    truth = build_run_truth(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="feat",
        payload={"final_status": "OK"},
        run_result={"recovery_decisions": [decision]},
        rounds=[],
    )
    lessons = truth["recovery_lessons"]
    assert len(lessons) == 1
    lesson = lessons[0]
    assert lesson["role"] == "deterministic_recovery"
    assert lesson["detector_name"] == "no_write_stall"
    assert lesson["produced_card"] is True
    assert lesson["applies_to_future_runs"] is True


def test_lesson_not_replayable_when_run_failed(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    decision = {
        "role": "deterministic_recovery",
        "action": "executor_no_write_stall_retry",
        "detector_name": "no_write_stall",
        "detector_evidence": {"error_code": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"},
    }
    _seed_decisions(planning_dir, [decision])
    truth = build_run_truth(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="feat",
        payload={"final_status": "BLOCKED"},
        run_result={"recovery_decisions": [decision]},
        rounds=[],
    )
    assert truth["recovery_lessons"][0]["applies_to_future_runs"] is False


def test_synthesizer_decision_recorded_but_not_replayable(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    decision = {
        "role": "recovery_synthesizer",
        "action": "narrow_patch_plan",
        "reason": "synthesizer-driven retry",
        "source": "kodawari.recovery_synthesizer",
    }
    _seed_decisions(planning_dir, [decision])
    truth = build_run_truth(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="feat",
        payload={"final_status": "OK"},
        run_result={"recovery_decisions": [decision]},
        rounds=[],
    )
    lessons = truth["recovery_lessons"]
    assert lessons[0]["role"] == "recovery_synthesizer"
    assert lessons[0]["produced_card"] is True  # narrow_patch_plan produces a card
    # Only deterministic recoveries are flagged as replayable lessons.
    assert lessons[0]["applies_to_future_runs"] is False


def test_no_recovery_decisions_yields_empty_lessons(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    truth = build_run_truth(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="feat",
        payload={"final_status": "OK"},
        run_result={},
        rounds=[],
    )
    assert truth["recovery_lessons"] == []

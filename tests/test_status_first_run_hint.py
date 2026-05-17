"""Tests for D3 — `kodawari status` first_run_hint field."""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.status.status_cmd import _compute_first_run_hint


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_hint_needs_planning_when_artifacts_missing(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] == "needs_planning"
    assert "kodawari plan" in hint["command"]
    assert "feat" in hint["command"]


def test_hint_awaiting_decision_when_decision_request_unresponded(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    _write_json(planning_dir / ".planning_decision_request.json", {"id": "x", "decision_type": "architecture"})

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] == "awaiting_decision"
    assert "kodawari decide" in hint["command"]


def test_hint_skips_awaiting_when_decision_already_applied(tmp_path: Path) -> None:
    """D3: a decision_request with applied_at set means the user already
    responded; status should move past awaiting_decision to whatever artifact
    state comes next (planning still missing -> needs_planning)."""
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    _write_json(
        planning_dir / ".planning_decision_request.json",
        {"id": "x", "decision_type": "architecture", "applied_at": "2026-05-17T00:00:00Z"},
    )

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] != "awaiting_decision"


def test_hint_ready_to_work_when_planning_done_but_no_autopilot_state(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    _write_json(planning_dir / "PRD_INTAKE.json", {"x": 1})
    _write_json(planning_dir / "TASK_GRAPH.json", {"tasks": []})

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] == "ready_to_work"
    assert "kodawari work" in hint["command"]


def test_hint_needs_review_when_work_complete_but_no_review(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    _write_json(planning_dir / "PRD_INTAKE.json", {"x": 1})
    _write_json(planning_dir / "TASK_GRAPH.json", {"tasks": []})
    _write_json(planning_dir / ".autopilot_state.json", {"completed_tasks": ["T1"]})

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] == "needs_review"
    assert "kodawari review" in hint["command"]


def test_hint_needs_release_when_review_complete_but_no_release_md(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    _write_json(planning_dir / "PRD_INTAKE.json", {"x": 1})
    _write_json(planning_dir / "TASK_GRAPH.json", {"tasks": []})
    _write_json(planning_dir / ".autopilot_state.json", {})
    _write_json(planning_dir / "REVIEW_RESULT.json", {"status": "pass"})

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] == "needs_release"
    assert "kodawari release" in hint["command"]


def test_hint_all_complete_when_release_md_exists(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    _write_json(planning_dir / "PRD_INTAKE.json", {"x": 1})
    _write_json(planning_dir / "TASK_GRAPH.json", {"tasks": []})
    _write_json(planning_dir / ".autopilot_state.json", {})
    _write_json(planning_dir / "REVIEW_RESULT.json", {})
    (planning_dir / "RELEASE.md").write_text("# Release", encoding="utf-8")

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] == "all_complete"


def test_hint_decision_request_state_includes_artifact_pointer(tmp_path: Path) -> None:
    """D3: when surfacing awaiting_decision, include the decision_request path
    so the user knows which file kodawari decide will respond to."""
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    decision_path = planning_dir / ".planning_decision_request.json"
    _write_json(decision_path, {"id": "x"})

    hint = _compute_first_run_hint(planning_dir)

    assert hint["state"] == "awaiting_decision"
    assert hint["artifact"] == str(decision_path)

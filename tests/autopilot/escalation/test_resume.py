"""Unit tests for escalation resume logic."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from kodawari.autopilot.escalation.resume import (
    SPLIT_PROPOSAL_FILENAME,
    SUPERSEDED_MARKER_FILENAME,
    _topological_sort,
    apply_pending_resume,
    detect_pending_resume,
)


# ---------------------------------------------------------------------------
# Topological sort
# ---------------------------------------------------------------------------


def test_topo_sort_linear():
    subs = [
        {"name": "C", "depends_on": ["B"]},
        {"name": "B", "depends_on": ["A"]},
        {"name": "A", "depends_on": []},
    ]
    ordered, cycle = _topological_sort(subs)
    assert [s["name"] for s in ordered] == ["A", "B", "C"]
    assert cycle == []


def test_topo_sort_independent():
    subs = [
        {"name": "X", "depends_on": []},
        {"name": "Y", "depends_on": []},
    ]
    ordered, cycle = _topological_sort(subs)
    names = [s["name"] for s in ordered]
    assert set(names) == {"X", "Y"}
    assert cycle == []


def test_topo_sort_diamond():
    subs = [
        {"name": "D", "depends_on": ["B", "C"]},
        {"name": "C", "depends_on": ["A"]},
        {"name": "B", "depends_on": ["A"]},
        {"name": "A", "depends_on": []},
    ]
    ordered, cycle = _topological_sort(subs)
    names = [s["name"] for s in ordered]
    assert names.index("A") < names.index("B")
    assert names.index("A") < names.index("C")
    assert names.index("B") < names.index("D")
    assert names.index("C") < names.index("D")
    assert cycle == []


def test_topo_sort_cycle_detected():
    subs = [
        {"name": "X", "depends_on": ["Y"]},
        {"name": "Y", "depends_on": ["X"]},
    ]
    ordered, cycle = _topological_sort(subs)
    assert ordered == []
    assert "X" in cycle and "Y" in cycle


def test_topo_sort_self_cycle():
    subs = [{"name": "X", "depends_on": ["X"]}]
    ordered, cycle = _topological_sort(subs)
    assert ordered == []
    assert "X" in cycle


# ---------------------------------------------------------------------------
# detect_pending_resume
# ---------------------------------------------------------------------------


def test_detect_no_pending():
    with tempfile.TemporaryDirectory() as tmp:
        assert detect_pending_resume(Path(tmp)) is None


def test_detect_split_proposal():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = {
            "schema_version": "workflow.split_proposal.v1",
            "parent_feature": "p",
            "sub_features": [{"name": "s1"}],
        }
        (tmp / SPLIT_PROPOSAL_FILENAME).write_text(json.dumps(data))
        pending = detect_pending_resume(tmp)
        assert pending is not None
        assert pending["kind"] == "split_proposal"


def test_detect_split_proposal_already_applied():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        data = {
            "schema_version": "workflow.split_proposal.v1",
            "parent_feature": "p",
            "sub_features": [{"name": "s1"}],
            "applied_at": "2026-05-15T00:00:00",  # already applied
        }
        (tmp / SPLIT_PROPOSAL_FILENAME).write_text(json.dumps(data))
        assert detect_pending_resume(tmp) is None


def test_detect_planning_response():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / ".planning_decision_response.json").write_text(json.dumps({
            "phase": "planning",
            "escalation_kind": "PLANNING_DEADLOCK",
            "action": "skip",
        }))
        pending = detect_pending_resume(tmp)
        assert pending is not None
        assert pending["kind"] == "decision_response"
        assert pending["phase"] == "planning"


def test_detect_priority_split_over_response():
    """When both split_proposal and response exist, split takes priority."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / SPLIT_PROPOSAL_FILENAME).write_text(json.dumps({
            "schema_version": "workflow.split_proposal.v1",
            "parent_feature": "p",
            "sub_features": [{"name": "s1"}],
        }))
        (tmp / ".planning_decision_response.json").write_text(json.dumps({
            "phase": "planning",
            "escalation_kind": "PLANNING_DEADLOCK",
            "action": "skip",
        }))
        pending = detect_pending_resume(tmp)
        assert pending["kind"] == "split_proposal"


# ---------------------------------------------------------------------------
# apply_pending_resume: skip action
# ---------------------------------------------------------------------------


def test_apply_planning_skip_marks_feature_aborted():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / ".planning_decision_response.json").write_text(json.dumps({
            "phase": "planning",
            "escalation_kind": "PLANNING_DEADLOCK",
            "action": "skip",
        }))
        outcome = apply_pending_resume(
            planning_dir=tmp,
            feature="x",
            project_root=tmp.parent,
        )
        assert outcome["status"] == "applied"
        assert outcome["effect"] == "feature_skipped"
        # Marker file written
        assert (tmp / "FEATURE_ABORTED.md").exists()
        # Response marked applied_at
        resp = json.loads((tmp / ".planning_decision_response.json").read_text())
        assert resp.get("applied_at")


def test_apply_executor_skip():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / ".executor_decision_response.json").write_text(json.dumps({
            "phase": "executor",
            "escalation_kind": "EXECUTOR_STUCK",
            "action": "skip",
        }))
        outcome = apply_pending_resume(
            planning_dir=tmp,
            feature="x",
            project_root=tmp.parent,
        )
        assert outcome["status"] == "applied"
        assert outcome["effect"] == "task_skipped"
        # Does NOT mark feature aborted (only task)
        assert not (tmp / "FEATURE_ABORTED.md").exists()


def test_apply_no_pending():
    with tempfile.TemporaryDirectory() as tmp:
        outcome = apply_pending_resume(
            planning_dir=Path(tmp),
            feature="x",
            project_root=Path(tmp).parent,
        )
        assert outcome["status"] == "no_pending"


# ---------------------------------------------------------------------------
# split_proposal with cycle is rejected
# ---------------------------------------------------------------------------


def test_apply_split_with_cycle_fails():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / SPLIT_PROPOSAL_FILENAME).write_text(json.dumps({
            "schema_version": "workflow.split_proposal.v1",
            "parent_feature": "p",
            "parent_split_depth": 0,
            "max_depth_check": {"limit": 2},
            "sub_features": [
                {"name": "X", "depends_on": ["Y"]},
                {"name": "Y", "depends_on": ["X"]},
            ],
        }))
        outcome = apply_pending_resume(
            planning_dir=tmp,
            feature="p",
            project_root=tmp.parent,
        )
        assert outcome["status"] == "failed"
        assert "cycle" in outcome.get("error", "").lower()


def test_apply_split_at_max_depth_fails():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / SPLIT_PROPOSAL_FILENAME).write_text(json.dumps({
            "schema_version": "workflow.split_proposal.v1",
            "parent_feature": "p",
            "parent_split_depth": 2,  # at limit
            "max_depth_check": {"limit": 2},
            "sub_features": [{"name": "X", "depends_on": []}],
        }))
        outcome = apply_pending_resume(
            planning_dir=tmp,
            feature="p",
            project_root=tmp.parent,
        )
        assert outcome["status"] == "failed"
        assert "max_split_depth" in outcome.get("error", "")

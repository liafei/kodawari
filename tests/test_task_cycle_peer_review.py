"""Tests for task-cycle peer review honoring + budget cap.

Pins the post-fix behavior where task-cycle entries respect the caller's
peer_review_enabled (typically driven by --real-peer-review) instead of
silently hardcoding False.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kodawari.cli.runtime import autopilot_workflow_runtime
from kodawari.cli.runtime.autopilot_workflow_runtime import (
    _TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS,
    _apply_task_cycle_peer_review_cap,
    _task_cycle_runtime,
)


def _fake_engine(*, max_rounds: int = 8) -> Any:
    engine = types.SimpleNamespace()
    engine.config = types.SimpleNamespace(collaboration_max_rounds=max_rounds)
    return engine


def test_apply_cap_noop_when_peer_review_disabled() -> None:
    """When peer review is off, single-pass already bounds the loop; don't
    rewrite the caller's config."""
    engine = _fake_engine(max_rounds=8)
    _apply_task_cycle_peer_review_cap(engine, peer_review_enabled=False)
    assert engine.config.collaboration_max_rounds == 8


def test_apply_cap_lowers_when_caller_max_exceeds_limit() -> None:
    """Caller default 8 rounds × N backlog entries would explode token budget.
    Cap to _TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS per entry."""
    engine = _fake_engine(max_rounds=8)
    _apply_task_cycle_peer_review_cap(engine, peer_review_enabled=True)
    assert engine.config.collaboration_max_rounds == _TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS


def test_apply_cap_does_not_widen_when_caller_max_already_small() -> None:
    """If caller's config is already at or below the cap, don't widen it."""
    engine = _fake_engine(max_rounds=1)
    _apply_task_cycle_peer_review_cap(engine, peer_review_enabled=True)
    assert engine.config.collaboration_max_rounds == 1


def test_apply_cap_tolerates_missing_config() -> None:
    engine = types.SimpleNamespace()
    # No config attribute. Should not raise.
    _apply_task_cycle_peer_review_cap(engine, peer_review_enabled=True)


def test_task_cycle_runtime_forwards_peer_review_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: _task_cycle_runtime used to hardcode peer_review_enabled=False.
    Now it must forward the caller's flag verbatim into _completed_task_cycle."""
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    engine = _fake_engine(max_rounds=8)

    monkeypatch.setattr(
        autopilot_workflow_runtime,
        "_planned_tasks",
        lambda planning_dir, upstream: [{"label": "T1: x", "scope": ""}],
    )
    monkeypatch.setattr(
        autopilot_workflow_runtime,
        "_reset_state_for_task_cycle",
        lambda engine: None,
    )

    captured: dict[str, Any] = {}

    def _capture(*, engine, project_root, planning_dir, tasks, peer_review_enabled):
        captured["peer_review_enabled"] = peer_review_enabled
        captured["max_rounds_at_call"] = engine.config.collaboration_max_rounds
        return {"upstream_passed": True, "tasks": tasks, "task_results": []}, []

    monkeypatch.setattr(
        autopilot_workflow_runtime, "_completed_task_cycle", _capture
    )

    _task_cycle_runtime(
        engine=engine,
        project_root=tmp_path,
        planning_dir=planning_dir,
        peer_review_enabled=True,
        upstream_task_label="upstream",
        upstream_passed=True,
    )

    assert captured["peer_review_enabled"] is True, (
        "task-cycle must forward caller's peer_review_enabled — used to hardcode False"
    )
    assert captured["max_rounds_at_call"] == _TASK_CYCLE_PEER_REVIEW_MAX_ROUNDS, (
        "When peer review is forwarded, the cap must apply BEFORE entries are dispatched"
    )


def test_task_cycle_runtime_forwards_false_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BC: when caller explicitly passed peer_review_enabled=False (e.g. user
    opts out), task-cycle must remain in single-pass and NOT apply the cap."""
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    engine = _fake_engine(max_rounds=8)

    monkeypatch.setattr(
        autopilot_workflow_runtime,
        "_planned_tasks",
        lambda planning_dir, upstream: [{"label": "T1: x", "scope": ""}],
    )
    monkeypatch.setattr(
        autopilot_workflow_runtime,
        "_reset_state_for_task_cycle",
        lambda engine: None,
    )

    captured: dict[str, Any] = {}

    def _capture(*, engine, project_root, planning_dir, tasks, peer_review_enabled):
        captured["peer_review_enabled"] = peer_review_enabled
        captured["max_rounds_at_call"] = engine.config.collaboration_max_rounds
        return {"upstream_passed": True, "tasks": tasks, "task_results": []}, []

    monkeypatch.setattr(
        autopilot_workflow_runtime, "_completed_task_cycle", _capture
    )

    _task_cycle_runtime(
        engine=engine,
        project_root=tmp_path,
        planning_dir=planning_dir,
        peer_review_enabled=False,
        upstream_task_label="upstream",
        upstream_passed=True,
    )

    assert captured["peer_review_enabled"] is False
    assert captured["max_rounds_at_call"] == 8, (
        "Cap must NOT fire when peer review is off (single-pass is already bounded)"
    )

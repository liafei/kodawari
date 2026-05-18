"""Tests for the work-all multi-slice loop (E1: epic replan)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.cli.runtime import work_all_runtime
from kodawari.cli.runtime.work_all_runtime import (
    MULTI_SLICE_STATE_FILENAME,
    _load_multi_slice_state,
    _read_prd_slices,
    _write_slice_prd,
)


def _multi_slice_prd(slice_count: int = 3) -> str:
    lines = [
        "# PRD: multi-feature",
        "",
        "## 目标",
        "Ship a multi-slice deliverable.",
        "",
    ]
    for i in range(1, slice_count + 1):
        lines.append(f"## Slice {i}: phase {i}")
        lines.append(f"Body of slice {i}. Multiple lines of detail.")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


def test_read_prd_slices_returns_empty_when_no_path() -> None:
    assert _read_prd_slices(None) == []
    assert _read_prd_slices("") == []


def test_read_prd_slices_returns_empty_when_path_missing(tmp_path: Path) -> None:
    assert _read_prd_slices(str(tmp_path / "missing.md")) == []


def test_read_prd_slices_returns_slices_from_real_file(tmp_path: Path) -> None:
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(_multi_slice_prd(3), encoding="utf-8")
    slices = _read_prd_slices(str(prd_path))
    assert len(slices) == 3
    assert slices[1]["title"] == "phase 2"


def test_write_slice_prd_creates_file_with_header(tmp_path: Path) -> None:
    slice_dir = tmp_path / "slice_00"
    slice_info = {"position": 0, "declared_index": 1, "title": "first", "content": "body text"}
    path = _write_slice_prd(slice_dir=slice_dir, feature="bookmark", slice_info=slice_info, total=3)
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "# bookmark — slice 1/3: first" in text
    assert "This is slice 1 of 3" in text
    assert "body text" in text


# ---------------------------------------------------------------------------
# Dispatcher branching
# ---------------------------------------------------------------------------


def _make_args(tmp_path: Path, prd_path: Path | None) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(tmp_path),
        feature="multi-test",
        planning_dir=None,
        prd=str(prd_path) if prd_path else None,
        requirements_file=None,
        task="",
        planner_route="auto",
        replan=False,
        executor_backend="",
        self_review_backend="",
        real_peer_review=None,
        force_rerun=False,
        base_branch="main",
        changed_file=[],
        scope_allow=[],
        command_file=None,
        command=None,
        eval_report_path=None,
        auto_eval=False,
        risk_profile="medium",
        release_gate_profile="strict",
        release_gate_path=["src"],
        verbose=0,
    )


def test_dispatch_single_slice_takes_legacy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PRD with no slice markers must NOT enter the multi-slice loop —
    the historical single-slice flow handles it unchanged. We pin this by
    stubbing the multi-slice runner and asserting it was never called."""
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text("# PRD\n\n## 目标\nSingle ship.\n", encoding="utf-8")

    called_multi: dict[str, bool] = {}

    def _stub_multi(**kwargs):
        called_multi["yes"] = True
        return 0

    monkeypatch.setattr(work_all_runtime, "_run_work_all_multi_slice", _stub_multi)

    # The legacy path will try to actually run plan/work/etc. We short-circuit
    # by stubbing invoke_cli_handler to return PASS immediately.
    monkeypatch.setattr(
        work_all_runtime,
        "invoke_cli_handler",
        lambda handler, namespace: (0, {"status": "PASS"}),
    )

    work_all_runtime.run_work_all_command(_make_args(tmp_path, prd_path))
    assert not called_multi, "single-slice PRD must NOT enter the multi-slice loop"


def test_dispatch_multi_slice_enters_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(_multi_slice_prd(2), encoding="utf-8")

    captured: dict[str, Any] = {}

    def _stub_multi(*, args, project_root, feature, planning_dir, prd_path, slices):
        captured["slices_count"] = len(slices)
        captured["feature"] = feature
        return 0

    monkeypatch.setattr(work_all_runtime, "_run_work_all_multi_slice", _stub_multi)

    rc = work_all_runtime.run_work_all_command(_make_args(tmp_path, prd_path))
    assert rc == 0
    assert captured.get("slices_count") == 2
    assert captured.get("feature") == "multi-test"


# ---------------------------------------------------------------------------
# Multi-slice runner integration (with stubbed plan/work invocation)
# ---------------------------------------------------------------------------


def test_multi_slice_loop_visits_each_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(_multi_slice_prd(3), encoding="utf-8")

    invocations: list[tuple[str, str]] = []

    def _fake_handler(handler, namespace):
        name = handler.__name__
        invocations.append((name, str(getattr(namespace, "planning_dir", "")) or ""))
        return 0, {"status": "PASS"}

    monkeypatch.setattr(work_all_runtime, "invoke_cli_handler", _fake_handler)

    rc = work_all_runtime.run_work_all_command(_make_args(tmp_path, prd_path))
    capsys.readouterr()
    assert rc == 0

    # 3 slices × (plan + work) + 1 review + 1 release = 8 handler calls.
    plan_calls = [inv for inv in invocations if inv[0] == "run_plan_command"]
    work_calls = [inv for inv in invocations if inv[0] == "run_autopilot_command"]
    review_calls = [inv for inv in invocations if inv[0] == "run_review_command"]
    release_calls = [inv for inv in invocations if inv[0] == "run_release_command"]
    assert len(plan_calls) == 3
    assert len(work_calls) == 3
    assert len(review_calls) == 1
    assert len(release_calls) == 1

    # Each slice's plan went to its own planning_dir.
    plan_dirs = {Path(inv[1]).name for inv in plan_calls}
    assert plan_dirs == {"slice_00", "slice_01", "slice_02"}


def test_multi_slice_loop_halts_on_plan_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(_multi_slice_prd(3), encoding="utf-8")

    call_count: list[int] = []

    def _fake_handler(handler, namespace):
        call_count.append(1)
        if handler.__name__ == "run_plan_command" and len(call_count) >= 3:
            return 2, {"status": "FAIL", "summary": "planner blew up"}
        return 0, {"status": "PASS"}

    monkeypatch.setattr(work_all_runtime, "invoke_cli_handler", _fake_handler)

    rc = work_all_runtime.run_work_all_command(_make_args(tmp_path, prd_path))
    capsys.readouterr()
    assert rc == 2

    state = _load_multi_slice_state(tmp_path / "planning" / "multi-test")
    assert state["status"] == "halted"
    assert state["current_position"] == 1
    assert state["completed_positions"] == [0]


def test_multi_slice_loop_resumes_skipping_completed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A re-invocation with completed slices in state must skip them
    instead of redoing work — same semantic as the single-slice
    manifest resume."""
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(_multi_slice_prd(3), encoding="utf-8")

    # Pre-seed multi-slice state showing slice 0 already done.
    planning_dir = tmp_path / "planning" / "multi-test"
    planning_dir.mkdir(parents=True)
    (planning_dir / MULTI_SLICE_STATE_FILENAME).write_text(
        json.dumps({
            "schema_version": "workflow.multi_slice_state.v1",
            "feature": "multi-test",
            "total_slices": 3,
            "completed_positions": [0],
            "current_position": 1,
            "status": "halted",
            "slices": [],
            "updated_at": "2026-01-01T00:00:00Z",
        }),
        encoding="utf-8",
    )

    invocations: list[str] = []

    def _fake_handler(handler, namespace):
        invocations.append(handler.__name__)
        return 0, {"status": "PASS"}

    monkeypatch.setattr(work_all_runtime, "invoke_cli_handler", _fake_handler)

    rc = work_all_runtime.run_work_all_command(_make_args(tmp_path, prd_path))
    capsys.readouterr()
    assert rc == 0

    # Slice 0 must be skipped → only 2 plan + 2 work calls (slices 1 + 2),
    # plus 1 review + 1 release at the end.
    plan_count = sum(1 for n in invocations if n == "run_plan_command")
    work_count = sum(1 for n in invocations if n == "run_autopilot_command")
    assert plan_count == 2
    assert work_count == 2


def test_multi_slice_loop_force_rerun_ignores_completion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(_multi_slice_prd(2), encoding="utf-8")

    planning_dir = tmp_path / "planning" / "multi-test"
    planning_dir.mkdir(parents=True)
    (planning_dir / MULTI_SLICE_STATE_FILENAME).write_text(
        json.dumps({
            "schema_version": "workflow.multi_slice_state.v1",
            "completed_positions": [0],
            "current_position": None,
            "status": "all_slices_complete",
        }),
        encoding="utf-8",
    )

    invocations: list[str] = []

    def _fake_handler(handler, namespace):
        invocations.append(handler.__name__)
        return 0, {"status": "PASS"}

    monkeypatch.setattr(work_all_runtime, "invoke_cli_handler", _fake_handler)

    args = _make_args(tmp_path, prd_path)
    args.force_rerun = True
    work_all_runtime.run_work_all_command(args)
    capsys.readouterr()

    plan_count = sum(1 for n in invocations if n == "run_plan_command")
    assert plan_count == 2, "--force-rerun must redo all slices, not skip via state"

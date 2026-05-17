"""Tests for D1 — autopilot --max-wall-clock-seconds + ABORT_REPORT.json."""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import pytest

from kodawari.cli.main import build_parser
from kodawari.cli.runtime import autopilot_cmd


def test_work_all_parser_accepts_max_wall_clock_seconds() -> None:
    """Regression: --max-wall-clock-seconds was originally added only to the
    `autopilot` parser; `work-all` (which delegates to autopilot internally
    via Namespace forwarding) silently rejected the flag because its parser
    didn't declare it. This pins the parser-level acceptance so the
    forwarding chain stays intact."""
    parser = build_parser()
    for entry in ("autopilot", "work-all", "work", "wf-work", "wf-work-all"):
        args = parser.parse_args([entry, "--feature", "x", "--max-wall-clock-seconds", "1800"])
        assert getattr(args, "max_wall_clock_seconds", None) == 1800, (
            f"{entry} parser must accept --max-wall-clock-seconds (work-all delegates to "
            "autopilot via Namespace forwarding; missing flag silently drops the budget)"
        )


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    defaults = dict(
        project_root=str(tmp_path),
        planning_dir=str(tmp_path / "planning"),
        feature="wallclock-test",
        max_wall_clock_seconds=0,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_resolve_abort_planning_dir_prefers_explicit_planning_dir(tmp_path: Path) -> None:
    explicit = tmp_path / "custom-planning"
    args = _args(tmp_path, planning_dir=str(explicit))
    assert autopilot_cmd._resolve_abort_planning_dir(args) == explicit.resolve()


def test_resolve_abort_planning_dir_derives_from_feature(tmp_path: Path) -> None:
    args = argparse.Namespace(project_root=str(tmp_path), feature="my-feature")
    resolved = autopilot_cmd._resolve_abort_planning_dir(args)
    assert resolved == (tmp_path / "planning" / "my-feature").resolve()


def test_write_abort_report_persists_metadata(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning"
    path = autopilot_cmd._write_abort_report(
        planning_dir=planning_dir,
        budget_seconds=30,
        elapsed_seconds=31.4,
        feature="test-feat",
        cause="wall_clock_budget_exceeded",
    )
    assert path is not None
    assert path.exists()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "abort_report.v1"
    assert payload["feature"] == "test-feat"
    assert payload["cause"] == "wall_clock_budget_exceeded"
    assert payload["budget_seconds"] == 30
    assert payload["elapsed_seconds"] == 31.4
    assert payload["exit_code"] == 124


def test_wall_clock_watchdog_no_op_when_budget_zero() -> None:
    """D1: budget==0 means feature is disabled — no watchdog thread spawned."""
    event = threading.Event()
    thread = autopilot_cmd._start_wall_clock_watchdog(
        budget_seconds=0,
        on_expire=event,
    )
    assert thread is None


def test_wall_clock_watchdog_skips_signal_when_event_set_first() -> None:
    """D1: if autopilot finishes before budget, watchdog sees the event set
    and aborts WITHOUT raising SIGINT — avoid spurious interrupts on success."""
    event = threading.Event()
    thread = autopilot_cmd._start_wall_clock_watchdog(
        budget_seconds=10,
        on_expire=event,
    )
    assert thread is not None
    event.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive(), "watchdog must exit cleanly when event is set"


def test_run_autopilot_with_wall_clock_writes_abort_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D1 end-to-end: when the inner body takes longer than budget, the wrapper
    catches KeyboardInterrupt, writes ABORT_REPORT.json, returns exit 124."""
    # _resolve_abort_planning_dir honors explicit args.planning_dir as-is (does
    # not append feature). _args() above passes planning_dir=tmp_path/planning.
    planning_dir = tmp_path / "planning"

    def _slow_inner(args: argparse.Namespace) -> int:
        # Simulate the inner autopilot taking longer than the 1s budget.
        # Sleep in small slices so the watchdog SIGINT actually delivers.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
        return 0  # would-be success ignored because watchdog interrupted

    monkeypatch.setattr(autopilot_cmd, "_run_autopilot_inner", _slow_inner)

    args = _args(tmp_path, max_wall_clock_seconds=1)
    rc = autopilot_cmd._run_autopilot_with_wall_clock(args=args, budget_seconds=1)

    assert rc == 124, f"expected exit 124 (POSIX timeout); got {rc}"
    report_path = planning_dir / "ABORT_REPORT.json"
    assert report_path.exists(), f"ABORT_REPORT.json must be written to {planning_dir}"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["cause"] == "wall_clock_budget_exceeded"
    assert payload["budget_seconds"] == 1
    assert payload["elapsed_seconds"] >= 1.0


def test_run_autopilot_with_wall_clock_propagates_real_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D1: a KeyboardInterrupt raised WELL BEFORE budget expiry is a genuine
    user interrupt (Ctrl-C), not the watchdog — propagate so the user's signal
    is honored instead of swallowed into an abort report."""
    def _quick_interrupt(args: argparse.Namespace) -> int:
        raise KeyboardInterrupt()

    monkeypatch.setattr(autopilot_cmd, "_run_autopilot_inner", _quick_interrupt)

    args = _args(tmp_path, max_wall_clock_seconds=60)
    with pytest.raises(KeyboardInterrupt):
        autopilot_cmd._run_autopilot_with_wall_clock(args=args, budget_seconds=60)


def test_run_autopilot_command_dispatches_to_wall_clock_when_budget_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called_with: dict[str, object] = {}

    def _capture(*, args: argparse.Namespace, budget_seconds: int) -> int:
        called_with["budget"] = budget_seconds
        return 0

    monkeypatch.setattr(autopilot_cmd, "_run_autopilot_with_wall_clock", _capture)

    args = _args(tmp_path, max_wall_clock_seconds=42)
    rc = autopilot_cmd.run_autopilot_command(args)
    assert rc == 0
    assert called_with["budget"] == 42


def test_run_autopilot_command_skips_wall_clock_when_budget_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BC: when --max-wall-clock-seconds is 0 (default), the wrapper must be
    bypassed entirely so existing autopilot runs are byte-for-byte identical."""
    called_with: list[argparse.Namespace] = []

    def _capture_inner(args: argparse.Namespace) -> int:
        called_with.append(args)
        return 7

    monkeypatch.setattr(autopilot_cmd, "_run_autopilot_inner", _capture_inner)

    args = _args(tmp_path, max_wall_clock_seconds=0)
    rc = autopilot_cmd.run_autopilot_command(args)
    assert rc == 7
    assert len(called_with) == 1

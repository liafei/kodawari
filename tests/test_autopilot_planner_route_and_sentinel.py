"""Tests for priority-#4 follow-ups:

  - ``--planner-route`` is now honored in the autopilot main path
    (``maybe_bootstrap_contract_first``), not just the standalone ``plan``
    subcommand.

  - Sentinel-based timeout recovery and ``execution_timeout_hint`` are now
    backend-agnostic (live in ``core/execution_sentinel.py``); both
    ``codex_cli`` and ``claude_code`` executors must honor them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.autopilot.core.execution_sentinel import (
    SENTINEL_FILENAME,
    TIMEOUT_HINT_MAP,
    read_sentinel,
    resolve_timeout_seconds,
    sentinel_indicates_verify_passed,
)


# --- execution_sentinel: shared protocol ----------------------------------


def test_resolve_timeout_seconds_hint_map_overrides_config() -> None:
    class _Cfg:
        timeout_seconds = 600

    cfg = _Cfg()
    assert resolve_timeout_seconds(cfg, {"execution_timeout_hint": "fast"}) == TIMEOUT_HINT_MAP["fast"]
    assert resolve_timeout_seconds(cfg, {"execution_timeout_hint": "normal"}) == TIMEOUT_HINT_MAP["normal"]
    assert resolve_timeout_seconds(cfg, {"execution_timeout_hint": "heavy"}) == TIMEOUT_HINT_MAP["heavy"]


def test_resolve_timeout_seconds_no_hint_falls_back_to_config() -> None:
    class _Cfg:
        timeout_seconds = 999

    assert resolve_timeout_seconds(_Cfg(), {}) == 999
    assert resolve_timeout_seconds(_Cfg(), {"execution_timeout_hint": ""}) == 999
    assert resolve_timeout_seconds(_Cfg(), {"execution_timeout_hint": "bogus"}) == 999


def test_resolve_timeout_seconds_zero_config_uses_safe_default() -> None:
    class _Cfg:
        timeout_seconds = 0

    assert resolve_timeout_seconds(_Cfg(), {}) == 600


def test_read_sentinel_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_sentinel(tmp_path) is None


def test_read_sentinel_parses_valid_payload(tmp_path: Path) -> None:
    (tmp_path / SENTINEL_FILENAME).write_text(
        json.dumps({"status": "verify_passed", "task_id": "T1"}),
        encoding="utf-8",
    )
    payload = read_sentinel(tmp_path)
    assert payload == {"status": "verify_passed", "task_id": "T1"}
    assert sentinel_indicates_verify_passed(payload) is True


def test_read_sentinel_rejects_invalid_json(tmp_path: Path) -> None:
    (tmp_path / SENTINEL_FILENAME).write_text("not json {", encoding="utf-8")
    assert read_sentinel(tmp_path) is None


def test_read_sentinel_rejects_non_dict(tmp_path: Path) -> None:
    (tmp_path / SENTINEL_FILENAME).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert read_sentinel(tmp_path) is None


def test_sentinel_indicates_verify_passed_handles_other_statuses() -> None:
    assert sentinel_indicates_verify_passed({"status": "verify_passed"}) is True
    assert sentinel_indicates_verify_passed({"status": "VERIFY_PASSED"}) is True  # case-insensitive
    assert sentinel_indicates_verify_passed({"status": "in_progress"}) is False
    assert sentinel_indicates_verify_passed({"status": ""}) is False
    assert sentinel_indicates_verify_passed(None) is False
    assert sentinel_indicates_verify_passed({}) is False


def test_claude_code_executor_imports_shared_sentinel_helpers() -> None:
    """Regression: claude_code must use the shared sentinel module, not hard-fail on timeout."""
    from kodawari.autopilot.execution import execution_claude_code

    assert hasattr(execution_claude_code, "read_sentinel")
    assert hasattr(execution_claude_code, "resolve_timeout_seconds")
    assert hasattr(execution_claude_code, "sentinel_indicates_verify_passed")


def test_codex_cli_executor_imports_shared_sentinel_helpers() -> None:
    from kodawari.autopilot.execution import execution_codex_cli

    assert hasattr(execution_codex_cli, "read_sentinel")
    assert hasattr(execution_codex_cli, "resolve_timeout_seconds")
    assert hasattr(execution_codex_cli, "sentinel_indicates_verify_passed")


# --- planner-route in autopilot main path ---------------------------------


def _stub_args(**overrides: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "task": "",
        "planner_route": "auto",
        "prd": None,
        "requirements_file": None,
        "feature": "demo",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_planner_route_explicit_model_forces_model_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--planner-route=model overrides legacy heuristic in autopilot main path."""
    from kodawari.cli.runtime import autopilot_runtime_flow

    captured: dict[str, Any] = {}

    def _fake_ensure(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()  # treated as truthy snapshot

    monkeypatch.setattr(autopilot_runtime_flow, "ensure_contract_first_planning", _fake_ensure)
    monkeypatch.setattr(autopilot_runtime_flow, "should_use_contract_first_bridge", lambda *a, **k: True)

    planning_dir = tmp_path / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    # Note: NO PLANNING_CONVERSATION.json and NO --task — under legacy heuristic
    # this would default to use_model_planning=False. With --planner-route=model
    # the explicit value MUST win.
    args = _stub_args(planner_route="model")

    autopilot_runtime_flow.maybe_bootstrap_contract_first(
        args=args,
        project_root=tmp_path,
        planning_dir=planning_dir,
        state_path=tmp_path / ".autopilot_state.json",
        feature="demo",
        requirements_file=None,
    )
    assert captured.get("use_model_planning") is True


def test_planner_route_explicit_generic_forces_generic_planner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kodawari.cli.runtime import autopilot_runtime_flow

    captured: dict[str, Any] = {}

    def _fake_ensure(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(autopilot_runtime_flow, "ensure_contract_first_planning", _fake_ensure)
    monkeypatch.setattr(autopilot_runtime_flow, "should_use_contract_first_bridge", lambda *a, **k: True)

    planning_dir = tmp_path / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    # Adversarial: PLANNING_CONVERSATION.json EXISTS (legacy heuristic would
    # return True). With --planner-route=generic the explicit value MUST win.
    (planning_dir / "PLANNING_CONVERSATION.json").write_text("{}", encoding="utf-8")
    args = _stub_args(planner_route="generic")

    autopilot_runtime_flow.maybe_bootstrap_contract_first(
        args=args,
        project_root=tmp_path,
        planning_dir=planning_dir,
        state_path=tmp_path / ".autopilot_state.json",
        feature="demo",
        requirements_file=None,
    )
    assert captured.get("use_model_planning") is False


def test_planner_route_auto_preserves_legacy_heuristic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--planner-route=auto (default) keeps the old PLANNING_CONVERSATION.json signal."""
    from kodawari.cli.runtime import autopilot_runtime_flow

    captured: dict[str, Any] = {}

    def _fake_ensure(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(autopilot_runtime_flow, "ensure_contract_first_planning", _fake_ensure)
    monkeypatch.setattr(autopilot_runtime_flow, "should_use_contract_first_bridge", lambda *a, **k: True)
    monkeypatch.setattr(autopilot_runtime_flow, "is_test_environment", lambda: False)
    # explicit_planning_input_requested checks --task and --prd; we leave both empty.
    monkeypatch.setattr(autopilot_runtime_flow, "explicit_planning_input_requested", lambda args: False)

    planning_dir = tmp_path / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    (planning_dir / "PLANNING_CONVERSATION.json").write_text("{}", encoding="utf-8")
    args = _stub_args(planner_route="auto")

    autopilot_runtime_flow.maybe_bootstrap_contract_first(
        args=args,
        project_root=tmp_path,
        planning_dir=planning_dir,
        state_path=tmp_path / ".autopilot_state.json",
        feature="demo",
        requirements_file=None,
    )
    # legacy heuristic: PLANNING_CONVERSATION.json present => True
    assert captured.get("use_model_planning") is True

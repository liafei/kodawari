"""Tests for Phase 4: diff_scope_guard (hard constraint) + scope prompt (soft constraint).

锁定行为：
- guard_diff_scope: 越界文件 → blocked=True；在范围内 → blocked=False
- allowed 为空时不做限制（无 task card 时放行）
- new_files 被视为合法（executor 新建的文件）
- scoped_executor_enabled: 默认关闭，env=1 时开启
- render_scope_constraint_lines: 关闭时返回空；开启时输出 do_not_change / target_symbols / read_only_symbols
- gate_round: WORKFLOW_SCOPED_EXECUTOR=1 + 越界 → DIFF_SCOPE_VIOLATION 终止
"""
from __future__ import annotations

import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from kodawari.autopilot.execution.diff_scope_guard import (
    DiffScopeReport,
    guard_diff_scope,
    scoped_executor_enabled,
)
from kodawari.autopilot.execution.execution_prompt_common import render_scope_constraint_lines
from kodawari.autopilot.engine.gate_round import _check_diff_scope, _finish_scope_violation


# ---------------------------------------------------------------------------
# scoped_executor_enabled
# ---------------------------------------------------------------------------

def test_scoped_executor_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SCOPED_EXECUTOR", raising=False)
    assert scoped_executor_enabled() is False


def test_scoped_executor_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1")
    assert scoped_executor_enabled() is True


# ---------------------------------------------------------------------------
# guard_diff_scope
# ---------------------------------------------------------------------------

def test_guard_passes_when_all_files_in_scope() -> None:
    report = guard_diff_scope(
        changed_files=["src/a.py", "src/b.py"],
        files_to_change=["src/a.py", "src/b.py"],
        new_files=[],
    )
    assert report.blocked is False
    assert report.out_of_scope_files == ()


def test_guard_passes_when_new_file_created() -> None:
    report = guard_diff_scope(
        changed_files=["src/new_module.py"],
        files_to_change=["src/existing.py"],
        new_files=["src/new_module.py"],
    )
    assert report.blocked is False


def test_guard_blocks_when_out_of_scope_file_modified() -> None:
    report = guard_diff_scope(
        changed_files=["src/a.py", "tests/unrelated.py"],
        files_to_change=["src/a.py"],
        new_files=[],
    )
    assert report.blocked is True
    assert "tests/unrelated.py" in report.out_of_scope_files


def test_guard_passes_when_allowed_is_empty() -> None:
    """No task card (empty allowed list) → no restriction."""
    report = guard_diff_scope(
        changed_files=["src/anything.py"],
        files_to_change=[],
        new_files=[],
    )
    assert report.blocked is False


def test_guard_normalizes_windows_backslash() -> None:
    report = guard_diff_scope(
        changed_files=["src\\a.py"],
        files_to_change=["src/a.py"],
        new_files=[],
    )
    assert report.blocked is False


def test_guard_to_dict() -> None:
    report = guard_diff_scope(
        changed_files=["src/out.py"],
        files_to_change=["src/allowed.py"],
        new_files=[],
    )
    d = report.to_dict()
    assert d["blocked"] is True
    assert "src/out.py" in d["out_of_scope_files"]


def test_guard_passes_when_no_changed_files() -> None:
    report = guard_diff_scope(
        changed_files=[],
        files_to_change=["src/a.py"],
        new_files=[],
    )
    assert report.blocked is False


# ---------------------------------------------------------------------------
# _check_diff_scope (gate_round helper)
# ---------------------------------------------------------------------------

def _make_engine(card=None, *, enabled=True, monkeypatch=None) -> Any:
    if monkeypatch is not None:
        monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1" if enabled else "0")
    eng = SimpleNamespace(_task_card_payload=card)
    return eng


def _make_runtime(changed_files=None) -> Any:
    return SimpleNamespace(last_changed_files=changed_files or [])


def test_check_diff_scope_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SCOPED_EXECUTOR", raising=False)
    eng = _make_engine(card={"files_to_change": ["src/a.py"]})
    rt = _make_runtime(["src/out.py"])
    assert _check_diff_scope(eng, rt) is None


def test_check_diff_scope_returns_none_when_no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1")
    eng = _make_engine(card=None)
    rt = _make_runtime(["src/a.py"])
    assert _check_diff_scope(eng, rt) is None


def test_check_diff_scope_returns_report_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1")
    eng = _make_engine(card={"files_to_change": ["src/a.py"], "new_files": []})
    rt = _make_runtime(["src/a.py", "src/out.py"])
    report = _check_diff_scope(eng, rt)
    assert report is not None
    assert report.blocked is True
    assert "src/out.py" in report.out_of_scope_files


# ---------------------------------------------------------------------------
# render_scope_constraint_lines
# ---------------------------------------------------------------------------

def test_scope_lines_empty_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_SCOPED_EXECUTOR", raising=False)
    lines = render_scope_constraint_lines({"task_card": {"do_not_change": ["ranking weights"]}})
    assert lines == []


def test_scope_lines_do_not_change(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1")
    payload = {
        "task_card": {
            "do_not_change": ["ranking weights", "candidate pool size"],
        }
    }
    lines = render_scope_constraint_lines(payload)
    combined = "\n".join(lines)
    assert "do NOT change" in combined
    assert "ranking weights" in combined
    assert "candidate pool size" in combined


def test_scope_lines_target_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1")
    payload = {
        "task_card": {
            "target_symbols": [
                {"kind": "method", "class": "TopListService", "name": "_format_channel_layout", "file": "services/top_list.py"}
            ]
        }
    }
    lines = render_scope_constraint_lines(payload)
    combined = "\n".join(lines)
    assert "Target symbols" in combined
    assert "TopListService._format_channel_layout" in combined


def test_scope_lines_read_only_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1")
    payload = {
        "task_card": {
            "read_only_symbols": [
                {"kind": "method", "class": "TopListService", "name": "_rank_channels", "file": "services/top_list.py"}
            ]
        }
    }
    lines = render_scope_constraint_lines(payload)
    combined = "\n".join(lines)
    assert "Read-only symbols" in combined
    assert "TopListService._rank_channels" in combined
    assert "do NOT modify" in combined


def test_scope_lines_empty_card(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_SCOPED_EXECUTOR", "1")
    lines = render_scope_constraint_lines({"task_card": {}})
    assert lines == []

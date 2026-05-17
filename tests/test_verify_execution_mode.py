"""Tests for verify_execution._execution_mode regression fix.

Pins the post-fix behavior where code-only rounds whose paired test file lives
under a non-heuristic-friendly name (e.g. test_api.py for app/main.py) still
get verify executed instead of silently skipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kodawari.autopilot.execution.verify_execution import (
    DEFAULT_VERIFY_CMD,
    _execution_mode,
    _project_has_tests_directory,
    maybe_execute_verify_command,
)


def test_execution_mode_returns_empty_for_empty_verify_cmd(tmp_path: Path) -> None:
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd="",
        changed_files=["app/main.py"],
    ) == ""


def test_execution_mode_returns_empty_for_empty_changed_files(tmp_path: Path) -> None:
    """Nothing changed, nothing to verify."""
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd=DEFAULT_VERIFY_CMD,
        changed_files=[],
    ) == ""


def test_execution_mode_returns_explicit_for_non_default_cmd(tmp_path: Path) -> None:
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd="pytest tests/test_api.py",
        changed_files=["app/main.py"],
    ) == "explicit"


def test_execution_mode_returns_detected_when_targets_resolved(tmp_path: Path) -> None:
    """When verify_targeting resolved scoped targets, run."""
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd=DEFAULT_VERIFY_CMD,
        changed_files=["app/main.py"],
        verify_targets=["tests/test_main.py"],
        verify_target_source="derived_files",
    ) == "detected"


def test_execution_mode_returns_detected_when_source_non_default(tmp_path: Path) -> None:
    """Empty targets but a non-default source still means resolution succeeded;
    e.g. instinct hints can produce source='instinct_hints' with no explicit
    target paths but the resolution still meant something."""
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd=DEFAULT_VERIFY_CMD,
        changed_files=["app/main.py"],
        verify_targets=[],
        verify_target_source="derived_files",
    ) == "detected"


def test_execution_mode_returns_broad_when_tests_dir_exists_but_heuristic_missed(tmp_path: Path) -> None:
    """REGRESSION pin for the greenfield-bookmark T4 case: code-only round on
    app/main.py with a paired test under tests/test_api.py — the heuristic
    looked for tests/test_main.py and missed it. Pre-fix: silent skip. Post-fix:
    we still detect tests/ presence and run broad pytest -q rather than silently
    pass."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_api.py").write_text("def test_x(): pass\n", encoding="utf-8")
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd=DEFAULT_VERIFY_CMD,
        changed_files=["app/main.py"],
        verify_targets=[],
        verify_target_source="default",
    ) == "broad"


def test_execution_mode_returns_empty_when_no_tests_at_all(tmp_path: Path) -> None:
    """Truly testless projects: skip is correct (avoid 'no tests collected' churn)."""
    (tmp_path / "app").mkdir()
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd=DEFAULT_VERIFY_CMD,
        changed_files=["app/main.py"],
        verify_targets=[],
        verify_target_source="default",
    ) == ""


def test_execution_mode_empty_tests_dir_does_not_count(tmp_path: Path) -> None:
    """tests/ exists but is empty — heuristic should NOT trigger broad run."""
    (tmp_path / "tests").mkdir()
    assert _execution_mode(
        project_root=tmp_path,
        verify_cmd=DEFAULT_VERIFY_CMD,
        changed_files=["app/main.py"],
        verify_targets=[],
        verify_target_source="default",
    ) == ""


def test_project_has_tests_directory_detects_test_singular(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    (tmp_path / "test" / "test_x.py").write_text("", encoding="utf-8")
    assert _project_has_tests_directory(tmp_path) is True


def test_maybe_execute_verify_command_does_not_run_when_mode_empty(tmp_path: Path) -> None:
    """End-to-end gate: when _execution_mode says skip, the outer function
    must return None and NOT subprocess.run anything."""
    result = maybe_execute_verify_command(
        project_root=tmp_path,
        feature="x",
        task_label="T1",
        verify_cmd=DEFAULT_VERIFY_CMD,
        changed_files=[],
    )
    assert result is None

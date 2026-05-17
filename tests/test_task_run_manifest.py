"""Tests for the task-run carry-over manifest (#2 structural fix).

Closes the loop where ``sync_isolated_workspace_to_project_root`` leaves
``backend/main.py`` dirty in project_root after a successful run, then the
next task-run's worktree preflight blocks with ``DIRTY_WORKTREE_BLOCKED``.

Contract:
  - Same-task retry MUST not be blocked by files the previous run wrote.
  - Cross-task contamination MUST still be blocked (the user has to resolve
    a different task's leftovers explicitly — auto-clearing is unsafe).
  - Missing/invalid manifest MUST degrade to the conservative pre-fix
    behavior (no carry-over).
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.runtime.task_run_manifest import (
    MANIFEST_FILENAME,
    MANIFEST_SCHEMA_VERSION,
    carry_over_files_for_task,
    read_task_run_manifest,
    write_task_run_manifest,
)


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T2",
        status="BLOCKED",
        carried_files=["backend/main.py", "backend/api/v1/router.py"],
    )
    payload = read_task_run_manifest(tmp_path)
    assert payload is not None
    assert payload["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert payload["task_id"] == "T2"
    assert payload["status"] == "BLOCKED"
    assert payload["carried_files"] == ["backend/main.py", "backend/api/v1/router.py"]
    assert "completed_at" in payload


def test_read_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_task_run_manifest(tmp_path) is None


def test_read_returns_none_for_corrupt_json(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text("not json {", encoding="utf-8")
    assert read_task_run_manifest(tmp_path) is None


def test_read_returns_none_for_non_dict_payload(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_FILENAME).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert read_task_run_manifest(tmp_path) is None


def test_carry_over_matches_same_task_id(tmp_path: Path) -> None:
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T2",
        status="BLOCKED",
        carried_files=["backend/main.py"],
    )
    files = carry_over_files_for_task(planning_dir=tmp_path, task_id="T2")
    assert files == ["backend/main.py"]


def test_carry_over_rejects_different_task_id(tmp_path: Path) -> None:
    """Cross-task contamination MUST not be auto-cleared.

    If T1 left main.py dirty and the user starts T2, the new preflight has
    no business assuming T2 is allowed to touch T1's leftovers. Forcing
    explicit user resolution here is the safe default.
    """
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T1",
        status="PASS",
        carried_files=["backend/main.py"],
    )
    assert carry_over_files_for_task(planning_dir=tmp_path, task_id="T2") == []


def test_carry_over_returns_empty_when_manifest_missing(tmp_path: Path) -> None:
    assert carry_over_files_for_task(planning_dir=tmp_path, task_id="T2") == []


def test_carry_over_rejects_unknown_schema_version(tmp_path: Path) -> None:
    """Unknown schema versions MUST degrade to no-carry-over (safe default)."""
    (tmp_path / MANIFEST_FILENAME).write_text(
        json.dumps({
            "schema_version": "task_run_manifest.v999",
            "task_id": "T2",
            "carried_files": ["backend/main.py"],
        }),
        encoding="utf-8",
    )
    assert carry_over_files_for_task(planning_dir=tmp_path, task_id="T2") == []


def test_write_normalizes_path_separators(tmp_path: Path) -> None:
    """Windows backslashes in carried_files must be normalized to forward slashes
    so the carry-over comparison against baseline core_dirty_files (which is
    already forward-slash) works on both platforms."""
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T2",
        status="PASS",
        carried_files=["backend\\main.py", "backend/api/router.py"],
    )
    files = carry_over_files_for_task(planning_dir=tmp_path, task_id="T2")
    assert files == ["backend/main.py", "backend/api/router.py"]


def test_write_strips_empty_entries(tmp_path: Path) -> None:
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T2",
        status="PASS",
        carried_files=["backend/main.py", "", "  ", "tests/test.py"],
    )
    files = carry_over_files_for_task(planning_dir=tmp_path, task_id="T2")
    assert files == ["backend/main.py", "tests/test.py"]


def test_write_merges_same_task_carried_files(tmp_path: Path) -> None:
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T2",
        status="BLOCKED",
        carried_files=["backend/provider.py", "tests/test_provider.py"],
    )
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T2",
        status="PASS",
        carried_files=["backend\\provider.py", "backend/__init__.py"],
    )

    files = carry_over_files_for_task(planning_dir=tmp_path, task_id="T2")
    assert files == ["backend/provider.py", "tests/test_provider.py", "backend/__init__.py"]


def test_write_replaces_different_task_carried_files(tmp_path: Path) -> None:
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T1",
        status="PASS",
        carried_files=["backend/old.py"],
    )
    write_task_run_manifest(
        planning_dir=tmp_path,
        task_id="T2",
        status="PASS",
        carried_files=["backend/new.py"],
    )

    assert carry_over_files_for_task(planning_dir=tmp_path, task_id="T1") == []
    assert carry_over_files_for_task(planning_dir=tmp_path, task_id="T2") == ["backend/new.py"]


def test_write_creates_planning_dir_if_missing(tmp_path: Path) -> None:
    nested = tmp_path / "planning" / "feature-x"
    write_task_run_manifest(
        planning_dir=nested,
        task_id="T1",
        status="PASS",
        carried_files=["a.py"],
    )
    assert (nested / MANIFEST_FILENAME).exists()

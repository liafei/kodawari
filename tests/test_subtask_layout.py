"""Per-subtask planning directory layout."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodawari.autopilot.planning.subtask_layout import (
    SUBTASKS_DIRNAME,
    is_subtask_planning_dir,
    sanitize_subtask_id,
    subtask_planning_dir,
)


def test_canonical_layout_is_under_subtasks_dir(tmp_path: Path) -> None:
    parent = tmp_path / "planning" / "feat"
    parent.mkdir(parents=True)
    sub = subtask_planning_dir(parent, "T01.A")
    assert sub == parent / "_subtasks" / "T01.A"
    assert sub.parent.name == SUBTASKS_DIRNAME


def test_helper_does_not_create_directory(tmp_path: Path) -> None:
    parent = tmp_path / "planning" / "feat"
    parent.mkdir(parents=True)
    sub = subtask_planning_dir(parent, "T01")
    # Caller is responsible for mkdir; the helper must stay side-effect-free.
    assert not sub.exists()


def test_two_subtasks_resolve_to_distinct_dirs(tmp_path: Path) -> None:
    parent = tmp_path / "planning" / "feat"
    parent.mkdir(parents=True)
    a = subtask_planning_dir(parent, "T01.A")
    b = subtask_planning_dir(parent, "T01.B")
    assert a != b
    assert a.parent == b.parent  # both under _subtasks/


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("T01.A", "T01.A"),
        ("path/with/slashes", "path_with_slashes"),
        ("name with spaces", "name_with_spaces"),
        ("..weird..", "weird"),
        ("", "subtask"),
        ("///", "subtask"),
    ],
)
def test_sanitize_subtask_id(raw: str, expected: str) -> None:
    assert sanitize_subtask_id(raw) == expected


def test_is_subtask_planning_dir(tmp_path: Path) -> None:
    parent = tmp_path / "planning" / "feat"
    parent.mkdir(parents=True)
    sub = subtask_planning_dir(parent, "T01")
    sub.mkdir(parents=True)
    assert is_subtask_planning_dir(sub)
    assert not is_subtask_planning_dir(parent)


def test_artifacts_in_subtask_dir_do_not_collide(tmp_path: Path) -> None:
    """Two subtasks writing the same canonical artifact name stay separate."""
    parent = tmp_path / "planning" / "feat"
    parent.mkdir(parents=True)
    a_dir = subtask_planning_dir(parent, "T01.A")
    b_dir = subtask_planning_dir(parent, "T01.B")
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    (a_dir / ".review_result.json").write_text("a payload", encoding="utf-8")
    (b_dir / ".review_result.json").write_text("b payload", encoding="utf-8")
    assert (a_dir / ".review_result.json").read_text(encoding="utf-8") == "a payload"
    assert (b_dir / ".review_result.json").read_text(encoding="utf-8") == "b payload"

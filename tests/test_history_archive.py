"""Planning artifact archive on rerun."""

from __future__ import annotations

from pathlib import Path

from kodawari.cli.evidence.history_archive import HISTORY_DIRNAME, archive_planning_artifacts


def _seed(planning_dir: Path, files: dict[str, str]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (planning_dir / name).write_text(content, encoding="utf-8")


def test_no_op_when_no_artifacts(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    result = archive_planning_artifacts(planning_dir)
    assert result is None
    # .history directory was not created (no archive happened)
    assert not (planning_dir / HISTORY_DIRNAME).exists()


def test_moves_known_artifacts_into_timestamped_subdir(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    _seed(
        planning_dir,
        {
            ".execution_result.json": "{}",
            ".verify_report.json": "{}",
            "REVIEW.md": "# review",
        },
    )

    archive = archive_planning_artifacts(planning_dir, timestamp="20260504T120000Z")
    assert archive is not None
    assert archive.name == "20260504T120000Z"
    assert archive.parent == planning_dir / HISTORY_DIRNAME

    # Original locations cleared, archive populated
    assert not (planning_dir / ".execution_result.json").exists()
    assert not (planning_dir / ".verify_report.json").exists()
    assert not (planning_dir / "REVIEW.md").exists()
    assert (archive / ".execution_result.json").read_text(encoding="utf-8") == "{}"
    assert (archive / ".verify_report.json").exists()
    assert (archive / "REVIEW.md").exists()


def test_skips_unknown_files(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    _seed(
        planning_dir,
        {
            ".execution_result.json": "{}",
            "scratch.tmp": "ignored",
            "README.md": "ignored",
        },
    )

    archive = archive_planning_artifacts(planning_dir)
    assert archive is not None
    # Known artifact moved
    assert (archive / ".execution_result.json").exists()
    # Unknown files stay in place
    assert (planning_dir / "scratch.tmp").exists()
    assert (planning_dir / "README.md").exists()


def test_extra_names_archives_additional_files(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    _seed(planning_dir, {"custom.json": "{}"})
    archive = archive_planning_artifacts(planning_dir, extra_names=["custom.json"])
    assert archive is not None
    assert (archive / "custom.json").exists()


def test_repeat_archive_with_same_timestamp_appends_suffix(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    _seed(planning_dir, {".execution_result.json": "{}"})
    archive_planning_artifacts(planning_dir, timestamp="20260504T120000Z")

    # Recreate the same artifact and re-archive into the same timestamped dir.
    (planning_dir / ".execution_result.json").write_text("{}", encoding="utf-8")
    archive_planning_artifacts(planning_dir, timestamp="20260504T120000Z")

    archive = planning_dir / HISTORY_DIRNAME / "20260504T120000Z"
    files = sorted(p.name for p in archive.iterdir())
    assert ".execution_result.json" in files
    assert ".execution_result.1.json" in files


def test_history_subtree_is_not_archived_recursively(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    _seed(planning_dir, {".execution_result.json": "{}"})
    archive_planning_artifacts(planning_dir, timestamp="20260504T120000Z")
    # Second rerun without any new artifacts at top: no-op, history is preserved.
    result = archive_planning_artifacts(planning_dir, timestamp="20260504T130000Z")
    assert result is None
    assert (planning_dir / HISTORY_DIRNAME / "20260504T120000Z").exists()

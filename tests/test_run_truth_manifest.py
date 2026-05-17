"""RunTruth artifact path manifest.

Verifies build_run_truth attaches an `artifact_paths` index that lists the
truth files actually present under the planning directory, so consumers
(delivery report / lane observation / instincts) can read it instead of
re-globbing.
"""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.cli.evidence.artifact_truth import build_run_truth


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_manifest_lists_present_artifacts(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    _write_json(planning_dir / ".execution_result.json", {"status": "ok"})
    _write_json(planning_dir / ".verify_report.json", {"status": "PASS"})
    (planning_dir / "REVIEW.md").write_text("# review", encoding="utf-8")

    truth = build_run_truth(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="feat",
        payload={},
        run_result={},
        rounds=[],
    )

    manifest = truth["artifact_paths"]
    assert ".execution_result.json" in manifest
    assert ".verify_report.json" in manifest
    assert "REVIEW.md" in manifest


def test_manifest_omits_missing_artifacts(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)

    truth = build_run_truth(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="feat",
        payload={},
        run_result={},
        rounds=[],
    )

    manifest = truth["artifact_paths"]
    assert ".task_run_result.json" not in manifest
    assert ".lane_observation.json" not in manifest


def test_manifest_skips_directories_with_known_names(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True)
    # An adversarial directory that shares a name with a tracked artifact name
    # must not be reported as a present file.
    (planning_dir / ".execution_result.json").mkdir()

    truth = build_run_truth(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="feat",
        payload={},
        run_result={},
        rounds=[],
    )
    assert ".execution_result.json" not in truth["artifact_paths"]

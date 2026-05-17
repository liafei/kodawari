from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_generate_evidence_bundle_writes_five_packages(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning"
    output_dir = project_root / "evidence"
    project_root.mkdir(parents=True, exist_ok=True)

    _write_json(
        planning_dir / "lane_stability_always-on.json",
        {"lane": "always-on", "status": "PASS", "passed_runs": 2, "failed_runs": 0, "skipped_runs": 0},
    )
    _write_json(
        planning_dir / "lane_stability_integration.json",
        {"lane": "integration", "status": "PASS", "passed_runs": 2, "failed_runs": 0, "skipped_runs": 0},
    )
    _write_json(
        planning_dir / "ci_repo_health_src" / ".gate_result.json",
        {"total_status": "PASS", "total_violations": 0, "scanned_files": 42},
    )
    _write_json(planning_dir / "demo" / ".autopilot_state.json", {"status": "BLOCKED", "blocking_reason": "fixture"})

    script_path = Path(__file__).resolve().parents[1] / "scripts" / "generate_evidence_bundle.py"
    run = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--project-root",
            str(project_root),
            "--planning-dir",
            str(planning_dir),
            "--output-dir",
            str(output_dir),
            "--source-commit",
            "abc123",
            "--timestamp-utc",
            "2026-03-29T00:00:00Z",
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr

    expected_files = [
        "happy-path.md",
        "blocked-recovery.md",
        "backend-capabilities.md",
        "lane-stability.md",
        "gate-enforcement.md",
    ]
    for name in expected_files:
        path = output_dir / name
        assert path.exists(), name
        text = path.read_text(encoding="utf-8")
        assert "- command: `" in text
        assert "- input_artifacts:" in text
        assert "- verdict: `" in text
        assert "- timestamp: `2026-03-29T00:00:00Z`" in text
        assert "- source_commit: `abc123`" in text

    manifest = json.loads((output_dir / "evidence_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "evidence.bundle.v1"
    assert manifest["evidence_total"] == 5
    assert len(manifest["items"]) == 5
    backend_row = next(item for item in manifest["items"] if item["file"] == "backend-capabilities.md")
    assert backend_row["command"] == "python -m pytest -q tests/test_execution_backend_capability_honesty.py"
    assert "execution_backend.py" in " ".join(backend_row["input_artifacts"])

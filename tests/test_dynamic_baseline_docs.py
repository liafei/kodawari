from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_render_dynamic_baseline_updates_only_controlled_block(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir(parents=True, exist_ok=True)
    planning = project_root / "planning"

    _write_json(planning / "lane_stability_always-on.json", {"status": "PASS"})
    _write_json(planning / "lane_stability_integration.json", {"status": "SKIP"})
    _write_json(planning / "ci_repo_health_src" / ".gate_result.json", {"total_status": "PASS", "total_violations": 0, "scanned_files": 10})
    _write_json(project_root / "pytest_summary.json", {"collected": 123, "passed": 120, "skipped": 3, "failed": 0})

    src_file = project_root / "src" / "module.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    src_file.write_text("print('ok')\n", encoding="utf-8")

    doc_path = tmp_path / "workflowsdk-rebuild.md"
    doc_path.write_text(
        "\n".join(
            [
                "header",
                "<!-- BEGIN WS-223:DYNAMIC_BASELINE -->",
                "old-line",
                "<!-- END WS-223:DYNAMIC_BASELINE -->",
                "footer",
                "",
            ]
        ),
        encoding="utf-8",
    )

    output_json = project_root / "planning" / "dynamic_baseline_snapshot.json"
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "render_dynamic_baseline_docs.py"
    run = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--project-root",
            str(project_root),
            "--doc",
            str(doc_path),
            "--pytest-summary-json",
            str(project_root / "pytest_summary.json"),
            "--output-json",
            str(output_json),
            "--source-commit",
            "deadbeef",
            "--timestamp-utc",
            "2026-03-29T00:00:00Z",
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr

    updated = doc_path.read_text(encoding="utf-8")
    assert "header" in updated
    assert "footer" in updated
    assert "old-line" not in updated
    assert "- source_commit: `deadbeef`" in updated
    assert "- pytest: `collected=123, passed=120, skipped=3, failed=0`" in updated
    assert "- strict_gate: `status=PASS, violations=0, scanned_files=10`" in updated
    assert "- lane_status: `always-on=PASS, integration=SKIP`" in updated
    assert "- src_redline: `limit=1000, scanned=1, violations=0`" in updated

    snapshot = json.loads(output_json.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == "dynamic.baseline.v1"
    assert snapshot["source_commit"] == "deadbeef"
    assert snapshot["pytest"]["collected"] == 123

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is required for lane script tests")


def _lane_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("WORKFLOW_REVIEWER_API_KEY", None)
    env.pop("WORKFLOW_REVIEWER_BASE_URL", None)
    env.pop("WORKFLOW_OPUS_API_KEY", None)
    env.pop("WORKFLOW_OPUS_GATEWAY", None)
    return env


def _run_powershell(script_path: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POWERSHELL, "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )


def test_invoke_test_lane_writes_skip_result_when_integration_env_missing(tmp_path: Path) -> None:
    result_path = tmp_path / "lane_result.json"
    run = _run_powershell(
        REPO_ROOT / "scripts" / "invoke_test_lane.ps1",
        "-Lane",
        "integration",
        "-ResultPath",
        str(result_path),
        env=_lane_env(),
    )

    assert run.returncode == 0, run.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "lane.run_result.v1"
    assert payload["lane"] == "integration"
    assert payload["status"] == "SKIP"
    assert payload["exit_code"] == 0
    assert payload["fail_if_skipped"] is False
    assert payload["missing_env"] == ["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"]
    assert "tests/test_generic_runtime_real_review.py" in payload["pytest_targets"]


def test_invoke_test_lane_writes_skip_result_when_real_review_success_env_missing(tmp_path: Path) -> None:
    result_path = tmp_path / "lane_result.json"
    run = _run_powershell(
        REPO_ROOT / "scripts" / "invoke_test_lane.ps1",
        "-Lane",
        "real-review-success",
        "-ResultPath",
        str(result_path),
        env=_lane_env(),
    )

    assert run.returncode == 0, run.stderr
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "lane.run_result.v1"
    assert payload["lane"] == "real-review-success"
    assert payload["status"] == "SKIP"
    assert payload["missing_env"] == ["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"]
    assert payload["pytest_targets"] == ["tests/test_generic_runtime_real_review_success.py"]


def test_run_lane_stability_records_skip_summary_when_integration_env_missing(tmp_path: Path) -> None:
    summary_path = tmp_path / "lane_stability_integration.json"
    triage_json_path = tmp_path / "lane_triage_integration.json"
    triage_markdown_path = tmp_path / "lane_triage_integration.md"
    run = _run_powershell(
        REPO_ROOT / "scripts" / "run_lane_stability.ps1",
        "-Lane",
        "integration",
        "-Repeat",
        "1",
        "-SummaryPath",
        str(summary_path),
        env=_lane_env(),
    )

    assert run.returncode == 0, run.stderr
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "lane.stability.v1"
    assert payload["summary_version"] == "lane.stability.v1"
    assert payload["lane"] == "integration"
    assert payload["status"] == "SKIP"
    assert payload["failed_runs"] == 0
    assert payload["skipped_runs"] == 1
    assert payload["passed_runs"] == 0
    assert payload["triage_artifacts"]["json"] == str(triage_json_path)
    assert payload["triage_artifacts"]["markdown"] == str(triage_markdown_path)
    assert payload["runs"][0]["status"] == "SKIP"
    assert payload["runs"][0]["missing_env"] == ["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"]
    triage_payload = json.loads(triage_json_path.read_text(encoding="utf-8"))
    assert triage_payload["schema_version"] == "lane.triage.v1"
    assert triage_payload["triage_version"] == "lane.triage.v1"
    assert triage_payload["classification_id"] == "lane.integration_env_missing"
    assert triage_payload["root_cause_bucket"] == "env_missing"
    assert triage_payload["alert_level"] == "warning"
    assert triage_payload["missing_env"] == ["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"]
    triage_md = triage_markdown_path.read_text(encoding="utf-8")
    assert "# Lane Triage Report" in triage_md
    assert "lane.integration_env_missing" in triage_md
    assert "root_cause_bucket: env_missing" in triage_md


def test_run_lane_stability_fail_if_skipped_records_fail_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "lane_stability_integration.json"
    triage_json_path = tmp_path / "lane_triage_integration.json"
    triage_markdown_path = tmp_path / "lane_triage_integration.md"
    run = _run_powershell(
        REPO_ROOT / "scripts" / "run_lane_stability.ps1",
        "-Lane",
        "integration",
        "-Repeat",
        "1",
        "-SummaryPath",
        str(summary_path),
        "-FailIfSkipped",
        env=_lane_env(),
    )

    assert run.returncode == 1
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "lane.stability.v1"
    assert payload["status"] == "FAIL"
    assert payload["fail_if_skipped"] is True
    assert payload["failed_runs"] == 1
    assert payload["skipped_runs"] == 0
    assert payload["runs"][0]["status"] == "FAIL"
    assert "required integration environment is incomplete" in payload["runs"][0]["message"]
    triage_payload = json.loads(triage_json_path.read_text(encoding="utf-8"))
    assert triage_payload["schema_version"] == "lane.triage.v1"
    assert triage_payload["classification_id"] == "lane.integration_env_missing_fail_closed"
    assert triage_payload["root_cause_bucket"] == "env_missing"
    assert triage_payload["alert_level"] == "error"
    triage_md = triage_markdown_path.read_text(encoding="utf-8")
    assert "lane.integration_env_missing_fail_closed" in triage_md
    assert "root_cause_bucket: env_missing" in triage_md

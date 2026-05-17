from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kodawari.cli.main import build_parser


def _run_cli(parser: Any, capsys: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    args = parser.parse_args(argv)
    rc = int(args.handler(args))
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_cli_help_includes_lane_trend_report_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "lane-trend-report" in help_text


def test_cli_lane_trend_report_writes_json_and_markdown(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    now = datetime.now(timezone.utc)
    artifact_dir = tmp_path / "planning" / "lane-history-a"
    summary_path = artifact_dir / "lane_stability_always-on.json"
    triage_path = artifact_dir / "lane_triage_always-on.json"
    json_output = tmp_path / "planning" / "AUTOMATION_LANE_TREND_REPORT.json"
    markdown_output = tmp_path / "planning" / "AUTOMATION_LANE_TREND_REPORT.md"

    _write_json(
        summary_path,
        {
            "schema_version": "lane.stability.v1",
            "summary_version": "lane.stability.v1",
            "lane": "always-on",
            "status": "PASS",
            "repeat_completed": 3,
            "passed_runs": 3,
            "failed_runs": 0,
            "skipped_runs": 0,
            "finished_at_utc": (now - timedelta(hours=1)).isoformat(),
            "runs": [
                {"status": "PASS", "message": "ok"},
                {"status": "PASS", "message": "ok"},
                {"status": "PASS", "message": "ok"},
            ],
            "triage_artifacts": {"json": str(triage_path)},
        },
    )
    _write_json(
        triage_path,
        {
            "schema_version": "lane.triage.v1",
            "triage_version": "lane.triage.v1",
            "lane": "always-on",
            "status": "PASS",
            "alert_level": "info",
            "classification_id": "lane.stable_pass",
            "classification_label": "Stable pass",
            "headline": "Lane repeated cleanly",
            "failure_signatures": [],
            "missing_env": [],
            "generated_at_utc": now.isoformat(),
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-trend-report",
            "--project-root",
            str(tmp_path),
            "--artifacts-dir",
            str(tmp_path / "planning"),
            "--max-history-days",
            "7",
            "--json-output",
            str(json_output),
            "--output",
            str(markdown_output),
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["schema_version"] == "lane.trend.report.v1"
    assert payload["summary"]["records_total"] == 1
    assert payload["summary"]["lanes_observed"] == ["always-on"]
    assert json_output.exists()
    assert markdown_output.exists()
    report_payload = json.loads(json_output.read_text(encoding="utf-8"))
    assert report_payload["schema_version"] == "lane.trend.report.v1"
    assert report_payload["status"] == "PASS"
    assert report_payload["summary"]["root_cause_bucket_counts"]["stable_pass"] == 1
    assert report_payload["records"][0]["root_cause_bucket"] == "stable_pass"
    assert report_payload["records"][0]["root_cause_label"] == "Stable pass"
    markdown = markdown_output.read_text(encoding="utf-8")
    assert "# AUTOMATION_LANE_TREND_REPORT" in markdown
    assert "## Lane Table" in markdown
    assert "stable_pass" in markdown


def test_cli_lane_trend_report_fail_on_block_returns_non_zero(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    now = datetime.now(timezone.utc)
    artifact_dir = tmp_path / "planning" / "lane-history-blocked"
    summary_path = artifact_dir / "lane_stability_integration.json"
    triage_path = artifact_dir / "lane_triage_integration.json"

    _write_json(
        summary_path,
        {
            "schema_version": "lane.stability.v1",
            "summary_version": "lane.stability.v1",
            "lane": "integration",
            "status": "FAIL",
            "repeat_completed": 3,
            "passed_runs": 0,
            "failed_runs": 3,
            "skipped_runs": 0,
            "finished_at_utc": (now - timedelta(minutes=30)).isoformat(),
            "runs": [
                {"status": "FAIL", "message": "gateway timeout"},
                {"status": "FAIL", "message": "gateway timeout"},
                {"status": "FAIL", "message": "gateway timeout"},
            ],
            "triage_artifacts": {"json": str(triage_path)},
        },
    )
    _write_json(
        triage_path,
        {
            "schema_version": "lane.triage.v1",
            "triage_version": "lane.triage.v1",
            "lane": "integration",
            "status": "FAIL",
            "alert_level": "error",
            "classification_id": "lane.consistent_failure",
            "classification_label": "Consistent lane failure",
            "headline": "Lane failed consistently",
            "failure_signatures": [{"signature": "gateway timeout", "count": 3}],
            "missing_env": [],
            "generated_at_utc": now.isoformat(),
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-trend-report",
            "--project-root",
            str(tmp_path),
            "--artifacts-dir",
            str(tmp_path / "planning"),
            "--lane",
            "integration",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["summary"]["blocked_lanes"] == ["integration"]
    assert payload["summary"]["records_total"] == 1
    assert payload["summary"]["root_cause_bucket_counts"]["timeout"] == 1


def test_cli_lane_trend_report_returns_error_when_no_inputs_exist(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-trend-report",
            "--project-root",
            str(tmp_path),
        ],
    )

    assert rc == 2
    assert payload["error_code"] == "lane_trend_report_failed"
    assert "artifacts-dir" in payload["remediation"][0]

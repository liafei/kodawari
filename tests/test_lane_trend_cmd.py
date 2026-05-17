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


def test_cli_help_includes_lane_trend_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "lane-trend" in help_text


def _triage_payload(
    *,
    lane: str,
    classification_id: str,
    generated_at: datetime,
    status: str = "PASS",
    alert_level: str = "info",
    passed_runs: int = 3,
    failed_runs: int = 0,
    skipped_runs: int = 0,
    missing_env: list[str] | None = None,
    failure_signatures: list[dict[str, Any]] | None = None,
    operator_actions: list[str] | None = None,
    headline: str = "",
    root_cause_bucket: str = "",
) -> dict[str, Any]:
    payload = {
        "triage_version": "lane.triage.v1",
        "lane": lane,
        "status": status,
        "alert_level": alert_level,
        "classification_id": classification_id,
        "headline": headline or classification_id,
        "summary_path": f"E:/history/{lane}.json",
        "repeat_requested": 3,
        "repeat_completed": 3,
        "passed_runs": passed_runs,
        "failed_runs": failed_runs,
        "skipped_runs": skipped_runs,
        "fail_if_skipped": lane == "integration",
        "missing_env": list(missing_env or []),
        "failure_signatures": list(failure_signatures or []),
        "operator_actions": list(operator_actions or ["review triage"]),
        "ci_actions": ["keep fixed recipe"],
        "recommended_commands": ["powershell -ExecutionPolicy Bypass -File .\\scripts\\run_lane_stability.ps1"],
        "generated_at_utc": generated_at.isoformat().replace("+00:00", "Z"),
    }
    if root_cause_bucket:
        payload["root_cause_bucket"] = root_cause_bucket
    return payload


def test_cli_lane_trend_reports_stable_weekly_standing_proof(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    artifacts_root = tmp_path / "artifacts"
    now = datetime.now(timezone.utc)

    _write_json(
        artifacts_root / "always-on-a" / "lane_triage_always-on.json",
        _triage_payload(lane="always-on", classification_id="lane.stable_pass", generated_at=now - timedelta(days=3)),
    )
    _write_json(
        artifacts_root / "always-on-b" / "lane_triage_always-on.json",
        _triage_payload(lane="always-on", classification_id="lane.stable_pass", generated_at=now - timedelta(days=2)),
    )
    _write_json(
        artifacts_root / "always-on-c" / "lane_triage_always-on.json",
        _triage_payload(lane="always-on", classification_id="lane.stable_pass", generated_at=now - timedelta(days=1)),
    )
    _write_json(
        artifacts_root / "integration-a" / "lane_triage_integration.json",
        _triage_payload(
            lane="integration",
            classification_id="lane.integration_env_missing",
            generated_at=now - timedelta(days=6),
            status="SKIP",
            alert_level="warning",
            passed_runs=0,
            skipped_runs=3,
            missing_env=["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"],
        ),
    )
    _write_json(
        artifacts_root / "integration-b" / "lane_triage_integration.json",
        _triage_payload(lane="integration", classification_id="lane.stable_pass", generated_at=now - timedelta(days=2)),
    )
    _write_json(
        artifacts_root / "integration-c" / "lane_triage_integration.json",
        _triage_payload(lane="integration", classification_id="lane.stable_pass", generated_at=now - timedelta(days=1)),
    )

    json_output = tmp_path / "planning" / "lane_weekly_trend.json"
    markdown_output = tmp_path / "planning" / "lane_weekly_trend.md"
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-trend",
            "--project-root",
            str(tmp_path),
            "--artifacts-root",
            str(artifacts_root),
            "--required-pass-streak",
            "2",
            "--json-output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["schema_version"] == "lane.trend.v1"
    assert payload["trend_version"] == "lane.trend.v1"
    assert payload["overview"]["lanes_stable"] == 2
    assert payload["overview"]["lanes_incident_recommended"] == 0
    assert payload["reports_considered"] == 6
    assert payload["incident_candidates"] == []
    assert payload["recommended_incidents"] == []
    assert payload["recommended_incident_candidates_total"] == 0
    assert payload["root_cause_bucket_counts"]["env_missing"] == 1
    assert payload["top_root_causes"][0]["bucket"] == "env_missing"
    assert payload["top_root_causes"][0]["label"] == "Environment missing"
    lane_map = {item["lane"]: item for item in payload["lanes"]}
    assert lane_map["always-on"]["standing_proof_state"] == "stable"
    assert lane_map["always-on"]["current_pass_streak"] == 3
    assert lane_map["always-on"]["root_cause_bucket_counts"]["stable_pass"] == 3
    assert lane_map["always-on"]["latest_root_cause_label"] == "Stable pass"
    assert lane_map["integration"]["standing_proof_state"] == "stable"
    assert lane_map["integration"]["current_pass_streak"] == 2
    assert lane_map["integration"]["classification_counts"]["lane.integration_env_missing"] == 1
    assert lane_map["integration"]["root_cause_bucket_counts"]["env_missing"] == 1
    assert lane_map["integration"]["latest_root_cause_bucket"] == "stable_pass"
    assert lane_map["integration"]["latest_root_cause_label"] == "Stable pass"
    assert json_output.exists()
    assert markdown_output.exists()
    markdown = markdown_output.read_text(encoding="utf-8")
    assert "# LANE_WEEKLY_TREND" in markdown
    assert "## Top Root Causes" in markdown
    assert "env_missing (Environment missing): 1" in markdown
    assert "Lane: integration" in markdown
    assert "latest_root_cause_bucket" in markdown


def test_cli_lane_trend_blocks_when_latest_lane_state_is_not_stable(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    artifacts_root = tmp_path / "artifacts"
    now = datetime.now(timezone.utc)

    _write_json(
        artifacts_root / "integration-current" / "lane_triage_integration.json",
        _triage_payload(
            lane="integration",
            classification_id="lane.integration_env_missing_fail_closed",
            generated_at=now - timedelta(hours=6),
            status="FAIL",
            alert_level="error",
            passed_runs=0,
            failed_runs=3,
            missing_env=["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"],
            operator_actions=["restore secrets", "rerun integration lane"],
        ),
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-trend",
            "--project-root",
            str(tmp_path),
            "--artifacts-root",
            str(artifacts_root),
            "--lane",
            "integration",
            "--required-pass-streak",
            "3",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["overview"]["lanes_non_stable"] == 1
    assert payload["overview"]["lanes_incident_recommended"] == 1
    assert payload["overview"]["incident_recommended_lanes"] == ["integration"]
    assert payload["lanes"][0]["standing_proof_state"] == "env_blocked"
    assert payload["lanes"][0]["latest_root_cause_bucket"] == "env_missing"
    assert payload["lanes"][0]["missing_env_counts"]["WORKFLOW_REVIEWER_API_KEY"] == 1
    assert payload["recommended_incident_candidates_total"] == 1
    assert len(payload["recommended_incidents"]) == 1
    assert len(payload["incident_candidates"]) == 1
    candidate = payload["incident_candidates"][0]
    assert candidate["lane"] == "integration"
    assert candidate["recommended"] is True
    assert candidate["severity"] == "high"
    assert candidate["incident_id"] == "lane-integration-env-missing"
    assert candidate["planning_scope_hint"]
    assert "kodawari incident-ingest" in candidate["suggested_command"]
    assert "--planning-dir <planning-dir>" in candidate["suggested_command"]
    assert payload["remediation"]


def test_cli_lane_trend_warns_on_invalid_artifacts_and_ignores_duplicates(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    artifacts_root = tmp_path / "artifacts"
    now = datetime.now(timezone.utc)
    payload = _triage_payload(
        lane="always-on",
        classification_id="lane.stable_pass",
        generated_at=now - timedelta(days=1),
    )
    _write_json(artifacts_root / "a" / "lane_triage_always-on.json", payload)
    _write_json(artifacts_root / "b" / "lane_triage_always-on.json", payload)
    broken = artifacts_root / "broken" / "lane_triage_always-on.json"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("{broken", encoding="utf-8")

    rc, result = _run_cli(
        parser,
        capsys,
        [
            "lane-trend",
            "--project-root",
            str(tmp_path),
            "--artifacts-root",
            str(artifacts_root),
            "--required-pass-streak",
            "1",
        ],
    )

    assert rc == 0
    assert result["reports_considered"] == 1
    assert result["warnings"]
    assert "broken" in result["warnings"][0]
    assert result["lanes"][0]["standing_proof_state"] == "stable"
    assert result["lanes"][0]["root_cause_bucket_counts"]["stable_pass"] == 1


def test_cli_lane_trend_infers_gate_blocked_bucket_from_failure_signature(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    artifacts_root = tmp_path / "artifacts"
    now = datetime.now(timezone.utc)

    _write_json(
        artifacts_root / "always-on-blocked" / "lane_triage_always-on.json",
        _triage_payload(
            lane="always-on",
            classification_id="lane.consistent_failure",
            generated_at=now - timedelta(hours=4),
            status="FAIL",
            alert_level="error",
            passed_runs=0,
            failed_runs=3,
            headline="quality gate blocked after workflow chain completion",
            failure_signatures=[
                {
                    "signature": "Advisory gate blocked after workflow chain completion.",
                    "count": 3,
                }
            ],
        ),
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-trend",
            "--project-root",
            str(tmp_path),
            "--artifacts-root",
            str(artifacts_root),
            "--lane",
            "always-on",
            "--required-pass-streak",
            "2",
        ],
    )

    assert rc == 0
    assert payload["status"] == "BLOCKED"
    assert payload["root_cause_bucket_counts"]["gate_blocked"] == 1
    assert payload["recommended_incident_candidates_total"] == 1
    assert payload["recommended_incidents"][0]["lane"] == "always-on"
    lane_summary = payload["lanes"][0]
    assert lane_summary["latest_root_cause_bucket"] == "gate_blocked"
    assert lane_summary["latest_root_cause_label"] == "Gate blocked"
    assert lane_summary["top_root_causes"][0]["bucket"] == "gate_blocked"
    assert lane_summary["top_root_causes"][0]["label"] == "Gate blocked"
    candidate = lane_summary["incident_candidate"]
    assert candidate["recommended"] is True
    assert candidate["severity"] == "high"
    assert candidate["incident_id"] == "lane-always-on-gate-blocked"
    assert "kodawari incident-ingest" in candidate["suggested_command"]
    markdown = (tmp_path / "planning" / "lane_weekly_trend.md").read_text(encoding="utf-8")
    assert "## Incident Candidates" in markdown
    assert "## Recommended Incidents" in markdown
    assert "### Incident Candidate" in markdown


def test_cli_lane_trend_prefers_persisted_root_cause_bucket(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    artifacts_root = tmp_path / "artifacts"
    now = datetime.now(timezone.utc)
    _write_json(
        artifacts_root / "integration-latest" / "lane_triage_integration.json",
        _triage_payload(
            lane="integration",
            classification_id="lane.consistent_failure",
            generated_at=now - timedelta(hours=1),
            status="FAIL",
            alert_level="error",
            passed_runs=0,
            failed_runs=3,
            failure_signatures=[{"signature": "unknown transient failure", "count": 3}],
            root_cause_bucket="external_gateway",
        ),
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-trend",
            "--project-root",
            str(tmp_path),
            "--artifacts-root",
            str(artifacts_root),
            "--lane",
            "integration",
            "--required-pass-streak",
            "2",
        ],
    )
    assert rc == 0
    assert payload["root_cause_bucket_counts"]["external_gateway"] == 1
    lane_summary = payload["lanes"][0]
    assert lane_summary["latest_root_cause_bucket"] == "external_gateway"
    assert lane_summary["latest_root_cause_label"] == "External gateway or network"

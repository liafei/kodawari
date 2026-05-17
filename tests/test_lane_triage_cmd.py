from __future__ import annotations

import json
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


def test_cli_help_includes_lane_triage_command() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "lane-triage" in help_text


def test_cli_lane_triage_writes_json_and_markdown_artifacts(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    summary_path = tmp_path / "planning" / "lane_stability_always-on.json"
    json_output = tmp_path / "planning" / "lane_triage_always-on.json"
    markdown_output = tmp_path / "planning" / "lane_triage_always-on.md"
    _write_json(
        summary_path,
        {
            "summary_version": "lane.stability.v1",
            "lane": "always-on",
            "repeat_requested": 3,
            "repeat_completed": 3,
            "passed_runs": 3,
            "failed_runs": 0,
            "skipped_runs": 0,
            "runs": [
                {"status": "PASS", "message": "ok"},
                {"status": "PASS", "message": "ok"},
                {"status": "PASS", "message": "ok"},
            ],
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-triage",
            "--project-root",
            str(tmp_path),
            "--lane",
            "always-on",
            "--summary",
            str(summary_path),
            "--output",
            str(json_output),
            "--markdown-output",
            str(markdown_output),
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["classification"] == "lane_pass"
    assert payload["classification_id"] == "lane.stable_pass"
    assert payload["root_cause_bucket"] == "stable_pass"
    assert payload["standing_proof_status"] == "pass"
    assert json_output.exists()
    assert markdown_output.exists()
    written = json.loads(json_output.read_text(encoding="utf-8"))
    assert written["status"] == "PASS"
    assert written["classification"] == "lane_pass"
    assert written["schema_version"] == "lane.triage.v1"
    assert written["triage_version"] == "lane.triage.v1"
    assert written["root_cause_bucket"] == "stable_pass"
    markdown = markdown_output.read_text(encoding="utf-8")
    assert "# LANE_TRIAGE" in markdown
    assert "## Summary" in markdown


def test_cli_lane_triage_fail_on_block_returns_non_zero(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    summary_path = tmp_path / "planning" / "lane_stability_integration.json"
    _write_json(
        summary_path,
        {
            "summary_version": "lane.stability.v1",
            "lane": "integration",
            "repeat_requested": 1,
            "repeat_completed": 1,
            "passed_runs": 0,
            "failed_runs": 0,
            "skipped_runs": 1,
            "runs": [
                {
                    "status": "SKIP",
                    "message": "required integration environment is incomplete",
                    "missing_env": ["WORKFLOW_REVIEWER_API_KEY", "WORKFLOW_REVIEWER_BASE_URL"],
                }
            ],
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "lane-triage",
            "--project-root",
            str(tmp_path),
            "--lane",
            "integration",
            "--summary",
            str(summary_path),
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["classification"] == "integration_env_missing"
    assert payload["classification_id"] == "lane.integration_env_missing"
    assert payload["root_cause_bucket"] == "env_missing"
    assert payload["severity"] == "high"
    assert payload["error_code"] == "integration_env_missing"

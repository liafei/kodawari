import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from kodawari.cli.main import build_parser
from kodawari.cli.stability_report_cmd import _load_run_summary
from kodawari.cli.telemetry_field_eval_cmd import _append_jsonl, _load_jsonl_dict_rows


def _run_cli(parser: Any, capsys: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    args = parser.parse_args(argv)
    rc = int(args.handler(args))
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _prepare_runtime_artifacts(planning_dir: Path, *, feature: str, blocked: bool = False) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "schema_version": "autopilot.state.v2",
            "revision": 1,
            "feature": feature,
            "cycle": 2,
            "tokens_used": 1234,
            "changed_files": ["src/service.py"],
            "final_status": "BLOCKED" if blocked else "PASS",
            "stop_reason": "GATE_BLOCKED" if blocked else "PASS",
            "error_events": [{"category": "gate", "message": "blocked"}] if blocked else [],
            "verify_setup_recovery_attempted": 1,
            "verify_setup_recovery_succeeded": 1 if not blocked else 0,
        },
    )
    _write_json(
        planning_dir / ".workflow_chain.json",
        {
            "upstream": {"verify": {"status": "FAIL" if blocked else "PASS"}},
            "final_quality_review": {"status": "BLOCKED" if blocked else "PASS"},
            "final_outcome": {"status": "BLOCKED" if blocked else "PASS"},
        },
    )
    _write_json(
        planning_dir / ".gate_result.json",
        {"total_status": "BLOCKED" if blocked else "PASS", "profile": {"name": "advisory"}},
    )
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": ["fix me"] if blocked else []})
    (planning_dir / ".autopilot_rounds.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"stage": "PLAN", "stage_status": "pass"}),
                json.dumps({"stage": "OPUS_REVIEW", "stage_status": "changes_requested"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_cli_help_includes_d_group_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "telemetry" in help_text
    assert "field-report" in help_text
    assert "field-report-update" in help_text
    assert "eval-report" in help_text


def test_cli_telemetry_writes_snapshot_and_history(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "telemetry-pass-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "telemetry",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["provenance"]["command"] == "telemetry"
    assert (planning_dir / ".telemetry_snapshot.json").exists()
    assert (planning_dir / ".telemetry_events.jsonl").exists()
    snapshot = json.loads((planning_dir / ".telemetry_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == "telemetry.snapshot.v1"
    assert snapshot["metrics"]["tokens_used"] == 1234
    assert snapshot["metrics"]["review_rounds_used"] == 1
    assert snapshot["signals"]["reasoning_tier"] in {"economy", "standard", "deep_reasoning"}
    assert isinstance(snapshot["signals"]["effort_score"], int)
    assert isinstance(snapshot["signals"]["effort_reasons"], list)
    assert payload["reasoning_tier"] == snapshot["signals"]["reasoning_tier"]


def test_cli_telemetry_schema_error_returns_field_details(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "telemetry-schema-error"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)

    state_path = planning_dir / ".autopilot_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["changed_files"] = {"invalid": "object"}
    state_path.write_text(json.dumps(state), encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        ["telemetry", "--project-root", str(tmp_path), "--feature", feature],
    )

    assert rc == 2
    assert payload["error_code"] == "schema_validation_failed"
    assert payload["schema"] == "telemetry_snapshot"
    assert any(item["field"] == "changed_files" for item in payload["field_errors"])


def test_review_rounds_used_consistent_between_telemetry_and_stability(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-rounds-consistency"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)

    telemetry_rc, telemetry_payload = _run_cli(
        parser,
        capsys,
        [
            "telemetry",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
        ],
    )
    assert telemetry_rc == 0
    telemetry_rounds = telemetry_payload["metrics"]["review_rounds_used"]

    stability_rc, _stability_payload = _run_cli(
        parser,
        capsys,
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            feature,
        ],
    )
    assert stability_rc == 0

    run_summary = _load_run_summary(planning_dir)
    assert run_summary["review_rounds_used"] == telemetry_rounds


def test_cli_field_report_writes_json_and_markdown_with_sanitization(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "field-report-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=True)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    long_text = "x" * 700
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "field-report",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--severity",
            "critical",
            "--title",
            long_text,
            "--summary",
            "Observed blocked delivery in production-like run",
            "--component",
            "service/aggregation",
            "--impact",
            "feeds blocked",
            "--owner",
            "ops-team",
            "--evidence",
            "C:/Users/liafei/private/secrets/incident.log",
            "--tag",
            "delivery",
            "--tag",
            "gate",
        ],
    )

    assert rc == 0
    assert payload["status"] == "RECORDED"
    assert payload["severity"] == "critical"
    assert payload["provenance"]["command"] == "field-report"

    report = json.loads((planning_dir / ".field_report.json").read_text(encoding="utf-8"))
    assert len(report["title"]) == 512
    assert report["evidence_files"] == [".../secrets/incident.log"]
    assert (planning_dir / "FIELD_REPORT.md").exists()
    assert (planning_dir / ".field_reports.jsonl").exists()


def test_cli_field_report_dedup_and_update_state_machine(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "field-report-update-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=True)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    report_id = "FR-STATIC-001"
    create_args = [
        "field-report",
        "--project-root",
        str(tmp_path),
        "--feature",
        feature,
        "--report-id",
        report_id,
        "--severity",
        "high",
        "--title",
        "Create report",
        "--summary",
        "summary",
    ]
    rc, payload = _run_cli(parser, capsys, create_args)
    assert rc == 0
    assert payload["report_id"] == report_id

    dup_rc, dup_payload = _run_cli(parser, capsys, create_args)
    assert dup_rc == 2
    assert dup_payload["error_code"] == "duplicate_report_id"

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "field-report-update",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--status",
            "in_progress",
        ],
    )
    assert rc == 0
    assert payload["status"] == "UPDATED"


def test_cli_incident_ingest_routes_into_field_report_state_machine(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "incident-ingest-demo"
    report_id = "INC-001"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=True)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "incident-ingest",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--incident-id",
            "INC-001",
            "--source",
            "production",
            "--severity",
            "critical",
            "--title",
            "Prod incident",
            "--summary",
            "Customer-facing regression",
        ],
    )

    assert rc == 0
    assert payload["status"] == "RECORDED"
    assert payload["incident_source"] == "production"
    assert payload["field_report_result"]["report_id"] == report_id

    rc, _ = _run_cli(
        parser,
        capsys,
        [
            "field-report-update",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--status",
            "resolved",
        ],
    )
    assert rc == 0

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "field-report-update",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--status",
            "open",
        ],
    )
    assert rc == 2
    assert payload["error_code"] == "invalid_status_transition"

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "field-report-update",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--status",
            "open",
            "--allow-reopen",
        ],
    )
    assert rc == 0
    assert payload["to_status"] == "open"

    latest_report = json.loads((planning_dir / ".field_report.json").read_text(encoding="utf-8"))
    assert latest_report["status"] == "open"
    history_rows = [line for line in (planning_dir / ".field_reports.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(history_rows) == 3


def test_cli_field_report_update_recovers_when_history_line_is_corrupted(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "field-report-corrupt-history-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=True)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    report_id = "FR-CORRUPT-001"
    rc, _payload = _run_cli(
        parser,
        capsys,
        [
            "field-report",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--severity",
            "high",
            "--title",
            "Create report",
            "--summary",
            "summary",
        ],
    )
    assert rc == 0

    # Simulate historical JSONL corruption while keeping latest snapshot intact.
    (planning_dir / ".field_reports.jsonl").write_text('{"broken":\n', encoding="utf-8")

    update_rc, update_payload = _run_cli(
        parser,
        capsys,
        [
            "field-report-update",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--status",
            "in_progress",
        ],
    )
    assert update_rc == 0
    assert update_payload["status"] == "UPDATED"
    assert update_payload["to_status"] == "in_progress"
    assert update_payload["resolution_source"] == "latest_snapshot"
    assert update_payload["history_parse_errors"] == 1


def test_cli_eval_report_blocks_on_strict_thresholds(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    pass_feature = "eval-pass-run"
    blocked_feature = "eval-blocked-run"
    pass_dir = tmp_path / "planning" / pass_feature
    blocked_dir = tmp_path / "planning" / blocked_feature
    _prepare_runtime_artifacts(pass_dir, feature=pass_feature, blocked=False)
    _prepare_runtime_artifacts(blocked_dir, feature=blocked_feature, blocked=True)

    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", pass_feature])
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", blocked_feature])
    _run_cli(
        parser,
        capsys,
        [
            "field-report",
            "--project-root",
            str(tmp_path),
            "--feature",
            blocked_feature,
            "--severity",
            "critical",
            "--title",
            "Critical field issue",
            "--summary",
            "Critical issue for eval threshold test",
        ],
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "eval-report",
            "--project-root",
            str(tmp_path),
            "--all-runs",
            "--min-pass-rate",
            "0.9",
            "--max-blocked-rate",
            "0.0",
            "--max-critical-field-reports",
            "0",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["rule_candidates"]
    assert (tmp_path / "AUTOMATION_EVAL_REPORT.json").exists()
    assert (tmp_path / "AUTOMATION_EVAL_REPORT.md").exists()


def test_cli_eval_report_input_lock_replay(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    pass_feature = "eval-lock-pass"
    blocked_feature = "eval-lock-blocked"
    pass_dir = tmp_path / "planning" / pass_feature
    blocked_dir = tmp_path / "planning" / blocked_feature
    _prepare_runtime_artifacts(pass_dir, feature=pass_feature, blocked=False)
    _prepare_runtime_artifacts(blocked_dir, feature=blocked_feature, blocked=True)

    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", pass_feature])
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", blocked_feature])

    lock_path = tmp_path / "locks" / "eval_input_lock.json"
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "eval-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            pass_feature,
            "--min-pass-rate",
            "0.8",
            "--max-blocked-rate",
            "0.5",
            "--max-critical-field-reports",
            "1",
            "--emit-input-lock",
            str(lock_path),
        ],
    )
    assert rc == 0
    assert payload["status"] == "PASS"
    assert lock_path.exists()

    replay_rc, replay_payload = _run_cli(
        parser,
        capsys,
        [
            "eval-report",
            "--project-root",
            str(tmp_path),
            "--all-runs",
            "--input-lock",
            str(lock_path),
            "--fail-on-block",
        ],
    )
    assert replay_rc == 0
    assert replay_payload["status"] == "PASS"
    assert replay_payload["summary"]["runs_total"] == 1
    assert replay_payload["input_lock"] == str(lock_path.resolve())


def test_cli_eval_report_invalid_input_lock_returns_schema_error(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "eval-lock-invalid"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    lock_path = tmp_path / "locks" / "broken_input_lock.json"
    _write_json(
        lock_path,
        {
            "schema_version": "eval.input_lock.v1",
            "run_ids": [feature],
            "planning_dirs": [str(planning_dir.resolve())],
            "thresholds": {
                "min_pass_rate": 0.8,
                "max_blocked_rate": 0.2,
                "max_critical_field_reports": 0,
            },
            "max_history_days": 7,
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "eval-report",
            "--project-root",
            str(tmp_path),
            "--input-lock",
            str(lock_path),
        ],
    )

    assert rc == 2
    assert payload["error_code"] == "schema_validation_failed"
    assert payload["schema"] == "eval_input_lock"
    assert any(item["field"] == "<root>" or item["field"] == "created_at" for item in payload["field_errors"])
    assert payload["remediation"]


def test_cli_eval_report_max_history_days_filters_outdated_snapshots(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "eval-history-filter"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    snapshot_path = planning_dir / ".telemetry_snapshot.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["captured_at"] = "2000-01-01T00:00:00+00:00"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "eval-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            feature,
            "--max-history-days",
            "3",
        ],
    )

    assert rc == 2
    assert "no telemetry snapshots" in payload["error"]


def test_cli_eval_report_skips_rate_thresholds_when_only_pending_runs(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "eval-pending-only"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)

    # Force non-terminal state so eval denominator excludes this run.
    state_path = planning_dir / ".autopilot_state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["final_status"] = None
    state_payload["stop_reason"] = ""
    state_path.write_text(json.dumps(state_payload), encoding="utf-8")

    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "eval-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            feature,
            "--min-pass-rate",
            "0.9",
            "--max-blocked-rate",
            "0.0",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["summary"]["runs_pending"] == 1
    assert payload["summary"]["runs_terminal"] == 0


def test_eval_report_excludes_non_terminal_runs_from_rate_denominator(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    pass_feature = "eval-terminal-pass"
    pending_feature = "eval-pending"
    pass_dir = tmp_path / "planning" / pass_feature
    pending_dir = tmp_path / "planning" / pending_feature
    _prepare_runtime_artifacts(pass_dir, feature=pass_feature, blocked=False)
    _prepare_runtime_artifacts(pending_dir, feature=pending_feature, blocked=False)

    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", pass_feature])
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", pending_feature])

    pending_snapshot_path = pending_dir / ".telemetry_snapshot.json"
    pending_snapshot = json.loads(pending_snapshot_path.read_text(encoding="utf-8"))
    pending_snapshot["status"] = "READY_FOR_GATE"
    pending_snapshot_path.write_text(json.dumps(pending_snapshot), encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "eval-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            pass_feature,
            "--run-id",
            pending_feature,
            "--min-pass-rate",
            "0.9",
            "--max-blocked-rate",
            "0.2",
            "--max-critical-field-reports",
            "0",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["summary"]["runs_total"] == 2
    assert payload["summary"]["runs_terminal"] == 1
    assert payload["summary"]["runs_pending"] == 1
    assert payload["summary"]["pass_rate"] == 1.0
    assert any("pending runs excluded" in item for item in payload["warnings"])
    eval_md = (tmp_path / "AUTOMATION_EVAL_REPORT.md").read_text(encoding="utf-8")
    assert "- runs_terminal: 1" in eval_md
    assert "- runs_pending: 1" in eval_md
    assert "pending runs excluded from pass_rate/blocked_rate denominator" in eval_md


def test_field_report_jsonl_append_is_atomic_under_parallel_writes(tmp_path: Path) -> None:
    history_path = tmp_path / "events" / ".field_reports.jsonl"

    def _append(index: int) -> None:
        _append_jsonl(history_path, {"report_id": f"FR-{index}", "status": "open"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_append, range(40)))

    raw_lines = [line for line in history_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(raw_lines) == 40
    for line in raw_lines:
        assert isinstance(json.loads(line), dict)

    rows = _load_jsonl_dict_rows(history_path)
    assert len(rows) == 40
    report_ids = {str(item.get("report_id") or "") for item in rows}
    assert len(report_ids) == 40


def test_field_report_update_keeps_working_with_partially_corrupted_history(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "field-report-partial-corruption"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    report_id = "FR-PARTIAL-001"
    create_rc, _ = _run_cli(
        parser,
        capsys,
        [
            "field-report",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--severity",
            "medium",
            "--title",
            "partial title",
            "--summary",
            "partial summary",
        ],
    )
    assert create_rc == 0

    history_path = planning_dir / ".field_reports.jsonl"
    valid_line = history_path.read_text(encoding="utf-8").strip()
    history_path.write_text(f"{valid_line}\n{{broken\n{valid_line}\n", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "field-report-update",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--status",
            "in_progress",
        ],
    )
    assert rc == 0
    assert payload["status"] == "UPDATED"
    assert payload["resolution_source"] == "history_jsonl"
    assert payload["history_parse_errors"] == 1
    latest = json.loads((planning_dir / ".field_report.json").read_text(encoding="utf-8"))
    assert latest["status"] == "in_progress"


def test_field_report_update_can_fallback_to_latest_single_report_artifact(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "field-report-fallback"
    planning_dir = tmp_path / "planning" / feature
    _prepare_runtime_artifacts(planning_dir, feature=feature, blocked=False)
    _run_cli(parser, capsys, ["telemetry", "--project-root", str(tmp_path), "--feature", feature])

    report_id = "FR-FALLBACK-001"
    create_rc, _ = _run_cli(
        parser,
        capsys,
        [
            "field-report",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--severity",
            "medium",
            "--title",
            "fallback title",
            "--summary",
            "fallback summary",
        ],
    )
    assert create_rc == 0

    # Corrupt history to simulate a previously damaged jsonl, while keeping the single latest snapshot.
    (planning_dir / ".field_reports.jsonl").write_text("{broken-json", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "field-report-update",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--report-id",
            report_id,
            "--status",
            "in_progress",
        ],
    )

    assert rc == 0
    assert payload["status"] == "UPDATED"
    latest = json.loads((planning_dir / ".field_report.json").read_text(encoding="utf-8"))
    assert latest["report_id"] == report_id
    assert latest["status"] == "in_progress"

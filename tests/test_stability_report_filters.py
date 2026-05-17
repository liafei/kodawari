import json
from pathlib import Path

from kodawari.cli.main import build_parser


def _state_payload(planning_dir: Path, *, updated_at: str, stop_reason: str) -> dict[str, object]:
    return {
        "schema_version": "autopilot.state.v2",
        "revision": 1,
        "feature": planning_dir.name,
        "project_root": str(planning_dir.parents[1]),
        "current_stage": "COMPLETED",
        "cycle": 2,
        "tokens_used": 500,
        "error_history": [],
        "last_error": None,
        "changed_files": [],
        "completed_tasks": ["T001: Implement ranking rules"] if stop_reason == "PASS" else [],
        "task_timings": {},
        "active_task": None,
        "active_pid": None,
        "active_attempt": None,
        "stage_started_at": None,
        "heartbeat_at": None,
        "last_stage_status": stop_reason,
        "warning_noise_events": 0,
        "warning_noise_degraded_events": 0,
        "warning_noise_by_task": {},
        "verify_setup_recovery_attempted": 0,
        "verify_setup_recovery_succeeded": 0,
        "verify_setup_recovery_last_error": None,
        "subtasks": {},
        "active_subtask": None,
        "architecture_decisions": [],
        "started_at": None,
        "updated_at": updated_at,
        "stop_reason": stop_reason,
        "final_status": stop_reason,
    }


def _write_rounds_pass(planning_dir: Path) -> None:
    (planning_dir / ".autopilot_rounds.jsonl").write_text(
        json.dumps(
            {
                "stage": "VERIFY",
                "stage_status": "pass",
                "last_error": "",
                "details": {"status": "pass"},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_run_artifacts(
    planning_dir: Path,
    *,
    updated_at: str,
    stop_reason: str = "PASS",
) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASKS.md").write_text("## T001: Implement ranking rules\n", encoding="utf-8")
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps(_state_payload(planning_dir, updated_at=updated_at, stop_reason=stop_reason)),
        encoding="utf-8",
    )
    _write_rounds_pass(planning_dir)


def _write_gate_result(planning_dir: Path, *, total_status: str, blocking_violations: int = 0) -> None:
    (planning_dir / ".gate_result.json").write_text(
        json.dumps(
            {
                "contract_version": "ws114.v2",
                "total_status": total_status,
                "blocking_violations": blocking_violations,
                "profile": {"name": "blocking" if total_status == "BLOCKED" else "advisory"},
            }
        ),
        encoding="utf-8",
    )


def _write_compact_context(
    planning_dir: Path,
    *,
    runtime_status: str = "partial",
    runtime_mode: str = "compat",
    instincts_status: str = "store_not_found",
    instincts_loaded: bool = False,
    merged_absorption_status: dict[str, str] | None = None,
) -> None:
    resolved_status = merged_absorption_status or {
        "planning_summary": "已吸收",
        "context_compact": "部分吸收",
        "instincts": "部分吸收",
    }
    (planning_dir / "compact_context.json").write_text(
        json.dumps(
            {
                "runtime_status": runtime_status,
                "runtime_mode": runtime_mode,
                "instincts_status": instincts_status,
                "instincts_loaded": instincts_loaded,
                "merged_absorption_status": resolved_status,
            }
        ),
        encoding="utf-8",
    )


def _write_workflow_chain(
    planning_dir: Path,
    *,
    final_status: str = "PASS",
    final_reason: str = "ALL_TASKS_COMPLETE",
) -> None:
    (planning_dir / ".workflow_chain.json").write_text(
        json.dumps(
            {
                "version": "ws115.chain.v1",
                "feature": planning_dir.name,
                "final_outcome": {
                    "status": final_status,
                    "reason": final_reason,
                    "blocking_reason": "",
                },
            }
        ),
        encoding="utf-8",
    )


def test_cli_stability_report_respects_updated_since_and_until(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    _write_run_artifacts(planning_root / "run-old", updated_at="2026-03-10T00:00:00+00:00")
    _write_run_artifacts(planning_root / "run-middle", updated_at="2026-03-15T10:00:00+00:00")
    _write_run_artifacts(planning_root / "run-new", updated_at="2026-03-17T00:00:00+00:00")

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--all-runs",
            "--updated-since",
            "2026-03-14",
            "--updated-until",
            "2026-03-16",
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_runs"] == 1
    assert payload["run_ids"] == ["run-middle"]
    assert payload["skipped_runs"] == 0
    assert payload["project_root"] == str(tmp_path.resolve())
    assert payload["selection"]["all_runs"] is True
    assert payload["selection"]["updated_since"] == "2026-03-14T00:00:00+00:00"
    assert payload["selection"]["updated_until"] == "2026-03-16T23:59:59.999999+00:00"
    assert payload["resolved_planning_dirs"] == [str((planning_root / "run-middle").resolve())]
    assert payload["provenance"]["command"] == "stability-report"


def test_cli_stability_report_scan_all_discovers_rounds_only_dir(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    _write_run_artifacts(planning_root / "run-good", updated_at="2026-03-15T10:00:00+00:00")

    rounds_only_dir = planning_root / "run-rounds-only"
    rounds_only_dir.mkdir(parents=True, exist_ok=True)
    (rounds_only_dir / ".autopilot_rounds.jsonl").write_text(
        json.dumps(
            {
                "stage": "VERIFY",
                "stage_status": "setup_error",
                "last_error": "missing state file",
                "details": {"status": "setup_error"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--all-runs",
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_runs"] == 1
    assert payload["run_ids"] == ["run-good"]
    assert payload["skipped_runs"] == 1
    assert "run-rounds-only" in payload["warnings"][0]
    assert payload["provenance"]["command"] == "stability-report"


def test_cli_stability_report_supports_run_id_and_relative_output_path(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    _write_run_artifacts(planning_root / "run-old", updated_at="2026-03-12T10:00:00+00:00")
    _write_run_artifacts(planning_root / "run-picked", updated_at="2026-03-16T10:00:00+00:00")

    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-picked",
            "--output",
            "reports/merged/STABILITY.md",
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)

    expected_output = (tmp_path / "reports" / "merged" / "STABILITY.md").resolve()
    assert payload["total_runs"] == 1
    assert payload["run_ids"] == ["run-picked"]
    assert payload["skipped_runs"] == 0
    assert payload["output_path"] == str(expected_output)
    assert expected_output.exists()
    assert payload["selection"]["run_ids"] == ["run-picked"]
    assert payload["selection"]["all_runs"] is False
    assert payload["provenance"]["command"] == "stability-report"


def test_cli_stability_report_mixed_selection_skips_explicit_damaged_run(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    good_dir = planning_root / "run-good"
    bad_dir = planning_root / "run-bad"
    _write_run_artifacts(good_dir, updated_at="2026-03-16T10:00:00+00:00")
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / ".autopilot_state.json").write_bytes(b"\x00\xffbroken")
    (bad_dir / ".autopilot_rounds.jsonl").write_text(
        json.dumps({"stage": "VERIFY", "stage_status": "setup_error", "last_error": "bad state"}) + "\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-good",
            "--planning-dir",
            str(bad_dir),
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_runs"] == 1
    assert payload["run_ids"] == ["run-good"]
    assert payload["skipped_runs"] == 1
    assert "run-bad" in payload["warnings"][0]
    assert payload["selection"]["run_ids"] == ["run-good"]
    assert payload["selection"]["planning_dirs"] == [str(bad_dir.resolve())]
    assert payload["provenance"]["command"] == "stability-report"


def test_cli_stability_report_counts_gate_blocked_issue_from_gate_result(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    blocked_dir = planning_root / "run-blocked"
    _write_run_artifacts(
        blocked_dir,
        updated_at="2026-03-16T10:00:00+00:00",
        stop_reason="HARD_ERROR",
    )
    _write_gate_result(blocked_dir, total_status="BLOCKED", blocking_violations=2)

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-blocked",
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_runs"] == 1
    assert payload["skipped_runs"] == 0
    assert payload["run_ids"] == ["run-blocked"]

    report = output_path.read_text(encoding="utf-8")
    assert "| Gate Blocked | 1 |" in report
    assert "| round_outcome |" in report
    assert "| run_outcome |" in report
    assert "blocked_by_gate:1" in report


def test_cli_stability_report_run_outcome_uses_unified_blocked_when_stop_reason_missing(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    blocked_dir = planning_root / "run-blocked-unified"
    _write_run_artifacts(
        blocked_dir,
        updated_at="2026-03-16T10:00:00+00:00",
        stop_reason="",
    )

    state_path = blocked_dir / ".autopilot_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["stop_reason"] = None
    state["final_status"] = "BLOCKED"
    state["unified_status"] = {
        "is_blocked": True,
        "final_status": "BLOCKED",
        "stop_reason": "",
        "blocking_reason": "MAX_CYCLES",
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-blocked-unified",
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_outcome_counts"]["blocked"] == 1
    assert payload["root_cause_bucket_counts"]["runtime_error"] == 1
    report = output_path.read_text(encoding="utf-8")
    assert "| run_outcome | blocked:1 |" in report


def test_cli_stability_report_marks_ready_for_gate_when_chain_passed_but_gate_not_run(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    ready_dir = planning_root / "run-ready-for-gate"
    _write_run_artifacts(
        ready_dir,
        updated_at="2026-03-16T10:00:00+00:00",
        stop_reason="",
    )
    state_path = ready_dir / ".autopilot_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["current_stage"] = "GATE"
    state["last_stage_status"] = "ready_for_gate"
    state["active_task"] = "T001: Implement ranking rules"
    state["stop_reason"] = None
    state["final_status"] = None
    state["unified_status"] = {
        "current_phase": "GATE",
        "stage_status": "ready_for_gate",
        "final_status": None,
        "stop_reason": None,
        "blocking_reason": "",
        "is_blocked": False,
        "is_terminal": False,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    _write_workflow_chain(ready_dir)

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-ready-for-gate",
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_outcome_counts"]["ready_for_gate"] == 1
    assert payload["root_cause_bucket_counts"]["ready_for_gate"] == 1
    report = output_path.read_text(encoding="utf-8")
    assert "ready_for_gate:1" in report


def test_cli_stability_report_prefers_workflow_chain_blocked_reason_over_state_stop_reason(
    tmp_path: Path,
    capsys,
) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    blocked_dir = planning_root / "run-chain-blocked"
    _write_run_artifacts(
        blocked_dir,
        updated_at="2026-03-16T10:00:00+00:00",
        stop_reason="HARD_ERROR",
    )
    _write_workflow_chain(
        blocked_dir,
        final_status="BLOCKED",
        final_reason="TASK_BLOCKED",
    )

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--run-id",
            "run-chain-blocked",
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_outcome_counts"]["blocked:task_blocked"] == 1
    assert payload["root_cause_bucket_counts"]["task_blocked"] == 1
    report = output_path.read_text(encoding="utf-8")
    assert "blocked:task_blocked:1" in report


def _assert_compact_observation_report(report: str) -> None:
    assert "## P0 诊断指标（v2）" in report
    assert "error_category_distribution" in report
    assert "root_cause_bucket_distribution" in report
    assert "repeated_failure_rate" in report
    assert "compact_hit_rate" in report
    assert "learned_instinct_hit_rate" in report
    assert "setup_recovery_success_rate" in report
    assert "stuck_round_limit_distribution" in report
    assert "### 6.1 Compact / Instincts 观测" in report
    assert "context_compact(runtime/mode)" in report
    assert "partial/compat:2" in report
    assert "instincts_status" in report
    assert "loaded:1" in report
    assert "store_not_found:1" in report
    assert "merged_absorption_status(sample)" in report
    assert "run_outcome" in report
    assert "pass:2" in report
    assert "planning_summary" in report
    assert "context_compact" in report
    assert "instincts" in report
    assert "round_outcome" in report
    assert "stable_pass:2" in report
    assert "absorption=planning_summary:已吸收/context_compact:部分吸收/instincts:部分吸收" in report


def test_cli_stability_report_renders_compact_and_instincts_observation(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    run_loaded = planning_root / "run-loaded"
    run_missing = planning_root / "run-missing"
    _write_run_artifacts(run_loaded, updated_at="2026-03-16T10:00:00+00:00")
    _write_run_artifacts(run_missing, updated_at="2026-03-16T12:00:00+00:00")
    _write_compact_context(
        run_loaded,
        runtime_status="partial",
        runtime_mode="compat",
        instincts_status="loaded",
        instincts_loaded=True,
    )
    _write_compact_context(
        run_missing,
        runtime_status="partial",
        runtime_mode="compat",
        instincts_status="store_not_found",
        instincts_loaded=False,
    )

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    args = parser.parse_args(
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--all-runs",
            "--output",
            str(output_path),
        ]
    )
    rc = args.handler(args)
    assert rc == 0

    _assert_compact_observation_report(output_path.read_text(encoding="utf-8"))

import json
from pathlib import Path

from kodawari.cli.main import build_parser


REQUIRED_ARTIFACTS = ["PLAN.md", "TASKS.md", "ACCEPTANCE.md", "GATE.md"]
STATUS_READ_ORDER = [".autopilot_state.json", ".gate_result.json", "GATE.md"]


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _run_cli(parser, capsys, argv: list[str]) -> dict[str, object]:
    args = parser.parse_args(argv)
    rc = args.handler(args)
    payload = json.loads(capsys.readouterr().out)
    payload["__rc__"] = rc
    return payload


def _write_complex_file(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "def complex_branch(x):",
                "    score = 0",
                "    if x > 0:",
                "        score += 1",
                "    if x > 1:",
                "        score += 1",
                "    if x > 2:",
                "        score += 1",
                "    if x > 3:",
                "        score += 1",
                "    if x > 4:",
                "        score += 1",
                "    if x > 5:",
                "        score += 1",
                "    if x > 6:",
                "        score += 1",
                "    if x > 7:",
                "        score += 1",
                "    if x > 8:",
                "        score += 1",
                "    if x > 9:",
                "        score += 1",
                "    if x > 10:",
                "        score += 1",
                "    return score",
            ]
        ),
        encoding="utf-8",
    )


def _assert_review_fix_progress(rounds: list[dict[str, object]]) -> None:
    assert all(row.get("round_outcome") for row in rounds)
    review_indexes = _matching_indexes(rounds, stage="PEER_REVIEW", stage_status="changes_requested")
    fix_indexes = _matching_indexes(rounds, stage="FIX_ROUND")
    verify_indexes = _matching_indexes(rounds, stage="VERIFY")
    gate_indexes = _matching_indexes(rounds, stage="RULES_GATE")
    self_review_indexes = _matching_indexes(rounds, stage="SELF_REVIEW")
    assert review_indexes
    assert fix_indexes
    assert verify_indexes
    assert gate_indexes
    assert review_indexes[0] < fix_indexes[0]
    _assert_round_outcomes(rounds, stage="PEER_REVIEW", stage_status="changes_requested", expected="needs_fix")
    _assert_round_outcomes(rounds, stage="FIX_ROUND", expected="success")
    _assert_round_outcomes(rounds, stage="VERIFY", expected="success")
    _assert_round_outcomes(rounds, stage="RULES_GATE", expected="success")
    if self_review_indexes:
        _assert_round_outcomes(rounds, stage="SELF_REVIEW", expected="success")
    gate_rows = _matching_rows(rounds, stage="PROCEED_TO_GATE")
    assert gate_rows
    assert gate_rows[-1].get("round_outcome") == "ready_for_gate"


def _matching_rows(
    rounds: list[dict[str, object]],
    *,
    stage: str,
    stage_status: str | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in rounds:
        if row.get("stage") != stage:
            continue
        if stage_status is not None and row.get("stage_status") != stage_status:
            continue
        rows.append(row)
    return rows


def _matching_indexes(
    rounds: list[dict[str, object]],
    *,
    stage: str,
    stage_status: str | None = None,
) -> list[int]:
    indexes: list[int] = []
    for index, row in enumerate(rounds):
        if row.get("stage") != stage:
            continue
        if stage_status is not None and row.get("stage_status") != stage_status:
            continue
        indexes.append(index)
    return indexes


def _assert_round_outcomes(
    rounds: list[dict[str, object]],
    *,
    stage: str,
    expected: str,
    stage_status: str | None = None,
) -> None:
    rows = _matching_rows(rounds, stage=stage, stage_status=stage_status)
    assert rows
    assert all(row.get("round_outcome") == expected for row in rows)


def _run_autopilot_for_feature(parser, capsys, *, tmp_path: Path, feature: str, requirements: Path) -> dict[str, object]:
    payload = _run_cli(
        parser,
        capsys,
        [
            "autopilot",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--tier",
            "heavy",
            "--requirements-file",
            str(requirements),
        ],
    )
    assert payload["__rc__"] == 0
    assert payload["run_reason"] == "PROCEED_TO_GATE"
    return payload


def _run_autopilot_with_max_cycles(
    parser,
    capsys,
    *,
    tmp_path: Path,
    feature: str,
    requirements: Path,
    max_cycles: int,
) -> dict[str, object]:
    payload = _run_cli(
        parser,
        capsys,
        [
            "autopilot",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--tier",
            "heavy",
            "--requirements-file",
            str(requirements),
            "--max-cycles",
            str(max_cycles),
        ],
    )
    return payload


def _assert_planning_files(planning_dir: Path, rounds_path: Path, state_path: Path, *, tmp_path: Path, feature: str) -> None:
    assert planning_dir == (tmp_path / "planning" / feature)
    assert state_path.exists()
    assert rounds_path.exists()
    assert (planning_dir / "PLAN.md").exists()
    assert (planning_dir / "TASKS.md").exists()
    assert (planning_dir / "ACCEPTANCE.md").exists()


def _assert_runtime_compact_artifacts(planning_dir: Path) -> None:
    compact_md = planning_dir / "COMPACT_CONTEXT.md"
    compact_json = planning_dir / "compact_context.json"
    assert compact_md.exists()
    assert compact_json.exists()
    compact_payload = json.loads(compact_json.read_text(encoding="utf-8"))
    assert compact_payload["runtime_trigger_event"] == "pre_compact"
    assert compact_payload["runtime_status"] == "partial"
    assert compact_payload["runtime_mode"] == "compat"
    assert compact_payload["instincts_loaded"] is False
    assert compact_payload["instincts_status"] == "store_not_found"
    assert compact_payload["post_loop"]["reason"] == "PROCEED_TO_GATE"
    assert compact_payload["post_loop"]["stop_reason"] == "PASS"
    assert compact_payload["loop_stop_reason"] == "PASS"
    assert compact_payload["loop_blocked"] is False
    assert compact_payload["merged_absorption_status"] == {
        "planning_summary": "已吸收",
        "context_compact": "部分吸收",
        "instincts": "部分吸收",
    }


def _status_payload(parser, capsys, *, tmp_path: Path, feature: str) -> dict[str, object]:
    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", feature])
    assert payload["__rc__"] == 0
    return payload


def _run_advisory_gate(parser, capsys, *, tmp_path: Path, feature: str) -> dict[str, object]:
    payload = _run_cli(
        parser,
        capsys,
        # --scope=full: these merged-smoke tests simulate project-wide state
        # (files written after autopilot, outside the planner's changed_files
        # set) and expect whole-project scanning to classify them.
        ["gate", "--project-root", str(tmp_path), "--feature", feature, "--profile", "advisory", "--scope", "full"],
    )
    assert payload["__rc__"] == 0
    return payload


def _run_blocking_gate(parser, capsys, *, tmp_path: Path, feature: str) -> dict[str, object]:
    payload = _run_cli(
        parser,
        capsys,
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--profile",
            "blocking",
            # See _run_advisory_gate comment: project-wide semantics.
            "--scope",
            "full",
            "--fail-on-block",
        ],
    )
    assert payload["__rc__"] == 2
    return payload


def _run_stability_report(parser, capsys, *, tmp_path: Path, planning_dir: Path, output_path: Path) -> dict[str, object]:
    payload = _run_cli(
        parser,
        capsys,
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--planning-dir",
            str(planning_dir),
            "--output",
            str(output_path),
        ],
    )
    assert payload["__rc__"] == 0
    return payload


def _run_stability_report_all_runs(parser, capsys, *, tmp_path: Path, output_path: Path) -> dict[str, object]:
    payload = _run_cli(
        parser,
        capsys,
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--all-runs",
            "--output",
            str(output_path),
        ],
    )
    assert payload["__rc__"] == 0
    return payload


def _assert_report_markdown(report_path: Path, feature: str) -> None:
    assert report_path.exists()
    markdown = report_path.read_text(encoding="utf-8")
    assert "kodawari 自动化稳定性报告" in markdown
    assert "merged_absorption_status(sample)" in markdown
    assert "planning_summary" in markdown
    assert "context_compact" in markdown
    assert "instincts" in markdown
    assert "round_outcome" in markdown
    assert "run_outcome" in markdown
    assert feature in markdown


def _assert_status_before_gate(status_payload: dict[str, object]) -> None:
    assert status_payload["contract_version"] == "ws115.v1"
    assert status_payload["planning_contract"]["version"] == "ws115.v1"
    assert status_payload["planning_contract"]["required_artifacts"] == REQUIRED_ARTIFACTS
    assert status_payload["planning_contract"]["status_read_order"] == STATUS_READ_ORDER
    # RULES_GATE round now writes GATE.md + .gate_result.json as part of the
    # autopilot loop, so planning_contract is complete after the autopilot run.
    assert status_payload["planning_contract"]["complete"] is True
    assert status_payload["artifacts"]["PLAN.md"]["exists"] is True
    assert status_payload["artifacts"]["TASKS.md"]["exists"] is True
    assert status_payload["artifacts"]["ACCEPTANCE.md"]["exists"] is True
    assert status_payload["artifacts"]["GATE.md"]["exists"] is True
    assert status_payload["artifacts"][".gate_result.json"]["exists"] is True
    assert status_payload["compact_context"]["runtime_status"] == "partial"
    assert status_payload["compact_context"]["runtime_mode"] == "compat"
    assert status_payload["gate"] is not None


def _assert_status_after_gate(status_payload: dict[str, object], *, total_status: str) -> None:
    assert status_payload["planning_contract"]["complete"] is True
    assert status_payload["artifacts"]["GATE.md"]["exists"] is True
    assert status_payload["artifacts"][".gate_result.json"]["exists"] is True
    assert status_payload["compact_context"]["runtime_status"] == "partial"
    assert status_payload["compact_context"]["runtime_mode"] == "compat"
    assert status_payload["gate"]["source"] == ".gate_result.json"
    assert status_payload["gate"]["total_status"] == total_status
    unified = status_payload["state"]["unified_status"]
    assert unified["current_phase"] == "COMPLETED"
    assert unified["is_terminal"] is True
    if total_status == "PASS":
        assert unified["final_status"] == "PASS"
        assert unified["stop_reason"] == "PASS"
        assert unified["stage_status"] == "gate_passed"
        assert unified["is_blocked"] is False
    else:
        assert unified["final_status"] == "BLOCKED"
        assert unified["stop_reason"] == "HARD_ERROR"
        assert unified["stage_status"] == "gate_blocked"
        assert unified["is_blocked"] is True


def test_kodawari_merged_smoke_chain(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("", encoding="utf-8")
    feature = "ws115-merged"

    autopilot_payload = _run_autopilot_for_feature(
        parser,
        capsys,
        tmp_path=tmp_path,
        feature=feature,
        requirements=requirements,
    )

    planning_dir = Path(autopilot_payload["planning_dir"])
    rounds_path = Path(autopilot_payload["rounds_path"])
    state_path = Path(autopilot_payload["state_path"])
    _assert_planning_files(planning_dir, rounds_path, state_path, tmp_path=tmp_path, feature=feature)
    _assert_runtime_compact_artifacts(planning_dir)

    rounds = _read_jsonl(rounds_path)
    _assert_review_fix_progress(rounds)

    status_before_gate = _status_payload(parser, capsys, tmp_path=tmp_path, feature=feature)
    assert status_before_gate["state"]["unified_status"]["current_phase"] == "GATE"
    _assert_status_before_gate(status_before_gate)

    (tmp_path / "module.py").write_text("def sample(x):\n    return x\n", encoding="utf-8")
    gate_payload = _run_advisory_gate(parser, capsys, tmp_path=tmp_path, feature=feature)
    assert gate_payload["total_status"] == "PASS"
    assert (planning_dir / ".gate_result.json").exists()
    assert (planning_dir / "GATE.md").exists()

    status_after_gate = _status_payload(parser, capsys, tmp_path=tmp_path, feature=feature)
    _assert_status_after_gate(status_after_gate, total_status=gate_payload["total_status"])

    report_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    report_payload = _run_stability_report(
        parser,
        capsys,
        tmp_path=tmp_path,
        planning_dir=planning_dir,
        output_path=report_path,
    )

    assert report_payload["total_runs"] == 1
    assert report_payload["project_root"] == str(tmp_path.resolve())
    assert report_payload["provenance"]["command"] == "stability-report"
    assert report_payload["run_outcome_counts"]["pass"] == 1
    _assert_report_markdown(report_path, feature)


def test_kodawari_blocked_smoke_all_runs_skips_damaged_state(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("blocked all-runs skip damaged\n", encoding="utf-8")
    feature = "ws115-merged-blocked-skip-damaged"

    autopilot_payload = _run_autopilot_for_feature(
        parser,
        capsys,
        tmp_path=tmp_path,
        feature=feature,
        requirements=requirements,
    )

    planning_dir = Path(autopilot_payload["planning_dir"])
    _assert_runtime_compact_artifacts(planning_dir)
    _write_complex_file(tmp_path / "module.py")
    gate_payload = _run_blocking_gate(parser, capsys, tmp_path=tmp_path, feature=feature)
    assert gate_payload["total_status"] == "BLOCKED"

    status_after_gate = _status_payload(parser, capsys, tmp_path=tmp_path, feature=feature)
    _assert_status_after_gate(status_after_gate, total_status="BLOCKED")

    damaged_dir = tmp_path / "planning" / "run-damaged-state"
    damaged_dir.mkdir(parents=True, exist_ok=True)
    (damaged_dir / ".autopilot_state.json").write_bytes(b"\x00\xffbad-state")
    (damaged_dir / ".autopilot_rounds.jsonl").write_text(
        json.dumps({"stage": "VERIFY", "stage_status": "setup_error", "last_error": "broken state"}) + "\n",
        encoding="utf-8",
    )

    report_path = tmp_path / "AUTOMATION_STABILITY_REPORT_ALL_RUNS.md"
    report_payload = _run_stability_report_all_runs(
        parser,
        capsys,
        tmp_path=tmp_path,
        output_path=report_path,
    )
    assert report_payload["total_runs"] == 1
    assert report_payload["run_ids"] == [feature]
    assert report_payload["skipped_runs"] == 1
    assert "run-damaged-state" in report_payload["warnings"][0]
    assert report_payload["provenance"]["command"] == "stability-report"

    markdown = report_path.read_text(encoding="utf-8")
    assert "## 数据质量说明" in markdown
    assert feature in markdown
    assert planning_dir.exists()


def test_kodawari_merged_smoke_blocked_gate_chain(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("force blocked gate smoke\n", encoding="utf-8")
    feature = "ws115-merged-blocked"

    autopilot_payload = _run_autopilot_for_feature(
        parser,
        capsys,
        tmp_path=tmp_path,
        feature=feature,
        requirements=requirements,
    )

    planning_dir = Path(autopilot_payload["planning_dir"])
    rounds_path = Path(autopilot_payload["rounds_path"])
    state_path = Path(autopilot_payload["state_path"])
    _assert_planning_files(planning_dir, rounds_path, state_path, tmp_path=tmp_path, feature=feature)
    _assert_runtime_compact_artifacts(planning_dir)

    rounds = _read_jsonl(rounds_path)
    _assert_review_fix_progress(rounds)

    _write_complex_file(tmp_path / "module.py")
    gate_payload = _run_blocking_gate(parser, capsys, tmp_path=tmp_path, feature=feature)
    assert gate_payload["total_status"] == "BLOCKED"
    assert gate_payload["blocking_violations"] > 0
    assert (planning_dir / ".gate_result.json").exists()
    assert (planning_dir / "GATE.md").exists()

    status_after_gate = _status_payload(parser, capsys, tmp_path=tmp_path, feature=feature)
    _assert_status_after_gate(status_after_gate, total_status="BLOCKED")
    assert status_after_gate["gate"]["blocking_violations"] > 0

    report_path = tmp_path / "AUTOMATION_STABILITY_REPORT_BLOCKED.md"
    report_payload = _run_stability_report(
        parser,
        capsys,
        tmp_path=tmp_path,
        planning_dir=planning_dir,
        output_path=report_path,
    )

    assert report_payload["total_runs"] == 1
    assert report_payload["provenance"]["command"] == "stability-report"
    assert report_payload["run_outcome_counts"]["blocked_by_gate"] == 1
    _assert_report_markdown(report_path, feature)


def test_kodawari_merged_smoke_stopped_by_max_cycles(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("force max cycles stop smoke\n", encoding="utf-8")
    feature = "ws115-merged-stopped-max-cycles"

    autopilot_payload = _run_autopilot_with_max_cycles(
        parser,
        capsys,
        tmp_path=tmp_path,
        feature=feature,
        requirements=requirements,
        max_cycles=1,
    )

    assert autopilot_payload["__rc__"] == 1
    assert autopilot_payload["status"] == "blocked"
    assert autopilot_payload["run_reason"] == "MAX_CYCLES_REACHED"
    assert autopilot_payload["unified_status"]["stop_reason"] == "MAX_CYCLES"
    assert autopilot_payload["unified_status"]["is_blocked"] is True
    planning_dir = Path(autopilot_payload["planning_dir"])
    assert (planning_dir / "PLAN.md").exists()
    assert (planning_dir / "TASKS.md").exists()
    assert (planning_dir / "ACCEPTANCE.md").exists()
    # RULES_GATE (cost=0) runs before MAX_CYCLES triggers in the task_cycle,
    # so GATE.md is written even though the run ends with MAX_CYCLES_REACHED.
    assert (planning_dir / "GATE.md").exists() is True

    status_payload = _status_payload(parser, capsys, tmp_path=tmp_path, feature=feature)
    assert status_payload["state"]["unified_status"]["stop_reason"] == "MAX_CYCLES"
    assert status_payload["state"]["unified_status"]["is_blocked"] is True
    assert status_payload["planning_contract"]["complete"] is True
    assert status_payload["gate"] is not None

    report_path = tmp_path / "AUTOMATION_STABILITY_REPORT_STOPPED.md"
    report_payload = _run_stability_report(
        parser,
        capsys,
        tmp_path=tmp_path,
        planning_dir=planning_dir,
        output_path=report_path,
    )

    assert report_payload["run_outcome_counts"]["stopped:max_cycles"] == 1
    assert report_payload["round_outcome_counts"]["blocked"] >= 1
    markdown = report_path.read_text(encoding="utf-8")
    assert "| run_outcome | stopped:max_cycles:1 |" in markdown

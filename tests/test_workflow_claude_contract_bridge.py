import json
from pathlib import Path

import pytest

from kodawari.cli.main import build_parser
from kodawari.spec_generator.materializer import summarize_plan


REQUIRED_ARTIFACTS = ["PLAN.md", "TASKS.md", "ACCEPTANCE.md", "GATE.md"]
REQUIRED_ARTIFACT_SEMANTICS = {
    "PLAN.md": "planning_scope_and_strategy",
    "TASKS.md": "execution_backlog_and_task_order",
    "ACCEPTANCE.md": "acceptance_criteria_checklist",
    "GATE.md": "human_readable_gate_decision_summary",
}
STATUS_READ_ORDER = [".autopilot_state.json", ".gate_result.json", "GATE.md"]


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


def _run_cli(parser, capsys, argv: list[str]) -> dict[str, object]:
    args = parser.parse_args(argv)
    rc = args.handler(args)
    payload = json.loads(capsys.readouterr().out)
    payload["__rc__"] = rc
    return payload


def _assert_gate_defaults(planning_contract: dict[str, object]) -> None:
    gate_defaults = planning_contract["gate_defaults"]
    assert gate_defaults["profile"] == "advisory"
    assert gate_defaults["mode"] == "non-blocking"
    assert gate_defaults["redline"]["file_max_lines"] == 1500
    assert gate_defaults["redline"]["function_max_lines"] == 10000
    assert gate_defaults["redline"]["nesting_max"] == 4
    assert gate_defaults["redline"]["complexity_max"] == 7
    assert gate_defaults["redline"]["complexity_warn"] == 7
    assert gate_defaults["redline"]["complexity_block"] == 10
    assert gate_defaults["redline"]["file_complexity_warn_lines"] == 1000
    assert gate_defaults["redline"]["file_complexity_warn_sum"] == 20
    assert gate_defaults["redline"]["file_complexity_block_lines"] == 1500
    assert gate_defaults["redline"]["file_complexity_block_sum"] == 30
    assert gate_defaults["redline"]["max_violations"] == 50
    assert gate_defaults["redline"]["severity"] == "WARNING"


def _assert_planning_contract_shape(planning_contract: dict[str, object]) -> None:
    assert planning_contract["required_artifacts"] == REQUIRED_ARTIFACTS
    assert planning_contract["artifact_semantics"] == REQUIRED_ARTIFACT_SEMANTICS
    assert planning_contract["status_read_order"] == STATUS_READ_ORDER


def _assert_autopilot_planning_artifacts(planning_dir: Path, autopilot_payload: dict[str, object], tmp_path: Path) -> None:
    assert planning_dir == (tmp_path / "planning" / "demo")
    assert (planning_dir / "PLAN.md").exists()
    assert (planning_dir / "TASKS.md").exists()
    assert (planning_dir / "ACCEPTANCE.md").exists()
    assert (planning_dir / ".autopilot_state.json").exists()
    assert autopilot_payload["planning_contract"]["version"] == "ws115.v1"
    assert autopilot_payload["planning_contract"]["required_artifacts"] == REQUIRED_ARTIFACTS
    assert autopilot_payload["planning_contract"]["complete"] is False


def _assert_status_contract(status_payload: dict[str, object]) -> None:
    assert status_payload["contract_version"] == "ws115.v1"
    assert status_payload["planning_contract"]["version"] == "ws115.v1"
    _assert_planning_contract_shape(status_payload["planning_contract"])
    assert status_payload["planning_contract"]["complete"] is True
    assert status_payload["artifacts"]["PLAN.md"]["exists"] is True
    assert status_payload["artifacts"]["TASKS.md"]["exists"] is True
    assert status_payload["artifacts"]["ACCEPTANCE.md"]["exists"] is True
    assert status_payload["artifacts"]["GATE.md"]["exists"] is True
    assert status_payload["gate"]["source"] == ".gate_result.json"
    assert status_payload["gate"]["profile"] == "advisory"
    assert status_payload["gate"]["total_status"] == "PASS"
    assert status_payload["compact_context"]["runtime_status"] == "partial"
    assert status_payload["compact_context"]["runtime_mode"] == "compat"
    assert status_payload["compact_context"]["instincts_status"] == "store_not_found"
    assert status_payload["state"]["unified_status"]["current_phase"] == "COMPLETED"
    assert status_payload["state"]["unified_status"]["final_status"] == "PASS"
    assert status_payload["state"]["unified_status"]["stop_reason"] == "PASS"
    assert status_payload["absorption_status"]["planning_summary"]["status"] == "absorbed"
    assert status_payload["absorption_status"]["context_compact"]["status"] == "partial"
    assert status_payload["absorption_status"]["instincts"]["status"] == "partial"
    _assert_gate_defaults(status_payload["planning_contract"])


def test_merged_planning_status_gate_contract(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("Build merged planning contract path\n", encoding="utf-8")

    autopilot_payload = _run_cli(
        parser,
        capsys,
        ["autopilot", "--project-root", str(tmp_path), "--feature", "demo", "--tier", "heavy", "--requirements-file", str(requirements)],
    )
    assert autopilot_payload["__rc__"] == 0

    planning_dir = Path(autopilot_payload["planning_dir"])
    _assert_autopilot_planning_artifacts(planning_dir, autopilot_payload, tmp_path)

    gate_payload = _run_cli(
        parser,
        capsys,
        ["gate", "--project-root", str(tmp_path), "--feature", "demo", "--profile", "advisory"],
    )
    assert gate_payload["__rc__"] == 0
    assert gate_payload["contract_version"] == "ws115.v1"
    assert gate_payload["total_status"] == "PASS"
    assert gate_payload["profile"]["name"] == "advisory"
    assert gate_payload["profile"]["mode"] == "advisory"
    assert gate_payload["profile"]["thresholds"] == {
        "file_max_lines": 1500,
        "function_max_lines": 10000,
        "nesting_max": 4,
        "complexity_max": 7,
        "complexity_warn": 7,
        "complexity_block": 10,
        "file_complexity_warn_lines": 1000,
        "file_complexity_warn_sum": 20,
        "file_complexity_block_lines": 1500,
        "file_complexity_block_sum": 30,
        "max_violations": 50,
        "severity": "WARNING",
    }
    assert (planning_dir / "GATE.md").exists()
    assert (planning_dir / ".gate_result.json").exists()

    status_payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "demo"])
    assert status_payload["__rc__"] == 0
    _assert_status_contract(status_payload)


def test_status_reads_blocked_gate_result_semantics(tmp_path: Path, capsys) -> None:
    parser = build_parser()

    autopilot_payload = _run_cli(
        parser,
        capsys,
        ["autopilot", "--project-root", str(tmp_path), "--feature", "blocked", "--tier", "heavy"],
    )
    assert autopilot_payload["__rc__"] == 0

    _write_complex_file(tmp_path / "module.py")

    gate_payload = _run_cli(
        parser,
        capsys,
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--feature",
            "blocked",
            "--profile",
            "blocking",
            # Use --scope=full because this test's intent is project-wide
            # blocking behavior: it writes module.py AFTER autopilot finishes,
            # so the planning dir's .execution_result.json (if any) doesn't
            # list that file. The default --scope=auto would therefore
            # scope-past the violation; --scope=full forces the whole tree.
            "--scope",
            "full",
            "--fail-on-block",
        ],
    )
    assert gate_payload["__rc__"] == 2
    assert gate_payload["total_status"] == "BLOCKED"

    status_payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "blocked"])
    assert status_payload["__rc__"] == 0

    assert status_payload["contract_version"] == "ws115.v1"
    assert status_payload["gate"]["source"] == ".gate_result.json"
    assert status_payload["gate"]["total_status"] == "BLOCKED"
    assert status_payload["gate"]["blocking_violations"] > 0
    assert status_payload["state"]["unified_status"]["current_phase"] == "COMPLETED"
    assert status_payload["state"]["unified_status"]["final_status"] == "BLOCKED"
    assert status_payload["state"]["unified_status"]["stop_reason"] == "HARD_ERROR"


def test_status_falls_back_to_gate_markdown_when_gate_json_missing(tmp_path: Path, capsys) -> None:
    parser = build_parser()

    autopilot_payload = _run_cli(
        parser,
        capsys,
        ["autopilot", "--project-root", str(tmp_path), "--feature", "gate-fallback", "--tier", "heavy"],
    )
    assert autopilot_payload["__rc__"] == 0
    planning_dir = Path(autopilot_payload["planning_dir"])

    (tmp_path / "module.py").write_text("def sample(x):\n    return x\n", encoding="utf-8")
    gate_payload = _run_cli(
        parser,
        capsys,
        ["gate", "--project-root", str(tmp_path), "--feature", "gate-fallback", "--profile", "advisory"],
    )
    assert gate_payload["__rc__"] == 0

    gate_json = planning_dir / ".gate_result.json"
    assert gate_json.exists()
    gate_json.unlink()

    status_payload = _run_cli(
        parser,
        capsys,
        ["status", "--project-root", str(tmp_path), "--feature", "gate-fallback"],
    )
    assert status_payload["__rc__"] == 0
    assert status_payload["contract_version"] == "ws115.v1"
    _assert_planning_contract_shape(status_payload["planning_contract"])
    assert status_payload["planning_contract"]["complete"] is True
    assert status_payload["artifacts"][".gate_result.json"]["exists"] is False
    assert status_payload["artifacts"]["GATE.md"]["exists"] is True
    assert status_payload["gate"]["source"] == "GATE.md"
    assert status_payload["gate"]["total_status"] == "UNKNOWN"


def test_status_falls_back_to_gate_markdown_when_gate_json_is_corrupted(tmp_path: Path, capsys) -> None:
    parser = build_parser()

    autopilot_payload = _run_cli(
        parser,
        capsys,
        ["autopilot", "--project-root", str(tmp_path), "--feature", "gate-corrupted-json", "--tier", "heavy"],
    )
    assert autopilot_payload["__rc__"] == 0
    planning_dir = Path(autopilot_payload["planning_dir"])

    (tmp_path / "module.py").write_text("def sample(x):\n    return x\n", encoding="utf-8")
    gate_payload = _run_cli(
        parser,
        capsys,
        ["gate", "--project-root", str(tmp_path), "--feature", "gate-corrupted-json", "--profile", "advisory"],
    )
    assert gate_payload["__rc__"] == 0

    gate_json = planning_dir / ".gate_result.json"
    assert gate_json.exists()
    gate_json.write_text("{invalid json", encoding="utf-8")

    status_payload = _run_cli(
        parser,
        capsys,
        ["status", "--project-root", str(tmp_path), "--feature", "gate-corrupted-json"],
    )
    assert status_payload["__rc__"] == 0
    assert status_payload["contract_version"] == "ws115.v1"
    _assert_planning_contract_shape(status_payload["planning_contract"])
    assert status_payload["planning_contract"]["complete"] is True
    assert status_payload["artifacts"][".gate_result.json"]["exists"] is True
    assert status_payload["artifacts"]["GATE.md"]["exists"] is True
    assert status_payload["gate"]["source"] == "GATE.md"
    assert status_payload["gate"]["total_status"] == "UNKNOWN"


def test_gate_and_status_support_explicit_planning_dir_binding(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "custom-planning-dir"
    (tmp_path / "module.py").write_text("def sample(x):\n    return x\n", encoding="utf-8")

    gate_payload = _run_cli(
        parser,
        capsys,
        [
            "gate",
            "--project-root",
            str(tmp_path),
            "--planning-dir",
            str(planning_dir),
            "--profile",
            "advisory",
        ],
    )
    assert gate_payload["__rc__"] == 0
    assert Path(gate_payload["planning_dir"]) == planning_dir.resolve()
    assert Path(gate_payload["gate_artifacts"][".gate_result.json"]).exists()
    assert Path(gate_payload["gate_artifacts"]["GATE.md"]).exists()

    status_payload = _run_cli(
        parser,
        capsys,
        ["status", "--project-root", str(tmp_path), "--planning-dir", str(planning_dir)],
    )
    assert status_payload["__rc__"] == 0
    assert Path(status_payload["planning_dir"]) == planning_dir.resolve()
    assert status_payload["state_source"] == "none"
    _assert_planning_contract_shape(status_payload["planning_contract"])
    assert status_payload["planning_contract"]["complete"] is False
    assert status_payload["gate"]["source"] == ".gate_result.json"
    assert status_payload["gate"]["total_status"] == gate_payload["total_status"]


def test_status_requires_feature_when_planning_dir_is_not_provided(tmp_path: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["status", "--project-root", str(tmp_path)])
    with pytest.raises(ValueError, match="status requires --feature when --planning-dir is not provided"):
        args.handler(args)


def test_planning_summary_helper_is_explicit_over_ws113_artifacts(tmp_path: Path, capsys) -> None:
    parser = build_parser()

    autopilot_payload = _run_cli(
        parser,
        capsys,
        ["autopilot", "--project-root", str(tmp_path), "--feature", "summary-compat", "--tier", "heavy"],
    )
    assert autopilot_payload["__rc__"] == 0
    planning_dir = Path(autopilot_payload["planning_dir"])

    existing_sections = [
        name
        for name in ["PLAN.md", "TASKS.md", "ACCEPTANCE.md"]
        if (planning_dir / name).exists()
    ]
    assert existing_sections == ["PLAN.md", "TASKS.md", "ACCEPTANCE.md"]

    summary = summarize_plan(
        "summary-compat",
        sections=existing_sections,
        source="ws113-artifact-bridge",
    )
    assert summary["feature"] == "summary-compat"
    assert summary["sections"] == ["PLAN.md", "TASKS.md", "ACCEPTANCE.md"]
    assert summary["section_count"] == 3
    assert summary["options"]["source"] == "ws113-artifact-bridge"


def test_compact_contract_reports_partial_absorption_boundary(tmp_path: Path, capsys) -> None:
    parser = build_parser()
    compact_payload = _run_cli(
        parser,
        capsys,
        [
            "compact",
            "--project-root",
            str(tmp_path),
            "--feature",
            "compact-contract",
        ],
    )
    assert compact_payload["__rc__"] == 0
    assert compact_payload["compatibility"]["status"] == "COMPAT_SHIM"
    assert compact_payload["context_compact"]["runtime_triggered"] is False
    assert compact_payload["context_compact"]["entrypoint_scope"] == "compat_shim_only"
    assert compact_payload["context_compact"]["status"] == "partial"
    assert compact_payload["context_compact"]["mode"] == "compat"
    assert compact_payload["absorption_status"]["planning_summary"]["status"] == "absorbed"
    assert compact_payload["absorption_status"]["context_compact"]["status"] == "partial"
    assert compact_payload["absorption_status"]["instincts"]["status"] == "partial"

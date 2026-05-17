import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.cli import contract_first_cmd
from kodawari.cli.main import build_parser


E2E_CASES = [
    (
        "cf-canary-zh-app",
        "\n".join(
            [
                "1. business outcome（业务结果）",
                "- 让用户在喝水记录页看到目标和最近一次调整历史。",
                "",
                "2. source of truth（真实数据源）",
                "- patient_settings.daily_water_goal_ml",
                "- reminder_events.amount_ml",
                "",
                "3. flow type（流程类型）",
                "- 这是 read path，只影响 current snapshot。",
                "",
                "4. layer ownership（层级归属）",
                "- repository/data layer：需要读取 patient_settings.daily_water_goal_ml",
                "- service layer：需要聚合展示字段",
                "- route layer：需要暴露接口",
                "",
                "7. non-goals（这次不做什么）",
                "- 不改提醒生成逻辑",
            ]
        ),
        "app",
    ),
    (
        "cf-canary-en-src",
        "\n".join(
            [
                "1. business outcome",
                "- Return the current hydration goal and the latest goal-change entry in one API response.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "- db.goal_change_events",
                "",
                "3. flow type",
                "- This is a read path for the current snapshot only.",
                "",
                "4. layer ownership",
                "- repository: add the read query",
                "- service: compose the response contract",
                "- route: expose the endpoint",
                "",
                "7. non-goals",
                "- Do not change reminder generation",
            ]
        ),
        "src",
    ),
    (
        "cf-canary-monorepo",
        "\n".join(
            [
                "1. business outcome",
                "- Return the active hydration goal and latest goal update details for the packaged API service.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "- db.goal_change_events",
                "",
                "3. flow type",
                "- This is a read path for the packaged API snapshot.",
                "",
                "4. layer ownership",
                "- repository: read the packaged project data source",
                "- service: shape the packaged response",
                "- route: expose the packaged endpoint",
                "",
                "7. non-goals",
                "- Do not change write-path jobs",
            ]
        ),
        "monorepo",
    ),
]


def _run_cli(parser: Any, capsys: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    args = parser.parse_args(argv)
    rc = int(args.handler(args))
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _write_contract_first_success_artifacts(planning_dir: Path, *, feature: str, changed_files: list[str]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "semantic_compact.json").write_text(
        json.dumps({"must_fix": [], "verify_check_status": "PASS"}),
        encoding="utf-8",
    )
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps({"feature": feature, "changed_files": changed_files}),
        encoding="utf-8",
    )


def _prepare_layout(tmp_path: Path, layout_kind: str) -> None:
    if layout_kind == "app":
        (tmp_path / "app").mkdir(parents=True, exist_ok=True)
        (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
        (tmp_path / "app" / "schemas.py").write_text("class Payload: ...\n", encoding="utf-8")
        (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
        return
    if layout_kind == "src":
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "main.py").write_text("def handler():\n    return 'ok'\n", encoding="utf-8")
        (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
        (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
        return
    (tmp_path / "packages" / "api" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "packages" / "api" / "src" / "main.py").write_text("def handler():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "packages" / "api" / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "packages" / "api" / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")


def _task_run_result(feature: str, changed_files: list[str]) -> dict[str, Any]:
    evidence = {
        "file": f"planning/{feature}/.task_run_result.json",
        "rule": "review_evidence.present",
        "hit": "dual review evidence available",
        "confidence": 1.0,
    }
    return {
        "reason": "",
        "changed_files": changed_files,
        "task_delta_changed_files": changed_files,
        "execution_result": {
            "schema_version": "execution.result.v1",
            "feature": feature,
            "task": "T1: fake execution",
            "backend": "external_cli",
            "status": "PASS",
            "changed_files": changed_files,
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "returncode": 0,
            "artifacts": changed_files,
            "error_code": "",
            "blocking_reason": "",
            "summary": "executor completed",
        },
        "rounds": [],
        "verify_check": {"status": "PASS", "details": ""},
        "gate_check": {"total_status": "PASS", "details": ""},
        "review_evidence": {
            "status": "PASS",
            "checks": {"self_review_count": 1, "peer_review_count": 1, "must_fix_remaining": 0},
            "issues": [],
            "evidence": [evidence],
        },
        "compliance_report": {
            "status": "PASS",
            "checks": [
                {
                    "check_name": "review_evidence",
                    "status": "PASS",
                    "details": "",
                    "evidence": [evidence],
                    "evidence_count": 1,
                    "evidence_sufficient": True,
                    "blocking_eligible": False,
                }
            ],
        },
    }


def _expected_test_file(layout_kind: str) -> str:
    return {
        "app": "tests/test_api.py",
        "src": "tests/test_api.py",
        "monorepo": "packages/api/tests/test_api.py",
    }[layout_kind]


def _install_fake_task_runner(monkeypatch: Any, planning_dir: Path, *, feature: str, layout_kind: str) -> None:
    def _fake_run_task_card(
        args: Any,
        *,
        card: dict[str, Any],
        card_path: Path | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        del args, card_path, run_id
        changed_files = list(card.get("files_to_change") or [])
        test_file = _expected_test_file(layout_kind)
        if test_file not in changed_files:
            changed_files.append(test_file)
        _write_contract_first_success_artifacts(planning_dir, feature=feature, changed_files=changed_files)
        return _task_run_result(feature, changed_files)

    monkeypatch.setattr(contract_first_cmd, "_run_task_card", _fake_run_task_card)


def _run_planning_chain(parser: Any, capsys: Any, tmp_path: Path, *, feature: str, layout_kind: str, prd_text: str) -> tuple[Path, Path, Path]:
    planning_dir = tmp_path / "planning" / feature
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(prd_text, encoding="utf-8")
    intake_rc, intake_payload = _run_cli(
        parser, capsys, ["prd-intake", "--project-root", str(tmp_path), "--feature", feature, "--prd", str(prd_path)]
    )
    assert intake_rc == 0
    graph_args = ["task-plan", "--project-root", str(tmp_path), "--feature", feature, "--intake", intake_payload["artifacts"]["PRD_INTAKE.json"]]
    if layout_kind == "monorepo":
        arch_rc, arch_payload = _run_cli(
            parser,
            capsys,
            ["architecture-plan", "--project-root", str(tmp_path), "--feature", feature, "--intake", intake_payload["artifacts"]["PRD_INTAKE.json"]],
        )
        assert arch_rc == 0
        graph_args.extend(["--architecture-plan", arch_payload["artifacts"]["ARCHITECTURE_PLAN.json"]])
    graph_rc, graph_payload = _run_cli(parser, capsys, graph_args)
    assert graph_rc == 0
    card_rc, card_payload = _run_cli(
        parser,
        capsys,
        ["task-prepare", "--project-root", str(tmp_path), "--feature", feature, "--graph", graph_payload["artifacts"]["TASK_GRAPH.json"], "--task", "T1"],
    )
    assert card_rc == 0
    return planning_dir, Path(graph_payload["artifacts"]["TASK_GRAPH.json"]), Path(card_payload["artifacts"]["TASK_CARD.json"])


def _run_delivery_chain(parser: Any, capsys: Any, tmp_path: Path, *, feature: str, planning_dir: Path, card_path: Path) -> None:
    task_run_rc, task_run_payload = _run_cli(
        parser,
        capsys,
        ["task-run", "--project-root", str(tmp_path), "--feature", feature, "--card", str(card_path), "--strict-scope", "--contract-mode", "strict"],
    )
    assert task_run_rc == 0
    assert task_run_payload["status"] == "PASS"
    assert ".review_evidence.json" in task_run_payload["artifacts"]
    assert (planning_dir / ".review_evidence.json").exists()
    compliance_rc, compliance_payload = _run_cli(parser, capsys, ["compliance-check", "--project-root", str(tmp_path), "--feature", feature])
    assert compliance_rc == 0
    assert compliance_payload["status"] == "PASS"
    review_rc, review_payload = _run_cli(
        parser,
        capsys,
        ["review", "--project-root", str(tmp_path), "--feature", feature, "--base-branch", "main", "--fail-on-block"],
    )
    assert review_rc == 0
    assert review_payload["status"] == "PASS"
    verify_rc, verify_payload = _run_cli(parser, capsys, ["verify", "--project-root", str(tmp_path), "--feature", feature])
    assert verify_rc == 0
    assert verify_payload["status"] == "PASS"
    qa_rc, qa_payload = _run_cli(parser, capsys, ["qa", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"])
    assert qa_rc == 0
    assert qa_payload["status"] == "PASS"


def _assert_ship_readiness(parser: Any, capsys: Any, tmp_path: Path, *, feature: str, planning_dir: Path) -> None:
    (tmp_path / "AUTOMATION_EVAL_REPORT.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    ship_rc, ship_payload = _run_cli(
        parser,
        capsys,
        ["ship-readiness", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )
    assert ship_rc == 0
    assert ship_payload["status"] == "PASS"
    assert ship_payload["planning_artifact_mode"] == "contract_first"
    assert ship_payload["required_docs"]["mode"] == "contract_first"
    assert "PLAN.md" not in ship_payload["required_docs"]["required"]
    assert not (planning_dir / "PLAN.md").exists()
    assert not (planning_dir / "TASKS.md").exists()
    assert not (planning_dir / "ACCEPTANCE.md").exists()
    assert not (planning_dir / "GATE.md").exists()


@pytest.mark.parametrize(("feature", "prd_text", "layout_kind"), E2E_CASES)
def test_contract_first_cli_end_to_end_chain_reaches_ship_readiness_without_legacy_planning_docs(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
    feature: str,
    prd_text: str,
    layout_kind: str,
) -> None:
    parser = build_parser()
    _prepare_layout(tmp_path, layout_kind)
    planning_dir, _graph_path, card_path = _run_planning_chain(
        parser,
        capsys,
        tmp_path,
        feature=feature,
        layout_kind=layout_kind,
        prd_text=prd_text,
    )
    _install_fake_task_runner(monkeypatch, planning_dir, feature=feature, layout_kind=layout_kind)
    _run_delivery_chain(parser, capsys, tmp_path, feature=feature, planning_dir=planning_dir, card_path=card_path)
    _assert_ship_readiness(parser, capsys, tmp_path, feature=feature, planning_dir=planning_dir)

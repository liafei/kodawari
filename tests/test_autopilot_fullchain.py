from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from kodawari.cli.runtime.autopilot_cmd import run_autopilot_command
from kodawari.cli.runtime.autopilot_decision_runtime import (
    DecisionKind,
    build_decision_request,
    build_decision_response,
    write_decision_request,
    write_decision_response,
)
from kodawari.cli.runtime.autopilot_release_runtime import (
    AutopilotReleaseTailConfig,
    run_autopilot_release_tail,
)


@pytest.fixture()
def release_paths(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    return project_root, planning_dir


def _builder(name: str, status: str = "PASS", **extra: object):
    def _run(*, project_root: Path, planning_dir: Path, feature: str, **kwargs: object) -> dict[str, object]:
        return {
            "status": status,
            "feature": feature,
            "planning_dir": str(planning_dir),
            "builder": name,
            "kwargs": dict(kwargs),
            **extra,
        }

    return _run


def _autopilot_args(*, project_root: Path, feature: str, prd_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(project_root),
        feature=feature,
        tier="heavy",
        prd=str(prd_path),
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=True,
        enable_peer_review=False,
        task_label=None,
        task_scope=None,
        executor_backend="",
        executor_command="",
        self_review_backend="",
        self_review_command="",
        real_peer_review=False,
        require_real_peer_review=False,
        peer_review_max_tokens=4096,
    )


def _write_prd(prd_path: Path) -> None:
    prd_path.write_text(
        "\n".join(
            [
                "Business outcome:",
                "- expose a backend endpoint and a frontend surface for hydration ranking.",
                "Source of truth:",
                "- db.rankings",
                "- api.rankings",
                "Flow type:",
                "- read",
                "Layers:",
                "- route",
                "- service",
                "- frontend",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _run_autopilot(args: argparse.Namespace, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    rc = run_autopilot_command(args)
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _approve_decision(planning_dir: Path, payload: dict[str, object], *, selected_option: str = "approve") -> None:
    write_decision_response(
        planning_dir,
        build_decision_response(
            decision_id=str(payload["decision_id"]),
            selected_option=selected_option,
            rationale=f"{selected_option} via test proof",
        ),
    )


def test_release_tail_runs_all_stages_on_happy_path(release_paths: tuple[Path, Path]) -> None:
    project_root, planning_dir = release_paths
    payload = run_autopilot_release_tail(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="demo",
        config=AutopilotReleaseTailConfig(auto_eval=True, risk_profile="high", verify_command="pytest -q tests/test_api.py"),
        builders={
            "review": _builder("review"),
            "verify": _builder("verify"),
            "qa": _builder("qa"),
            "ship_readiness": _builder("ship"),
        },
    )

    assert payload["status"] == "PASS"
    assert payload["completed_stages"] == ["review", "verify", "qa", "ship_readiness"]
    assert payload["blocked_stage"] is None
    assert payload["stages"]["verify"]["kwargs"]["verify_command"] == "pytest -q tests/test_api.py"
    assert payload["stages"]["ship_readiness"]["kwargs"]["auto_eval"] is True
    assert payload["stages"]["ship_readiness"]["kwargs"]["risk_profile"] == "high"


def test_release_tail_stops_on_first_terminal_stage(release_paths: tuple[Path, Path]) -> None:
    project_root, planning_dir = release_paths
    payload = run_autopilot_release_tail(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="demo",
        builders={
            "review": _builder("review"),
            "verify": _builder("verify", status="BLOCKED", blocking_reason="verify failed", next_action="fix verify"),
            "qa": _builder("qa"),
            "ship_readiness": _builder("ship"),
        },
    )

    assert payload["status"] == "BLOCKED"
    assert payload["completed_stages"] == ["review"]
    assert payload["blocked_stage"] == "verify"
    assert payload["blocking_reason"] == "verify failed"
    assert payload["next_action"] == "fix verify"
    assert "qa" not in payload["stages"]
    assert "ship_readiness" not in payload["stages"]


def test_release_tail_passes_review_and_verify_overrides(release_paths: tuple[Path, Path]) -> None:
    project_root, planning_dir = release_paths
    payload = run_autopilot_release_tail(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="demo",
        config=AutopilotReleaseTailConfig(
            base_branch="develop",
            changed_files_override=["app/main.py"],
            scope_allow=["tests/"],
            verify_command_file="scripts/verify.cmd",
            eval_report_path="AUTOMATION_EVAL_REPORT.json",
        ),
        builders={
            "review": _builder("review"),
            "verify": _builder("verify"),
            "qa": _builder("qa"),
            "ship_readiness": _builder("ship"),
        },
    )

    review_kwargs = payload["stages"]["review"]["kwargs"]
    verify_kwargs = payload["stages"]["verify"]["kwargs"]
    ship_kwargs = payload["stages"]["ship_readiness"]["kwargs"]

    assert review_kwargs["base_branch"] == "develop"
    assert review_kwargs["changed_files_override"] == ["app/main.py"]
    assert review_kwargs["scope_allow"] == ["tests/"]
    assert verify_kwargs["verify_command_file"] == "scripts/verify.cmd"
    assert ship_kwargs["eval_report_path"] == "AUTOMATION_EVAL_REPORT.json"


def test_release_tail_rejects_non_dict_builder_payload(release_paths: tuple[Path, Path]) -> None:
    project_root, planning_dir = release_paths

    def _bad_builder(**_: object) -> list[str]:
        return ["bad"]

    with pytest.raises(ValueError, match="must return a dict payload"):
        run_autopilot_release_tail(
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            builders={
                "review": _bad_builder,
                "verify": _builder("verify"),
                "qa": _builder("qa"),
                "ship_readiness": _builder("ship"),
            },
        )


def test_autopilot_main_entry_reaches_release_tail_after_prd_approvals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = "autopilot-fullchain-proof"
    prd_path = tmp_path / "PRD.md"
    _write_prd(prd_path)
    args = _autopilot_args(project_root=tmp_path, feature=feature, prd_path=prd_path)

    def _pass_release_tail(*, project_root: Path, planning_dir: Path, feature: str, **kwargs: object) -> dict[str, object]:
        return {
            "status": "PASS",
            "entrypoint": "kodawari autopilot",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "completed_stages": ["review", "verify", "qa", "ship_readiness"],
            "blocked_stage": None,
            "blocking_reason": "",
            "next_action": "",
            "stages": {
                "review": {"status": "PASS"},
                "verify": {"status": "PASS"},
                "qa": {"status": "PASS"},
                "ship_readiness": {"status": "PASS"},
            },
        }

    monkeypatch.setattr("kodawari.cli.runtime.autopilot_cmd.run_autopilot_release_tail", _pass_release_tail)

    first_rc, first_payload = _run_autopilot(args, capsys)
    assert first_rc == 0
    assert first_payload["status"] == "awaiting_decision"
    assert first_payload["decision_kind"] == "architecture_freeze"
    assert first_payload["interaction_state"] == "AWAITING_DECISION"

    planning_dir = Path(str(first_payload["planning_dir"]))
    _approve_decision(planning_dir, first_payload)

    second_rc, second_payload = _run_autopilot(args, capsys)
    assert second_rc == 0
    assert second_payload["status"] == "awaiting_decision"
    assert second_payload["decision_kind"] == "release_approval"
    assert second_payload["planning_artifact_mode"] == "contract_first"

    _approve_decision(planning_dir, second_payload, selected_option="ship")

    third_rc, third_payload = _run_autopilot(args, capsys)
    assert third_rc == 0
    assert third_payload["status"] == "ok"
    assert third_payload["interaction_state"] == "PASS"
    assert third_payload["release_tail"]["status"] == "PASS"
    assert third_payload["release_tail"]["completed_stages"] == ["review", "verify", "qa", "ship_readiness"]
    assert third_payload["release_tail"]["blocked_stage"] is None
    assert third_payload["planning_artifact_mode"] == "contract_first"
    assert third_payload.get("task_graph_selection") == "all_tasks_complete" or third_payload["planning_snapshot"]["task_card_path"]

    fourth_rc, fourth_payload = _run_autopilot(args, capsys)
    assert fourth_rc == 0
    assert fourth_payload["status"] == "ok"
    assert fourth_payload["interaction_state"] == "PASS"
    assert fourth_payload["task_graph_selection"] == "all_tasks_complete"
    assert fourth_payload["release_tail"]["status"] == "PASS"


def test_autopilot_resumes_release_tail_before_bootstrap_when_chain_already_passed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = "release-resume-existing-chain"
    project_root = tmp_path
    prd_path = project_root / "PRD.md"
    _write_prd(prd_path)
    planning_dir = project_root / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".workflow_chain.json").write_text(
        json.dumps({"final_outcome": {"status": "PASS", "reason": "ALL_TASKS_COMPLETE"}}),
        encoding="utf-8",
    )
    decision_id = f"{feature}:release_approval"
    write_decision_request(
        planning_dir,
        build_decision_request(
            decision_id=decision_id,
            decision_kind=DecisionKind.RELEASE_APPROVAL,
            question="Approve release?",
            context_summary="chain passed",
            options=[{"option_id": "ship", "label": "Approve release"}],
            recommended_option="ship",
            blocking_reason="release approval required before ship-readiness",
        ),
    )
    write_decision_response(
        planning_dir,
        build_decision_response(
            decision_id=decision_id,
            selected_option="ship",
            rationale="resume release tail",
        ),
    )
    args = _autopilot_args(project_root=project_root, feature=feature, prd_path=prd_path)

    def _fail_bootstrap(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("autopilot should resume release tail before bootstrap")

    def _pass_release_tail(*, project_root: Path, planning_dir: Path, feature: str, **kwargs: object) -> dict[str, object]:
        return {
            "status": "PASS",
            "entrypoint": "kodawari autopilot",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "completed_stages": ["review", "verify", "qa", "ship_readiness"],
            "blocked_stage": None,
            "blocking_reason": "",
            "next_action": "",
            "stages": {
                "review": {"status": "PASS"},
                "verify": {"status": "PASS"},
                "qa": {"status": "PASS"},
                "ship_readiness": {"status": "PASS"},
            },
        }

    monkeypatch.setattr("kodawari.cli.runtime.autopilot_cmd.bootstrap_command_runtime", _fail_bootstrap)
    monkeypatch.setattr("kodawari.cli.runtime.autopilot_cmd.run_autopilot_release_tail", _pass_release_tail)

    rc, payload = _run_autopilot(args, capsys)

    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["interaction_state"] == "PASS"
    assert payload["task_graph_selection"] == "all_tasks_complete"
    assert payload["release_tail"]["status"] == "PASS"


def test_autopilot_main_entry_blocks_when_release_tail_blocks_after_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    feature = "autopilot-release-blocked"
    prd_path = tmp_path / "PRD.md"
    _write_prd(prd_path)
    args = _autopilot_args(project_root=tmp_path, feature=feature, prd_path=prd_path)

    def _blocked_release_tail(*, project_root: Path, planning_dir: Path, feature: str, **kwargs: object) -> dict[str, object]:
        return {
            "status": "BLOCKED",
            "entrypoint": "kodawari autopilot",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "completed_stages": ["review"],
            "blocked_stage": "verify",
            "blocking_reason": "verify fixtures missing for release tail",
            "next_action": "restore benchmark verify fixture",
            "stages": {
                "review": {"status": "PASS"},
                "verify": {
                    "status": "BLOCKED",
                    "blocking_reason": "verify fixtures missing for release tail",
                    "next_action": "restore benchmark verify fixture",
                },
            },
        }

    monkeypatch.setattr("kodawari.cli.runtime.autopilot_cmd.run_autopilot_release_tail", _blocked_release_tail)

    first_rc, first_payload = _run_autopilot(args, capsys)
    assert first_rc == 0
    planning_dir = Path(str(first_payload["planning_dir"]))
    _approve_decision(planning_dir, first_payload)

    second_rc, second_payload = _run_autopilot(args, capsys)
    assert second_rc == 0
    assert second_payload["decision_kind"] == "release_approval"
    _approve_decision(planning_dir, second_payload, selected_option="ship")

    third_rc, third_payload = _run_autopilot(args, capsys)
    assert third_rc == 1
    assert third_payload["status"] == "blocked"
    assert third_payload["interaction_state"] == "BLOCKED"
    assert third_payload["release_tail"]["status"] == "BLOCKED"
    assert third_payload["release_tail"]["blocked_stage"] == "verify"
    assert third_payload["blocking_reason"] == "verify fixtures missing for release tail"
    assert third_payload["next_action_type"] == "resolve_blocked"

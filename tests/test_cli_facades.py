import json
import argparse
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from kodawari.cli.main import build_parser, main
from kodawari.cli.runtime.work_all_runtime import _resume_skip


def test_parser_help_includes_v2_facade_commands() -> None:
    help_text = build_parser().format_help()
    assert "setup" in help_text
    assert "plan" in help_text
    assert "work" in help_text
    assert "release" in help_text
    assert "wf-setup" in help_text
    assert "wf-plan" in help_text
    assert "wf-work" in help_text
    assert "wf-work-all" in help_text
    assert "wf-review" in help_text
    assert "wf-release" in help_text

def test_canonical_and_wf_aliases_share_same_handlers() -> None:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    pairs = [
        ("setup", "wf-setup"),
        ("plan", "wf-plan"),
        ("work", "wf-work"),
        ("review", "wf-review"),
        ("release", "wf-release"),
        ("status", "wf-status"),
        ("work-all", "wf-work-all"),
    ]

    for canonical, alias in pairs:
        canonical_parser = subparsers.choices[canonical]
        alias_parser = subparsers.choices[alias]
        assert canonical_parser.get_default("handler") == alias_parser.get_default("handler")

def test_cli_plan_facade_writes_plans_markdown(tmp_path: Path, capsys: Any) -> None:
    feature = "facade-plan"
    prd_path = tmp_path / "prd.md"
    prd_path.write_text("# PRD\n\nBuild a facade plan.\n", encoding="utf-8")

    rc = main(["plan", "--project-root", str(tmp_path), "--feature", feature, "--prd", str(prd_path)])
    payload = json.loads(capsys.readouterr().out)
    planning_dir = tmp_path / "planning" / feature

    assert rc == 0
    assert payload["status"] == "PASS"
    assert (planning_dir / "Plans.md").exists()
    assert payload["artifacts"]["Plans.md"].endswith("Plans.md")


def test_cli_wf_plan_alias_routes_to_plan_facade(tmp_path: Path, capsys: Any) -> None:
    feature = "facade-wf-plan"
    prd_path = tmp_path / "prd.md"
    prd_path.write_text("# PRD\n\nBuild via wf-plan alias.\n", encoding="utf-8")

    rc = main(["wf-plan", "--project-root", str(tmp_path), "--feature", feature, "--prd", str(prd_path)])
    payload = json.loads(capsys.readouterr().out)
    planning_dir = tmp_path / "planning" / feature

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["entrypoint"] == "kodawari plan"
    assert (planning_dir / "Plans.md").exists()


def test_cli_plan_with_task_uses_model_planning_bridge(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    feature = "facade-plan-task"
    planning_dir = tmp_path / "planning" / feature
    captured: dict[str, Any] = {}
    task_graph_payload = {
        "schema_version": "contract_first.task_graph.v1",
        "generated_at": "2026-04-13T00:00:00Z",
        "business_outcome": "demo outcome",
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "Demo task",
                "layer_owner": "service",
                "surface": "rest_api",
                "core_files": ["app/main.py"],
                "invariants": ["keep api contract"],
                "test_proof": "pytest -q",
                "depends_on": [],
                "executability": {"status": "PASS", "issues": []},
            }
        ],
    }

    def _fake_ensure_contract_first_planning(**kwargs: Any) -> Any:
        captured.update(kwargs)
        planning_dir.mkdir(parents=True, exist_ok=True)
        (planning_dir / "TASK_GRAPH.json").write_text(json.dumps(task_graph_payload, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            artifacts={"TASK_GRAPH.json": str((planning_dir / "TASK_GRAPH.json").resolve())},
            planning_mode="existing",
            archetype="fastapi_api",
            capabilities=[],
            primary_task_id="T1",
            task_label="T1: Demo task",
            task_scope="app/main.py",
            steps_run=["planning_conversation"],
        )

    def _fake_load_contract_first_artifact(path: Path, schema_name: str | None = None) -> dict[str, Any]:
        del schema_name
        if path.name == "TASK_GRAPH.json":
            return dict(task_graph_payload)
        return {}

    monkeypatch.setattr("kodawari.cli.contract.plan_cmd.ensure_contract_first_planning", _fake_ensure_contract_first_planning)
    monkeypatch.setattr("kodawari.cli.contract.plan_cmd.load_contract_first_artifact", _fake_load_contract_first_artifact)

    rc = main(
        [
            "plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--task",
            "implement model-driven planning",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["status"] == "PASS"
    assert captured["use_model_planning"] is True
    assert captured["task_direction"] == "implement model-driven planning"
    assert (planning_dir / "Plans.md").exists()


def test_cli_plan_surfaces_model_planning_decision_gate(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    feature = "facade-plan-awaiting-decision"
    planning_dir = tmp_path / "planning" / feature
    task_graph_payload = {
        "schema_version": "contract_first.task_graph.v1",
        "generated_at": "2026-04-29T00:00:00Z",
        "business_outcome": "demo outcome",
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "Demo task",
                "layer_owner": "service",
                "surface": "rest_api",
                "core_files": ["app/main.py"],
                "invariants": ["keep api contract"],
                "test_proof": "pytest -q",
                "depends_on": [],
                "executability": {"status": "PASS", "issues": []},
            }
        ],
    }

    def _fake_ensure_contract_first_planning(**kwargs: Any) -> Any:
        del kwargs
        planning_dir.mkdir(parents=True, exist_ok=True)
        (planning_dir / "TASK_GRAPH.json").write_text(json.dumps(task_graph_payload, ensure_ascii=False), encoding="utf-8")
        return SimpleNamespace(
            artifacts={"TASK_GRAPH.json": str((planning_dir / "TASK_GRAPH.json").resolve())},
            planning_mode="existing",
            archetype="fastapi_api",
            capabilities=[],
            primary_task_id="T1",
            task_label="T1: Demo task",
            task_scope="app/main.py",
            steps_run=["planning_conversation", "task-plan", "task-prepare"],
            planning_status="escalation_required",
            planning_approval_decision="human_required",
            planning_approval_reason="score_checks_failed",
        )

    def _fake_load_contract_first_artifact(path: Path, schema_name: str | None = None) -> dict[str, Any]:
        del schema_name
        if path.name == "TASK_GRAPH.json":
            return dict(task_graph_payload)
        return {}

    monkeypatch.setattr("kodawari.cli.contract.plan_cmd.ensure_contract_first_planning", _fake_ensure_contract_first_planning)
    monkeypatch.setattr("kodawari.cli.contract.plan_cmd.load_contract_first_artifact", _fake_load_contract_first_artifact)

    rc = main(["plan", "--project-root", str(tmp_path), "--feature", feature, "--task", "implement"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["status"] == "awaiting_decision"
    assert payload["planning_status"] == "escalation_required"
    assert payload["planning_approval_decision"] == "human_required"
    assert payload["interaction_state"] == "AWAITING_DECISION"
    assert payload["decision_kind"] == "planning_escalation"
    assert (planning_dir / "Plans.md").exists()


def test_work_all_stops_after_plan_awaiting_decision() -> None:
    from kodawari.cli.runtime.work_all_runtime import _should_stop

    assert _should_stop("plan", {"status": "awaiting_decision"}, 0) is True


def test_work_all_normalizes_ok_work_step_and_honors_interaction_block() -> None:
    from kodawari.cli.runtime.work_all_runtime import _step_record

    passed = _step_record("work", 0, {"status": "ok", "interaction_state": "PASS"})
    blocked = _step_record("work", 0, {"status": "ok", "interaction_state": "BLOCKED"})

    assert passed["status"] == "PASS"
    assert blocked["status"] == "BLOCKED"


def test_cli_review_facade_runs_review_and_verify(tmp_path: Path, capsys: Any) -> None:
    feature = "facade-review"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".review_result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "feature": feature,
                "planning_dir": str(planning_dir),
                "changed_files": {"source": "manual", "items": ["app/main.py", "tests/test_api.py"], "count": 2},
                "checks": {},
                "execution_status": "PASS",
                "execution_source": ".execution_result.json",
                "execution_backend": "manual",
                "review_evidence_status": "PASS",
                "review_evidence_source": ".review_evidence.json",
                "summary": "review ok",
                "blocking_reason": "",
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".verify_report.json").write_text(
        json.dumps(
            {
                "schema_version": "verify.report.v1",
                "generated_at": "2026-03-29T00:00:00Z",
                "feature": feature,
                "planning_dir": str(planning_dir.resolve()),
                "status": "PASS",
                "entrypoint": "kodawari verify",
                "requested_command": "pytest -q",
                "requested_command_kind": "default",
                "changed_files": {"source": "manual", "items": ["app/main.py", "tests/test_api.py"], "count": 2},
                "input_confidence": "curated",
                "verify_check": {
                    "status": "PASS",
                    "passed": True,
                    "mode": "command",
                    "source": "verify_command",
                    "verify_cmd": "pytest -q",
                    "verify_cmd_resolved": "pytest -q",
                    "verify_target_source": "default",
                    "verify_targets": [],
                    "summary": "verify ok",
                    "blocking_reason": "",
                    "command_executed": False,
                },
            }
        ),
        encoding="utf-8",
    )

    rc = main(
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "app/main.py",
            "--changed-file",
            "tests/test_api.py",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["verify"]["status"] == "PASS"
    assert len(payload["steps"]) == 2


def test_cli_work_all_manifest_resumes_completed_plan_step(tmp_path: Path, capsys: Any) -> None:
    feature = "facade-work-all"
    prd_path = tmp_path / "prd.md"
    prd_path.write_text("# PRD\n\nResume work all.\n", encoding="utf-8")

    rc = main(
        [
            "work",
            "all",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--prd",
            str(prd_path),
            "--executor-backend",
            "noop_test_only",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    planning_dir = tmp_path / "planning" / feature
    manifest_path = planning_dir / ".work_all_manifest.json"

    assert manifest_path.exists()
    assert payload["entrypoint"] == "kodawari work all"
    assert payload["steps"]
    assert rc in {0, 2}

    rc = main(
        [
            "work",
            "all",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--prd",
            str(prd_path),
            "--executor-backend",
            "noop_test_only",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert any(step["name"] == "plan" and step["skipped"] for step in payload["steps"])
    assert rc in {0, 2}


def test_work_all_resume_skip_reuses_prior_resume_skip_record() -> None:
    manifest = {
        "steps": [
            {
                "name": "plan",
                "status": "SKIPPED",
                "skipped": True,
                "reason": "resume_skip",
                "summary": "already planned",
            }
        ]
    }

    assert _resume_skip("plan", manifest, force_rerun=False) == manifest["steps"][0]


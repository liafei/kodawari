from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kodawari.autopilot.planning.planning_orchestrator import PlanningResult
from kodawari.cli.contract.autopilot_contract_bridge import (
    AutopilotPlanningBridgeError,
    ensure_contract_first_planning,
)
from kodawari.cli.runtime.autopilot_release_flow import planning_decision_spec


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _task(task_id: str, file_path: str, *, depends_on: list[str] | None = None) -> dict:
    return {
        "task_id": task_id,
        "task_name": f"{task_id} task",
        "depends_on": list(depends_on or []),
        "core_files": [file_path],
        "layer_owner": "service",
        "invariants": ["keep behavior"],
        "test_proof": "pytest -q",
        "executability": {"status": "PASS", "issues": []},
    }


def _plan_task(task_id: str, file_path: str) -> dict:
    return {
        "task_id": task_id,
        "task_name": f"{task_id} task",
        "depends_on": [],
        "files_to_change": [file_path],
        "layer_owner": "service",
        "invariants": ["keep behavior"],
        "test_plan": "pytest -q",
    }


def _graph(*, planning_source: dict | None = None) -> dict:
    payload = {
        "schema_version": "contract_first.task_graph.v1",
        "generated_at": "2026-04-29T00:00:00+00:00",
        "tasks": [
            _task("T1", "src/a.py"),
            _task("T2", "src/b.py", depends_on=["T1"]),
        ],
        "executability": {"status": "PASS", "issues": []},
    }
    if planning_source is not None:
        payload["planning_source"] = planning_source
    return payload


def _prepare_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project_root = tmp_path / "project"
    (project_root / "src").mkdir(parents=True)
    (project_root / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (project_root / "src" / "b.py").write_text("B = 1\n", encoding="utf-8")
    prd = project_root / "PRD.md"
    prd.write_text("# Feature\n\nDo the thing.\n", encoding="utf-8")
    planning_dir = project_root / "planning" / "feature"
    _write_json(planning_dir / "TASK_GRAPH.json", _graph())
    _write_json(planning_dir / ".autopilot_state.json", {"completed_tasks": ["T1: first"]})
    return project_root, planning_dir, prd


def _model_plan_result(
    *,
    status: str = "approved",
    tasks: list[dict] | None = None,
    active_scope_decision: str = "",
) -> PlanningResult:
    approval = {"decision": "approved", "reason": "ok", "checks": {}}
    if active_scope_decision:
        approval["active_scope_view"] = {"decision": active_scope_decision}
    return PlanningResult(
        status=status,
        task_direction="Do the thing.",
        rounds=[],
        final_plan={
            "business_outcome": "Do the thing.",
            "tasks": tasks if tasks is not None else [_plan_task("T1", "src/a.py")],
        },
        final_review={},
        approval=approval,
        escalation=None,
        business_outcome="Do the thing.",
        out_of_scope=[],
        source_of_truth=[],
        source_of_truth_canonical=[],
        path_type="write",
        layers=["service"],
        coverage_hints=[],
        module_boundaries=[],
        verify_recipes=[],
        approval_points=[],
        execution_constraints={},
        confidence="high",
        confidence_issues=[],
        archetype="python",
        capabilities=[],
        input_fingerprint="sha256:test",
    )


def test_model_planning_reuses_existing_task_graph_for_next_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, planning_dir, prd = _prepare_project(tmp_path)

    def _fail_planner(*_args, **_kwargs):  # pragma: no cover - should not be called.
        raise AssertionError("planner should not run when TASK_GRAPH can be consumed")

    monkeypatch.setattr(
        "kodawari.cli.contract.autopilot_contract_bridge.run_planning_conversation",
        _fail_planner,
    )

    snapshot = ensure_contract_first_planning(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="feature",
        prd_path=prd,
        task_direction="Do the thing.",
        use_model_planning=True,
    )

    assert snapshot.primary_task_id == "T2"
    assert snapshot.stage_profile == "take_task"
    assert snapshot.selection_action == "take_task"
    assert snapshot.planning_source_status == "legacy_unknown"
    active = json.loads((planning_dir / "TASK_CARD_ACTIVE.json").read_text(encoding="utf-8"))
    assert active["task_id"] == "T2"
    telemetry_rows = [
        json.loads(line)
        for line in (planning_dir / ".planning_round_telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert telemetry_rows[-1]["schema_version"] == "planning.round_telemetry.v1"
    assert telemetry_rows[-1]["stage_profile"] == "take_task"
    assert telemetry_rows[-1]["selection_action"] == "take_task"
    assert telemetry_rows[-1]["tokens"] is None
    assert telemetry_rows[-1]["latency_seconds"] is None


def test_model_planning_blocks_stale_sourced_task_graph(tmp_path: Path) -> None:
    project_root, planning_dir, prd = _prepare_project(tmp_path)
    stale_source = {
        "schema_version": "planning.source.v1",
        "feature": "feature",
        "prd_path": str(prd),
        "prd_sha256": "old",
        "task_direction_sha256": hashlib.sha256("Do the thing.".encode("utf-8")).hexdigest(),
        "has_task_direction": True,
    }
    _write_json(planning_dir / "TASK_GRAPH.json", _graph(planning_source=stale_source))

    with pytest.raises(AutopilotPlanningBridgeError) as exc:
        ensure_contract_first_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature="feature",
            prd_path=prd,
            task_direction="Do the thing.",
            use_model_planning=True,
        )

    assert exc.value.error_code == "planning_graph_stale"


def test_next_task_reuse_does_not_block_on_old_epic_planning_decision(tmp_path: Path) -> None:
    project_root, planning_dir, prd = _prepare_project(tmp_path)
    _write_json(
        planning_dir / "PLANNING_CONVERSATION.json",
        {
            "schema_version": "planning.conversation.v1",
            "input_fingerprint": "sha256:old",
            "task_direction": "Do the thing.",
            "status": "escalation_required",
            "final_plan": {"tasks": [{"task_id": "T1"}]},
            "approval": {
                "decision": "human_required",
                "reason": "old epic review was unresolved",
                "checks": {},
            },
        },
    )

    snapshot = ensure_contract_first_planning(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="feature",
        prd_path=prd,
        task_direction="Do the thing.",
        use_model_planning=True,
    )

    assert snapshot.stage_profile == "take_task"
    assert snapshot.planning_status == ""
    assert snapshot.planning_approval_decision == ""
    assert planning_decision_spec(feature="feature", planning_snapshot=snapshot) is None


def test_existing_conversation_without_task_graph_generates_graph_and_card(tmp_path: Path) -> None:
    project_root, planning_dir, _prd = _prepare_project(tmp_path)
    (planning_dir / "TASK_GRAPH.json").unlink()
    (planning_dir / ".autopilot_state.json").unlink()
    _write_json(
        planning_dir / "PLANNING_CONVERSATION.json",
        {
            "schema_version": "planning.conversation.v1",
            "input_fingerprint": "sha256:old",
            "task_direction": "Do the thing.",
            "status": "approved",
            "final_plan": {"business_outcome": "Do the thing.", "tasks": [_plan_task("T1", "src/a.py")]},
            "approval": {"decision": "approved", "reason": "ok", "checks": {}},
        },
    )

    snapshot = ensure_contract_first_planning(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="feature",
        prd_path=None,
        task_direction="",
        use_model_planning=True,
    )

    assert snapshot.primary_task_id == "T1"
    assert snapshot.planning_source_status == "legacy_conversation"
    assert (planning_dir / "TASK_GRAPH.json").exists()
    assert (planning_dir / "TASK_CARD_ACTIVE.json").exists()
    assert "task-plan" in snapshot.steps_run


def test_existing_conversation_without_tasks_keeps_error_code(tmp_path: Path) -> None:
    project_root, planning_dir, _prd = _prepare_project(tmp_path)
    (planning_dir / "TASK_GRAPH.json").unlink()
    (planning_dir / ".autopilot_state.json").unlink()
    _write_json(
        planning_dir / "PLANNING_CONVERSATION.json",
        {
            "schema_version": "planning.conversation.v1",
            "input_fingerprint": "sha256:old",
            "task_direction": "Do the thing.",
            "status": "approved",
            "final_plan": {"tasks": []},
            "approval": {"decision": "approved", "reason": "ok", "checks": {}},
        },
    )

    with pytest.raises(AutopilotPlanningBridgeError) as exc:
        ensure_contract_first_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature="feature",
            prd_path=None,
            task_direction="",
            use_model_planning=True,
        )

    assert exc.value.error_code == "planning_conversation_invalid"


def test_fresh_model_planning_uses_facade_monkeypatch_and_writes_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, planning_dir, _prd = _prepare_project(tmp_path)
    (planning_dir / "TASK_GRAPH.json").unlink()
    (planning_dir / ".autopilot_state.json").unlink()
    calls: list[str] = []

    def _fake_planner(*_args, **_kwargs):
        calls.append("planner")
        return _model_plan_result(active_scope_decision="auto_approve_active_scope")

    monkeypatch.setattr(
        "kodawari.cli.contract.autopilot_contract_bridge.run_planning_conversation",
        _fake_planner,
    )

    snapshot = ensure_contract_first_planning(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="feature",
        prd_path=None,
        task_direction="Do the thing.",
        use_model_planning=True,
        force_replan=True,
    )

    assert calls == ["planner"]
    assert snapshot.stage_profile == "epic_plan"
    assert snapshot.planning_source_status == "fresh"
    assert snapshot.planning_status == "approved"
    assert snapshot.planning_approval_active_scope_decision == "auto_approve_active_scope"
    assert (planning_dir / "PLANNING_CONVERSATION.json").exists()
    assert (planning_dir / "TASK_GRAPH.json").exists()
    assert (planning_dir / "TASK_CARD_ACTIVE.json").exists()


def test_fresh_model_planning_empty_tasks_precedes_error_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, planning_dir, _prd = _prepare_project(tmp_path)
    (planning_dir / "TASK_GRAPH.json").unlink()
    (planning_dir / ".autopilot_state.json").unlink()
    monkeypatch.setattr(
        "kodawari.cli.contract.autopilot_contract_bridge.run_planning_conversation",
        lambda *_args, **_kwargs: _model_plan_result(status="error", tasks=[]),
    )

    with pytest.raises(AutopilotPlanningBridgeError) as exc:
        ensure_contract_first_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature="feature",
            prd_path=None,
            task_direction="Do the thing.",
            use_model_planning=True,
            force_replan=True,
        )

    assert exc.value.error_code == "planning_conversation_invalid"


def test_fresh_model_planning_escalation_does_not_write_executable_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, planning_dir, _prd = _prepare_project(tmp_path)
    (planning_dir / "TASK_GRAPH.json").unlink()
    (planning_dir / ".autopilot_state.json").unlink()
    monkeypatch.setattr(
        "kodawari.cli.contract.autopilot_contract_bridge.run_planning_conversation",
        lambda *_args, **_kwargs: _model_plan_result(status="escalation_required"),
    )

    with pytest.raises(AutopilotPlanningBridgeError) as exc:
        ensure_contract_first_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature="feature",
            prd_path=None,
            task_direction="Do the thing.",
            use_model_planning=True,
            force_replan=True,
        )

    assert exc.value.error_code == "planning_escalation_required"
    assert exc.value.details["planning_status"] == "escalation_required"
    assert (planning_dir / "PLANNING_CONVERSATION.json").exists()
    assert not (planning_dir / "TASK_GRAPH.json").exists()
    assert not (planning_dir / "TASK_CARD_ACTIVE.json").exists()


def test_fresh_model_planning_graph_validation_error_keeps_contract_error_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, planning_dir, _prd = _prepare_project(tmp_path)
    (planning_dir / "TASK_GRAPH.json").unlink()
    monkeypatch.setattr(
        "kodawari.cli.contract.autopilot_contract_bridge.run_planning_conversation",
        lambda *_args, **_kwargs: _model_plan_result(),
    )
    monkeypatch.setattr(
        "kodawari.cli.contract.model_bootstrap.validate_task_graph",
        lambda _payload: ["graph broken"],
    )

    with pytest.raises(AutopilotPlanningBridgeError) as exc:
        ensure_contract_first_planning(
            project_root=project_root,
            planning_dir=planning_dir,
            feature="feature",
            prd_path=None,
            task_direction="Do the thing.",
            use_model_planning=True,
            force_replan=True,
        )

    assert exc.value.error_code == "task_graph_invalid"
    assert exc.value.details["validation_errors"] == ["graph broken"]


def test_next_task_refreshes_stale_active_card_with_graph_guidance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root, planning_dir, prd = _prepare_project(tmp_path)
    graph = json.loads((planning_dir / "TASK_GRAPH.json").read_text(encoding="utf-8"))
    graph["tasks"][1]["coverage_hints"] = ["assert the session-admin bearer is rejected"]
    graph["tasks"][1]["api_contracts"] = [
        {"method": "GET", "endpoint": "/admin/example", "response_shape": "401 on missing token"}
    ]
    _write_json(planning_dir / "TASK_GRAPH.json", graph)
    _write_json(
        planning_dir / "TASK_CARD_ACTIVE.json",
        {
            "schema_version": "contract_first.task_card.v1",
            "task_id": "T2",
            "task_name": "old T2 task",
            "why_this_layer": "old card",
            "files_to_change": ["src/b.py"],
            "new_files": [],
            "invariants": ["keep behavior"],
            "test_plan": "pytest -q",
            "forbidden_changes": [],
            "requires": [],
        },
    )

    def _fail_planner(*_args, **_kwargs):  # pragma: no cover - should not be called.
        raise AssertionError("planner should not run when TASK_GRAPH can be consumed")

    monkeypatch.setattr(
        "kodawari.cli.contract.autopilot_contract_bridge.run_planning_conversation",
        _fail_planner,
    )

    snapshot = ensure_contract_first_planning(
        project_root=project_root,
        planning_dir=planning_dir,
        feature="feature",
        prd_path=prd,
        task_direction="Do the thing.",
        use_model_planning=True,
    )

    assert snapshot.primary_task_id == "T2"
    active = json.loads((planning_dir / "TASK_CARD_ACTIVE.json").read_text(encoding="utf-8"))
    assert active["coverage_hints"] == ["assert the session-admin bearer is rejected"]
    assert active["api_contracts"][0]["endpoint"] == "/admin/example"

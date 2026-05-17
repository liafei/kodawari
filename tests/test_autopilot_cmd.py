"""Tests for autopilot CLI command stateful simulation."""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from kodawari.autopilot.engine.engine import AutopilotConfig, AutopilotEngine
from kodawari.cli.runtime.autopilot_cmd import run_autopilot_command
from kodawari.cli.runtime.autopilot_changed_files import resolve_reliable_changed_files
from kodawari.cli.runtime.autopilot_runtime_flow import (
    _claim_is_active,
    _clear_task_claim,
    _mark_primary_task_complete_if_pass,
    build_engine_config,
    ensure_planning_contract_artifacts,
)
from kodawari.cli.runtime.autopilot_release_flow import autopilot_interaction_snapshot
from kodawari.cli.runtime.autopilot_decision_runtime import (
    build_decision_response,
    write_decision_response,
)
from kodawari.cli.runtime.autopilot_workflow_runtime import build_workflow_chain_runtime
from kodawari.cli.delivery.workflow_chain import (
    build_final_outcome,
    build_final_quality_review,
    build_task_entry_result,
    build_task_cycle_result,
    build_upstream_result,
)
from kodawari.instincts import learn_from_globs


_STALE_FAILED_SUBTASK = {
    "subtask_id": "T001.1",
    "title": "legacy failed subtask",
    "parent_task_id": "T001",
    "status": "FAILED",
    "depends_on": [],
    "changed_files": [],
    "tokens_used": 0,
    "duration_seconds": 0.0,
    "verify_cmd": "pytest -q",
    "verify_status": "BLOCKED",
    "verify_output": "legacy failure",
    "error": "legacy failure",
    "attempt": 1,
    "started_at": None,
    "completed_at": None,
}


def _stale_state_payload(*, project_root: Path, feature: str) -> dict[str, object]:
    return {
        "schema_version": "autopilot.state.v2",
        "revision": 1,
        "feature": feature,
        "project_root": str(project_root),
        "current_stage": "COMPLETED",
        "cycle": 99,
        "tokens_used": 1200,
        "error_history": ["legacy failure"],
        "last_error": "legacy failure",
        "changed_files": [],
        "completed_tasks": [],
        "task_timings": {},
        "active_task": None,
        "active_pid": None,
        "active_attempt": None,
        "stage_started_at": None,
        "heartbeat_at": None,
        "last_stage_status": "blocked",
        "warning_noise_events": 0,
        "warning_noise_degraded_events": 0,
        "warning_noise_by_task": {},
        "verify_setup_recovery_attempted": 0,
        "verify_setup_recovery_succeeded": 0,
        "verify_setup_recovery_last_error": None,
        "subtasks": {"T001.1": dict(_STALE_FAILED_SUBTASK)},
        "active_subtask": "T001.1",
        "architecture_decisions": [],
        "started_at": None,
        "updated_at": "2026-03-19T00:00:00+00:00",
        "stop_reason": "MAX_CYCLES",
        "final_status": "BLOCKED",
    }


def _write_stale_autopilot_state(planning_dir: Path, *, project_root: Path, feature: str) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps(_stale_state_payload(project_root=project_root, feature=feature)),
        encoding="utf-8",
    )


class _TaskCyclePassAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["app/main.py", "tests/test_main.py"],
        }


class _RecordingTaskCardAdapter:
    def __init__(self) -> None:
        self.contexts: list[dict[str, object]] = []

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        task_card = dict(context.get("task_card") or {})
        task_card_files = [str(item) for item in list(context.get("task_card_files") or []) if str(item).strip()]
        self.contexts.append(
            {
                "task": task,
                "task_id": str(task_card.get("task_id") or ""),
                "task_card_files": task_card_files,
            }
        )
        return {
            "status": "done",
            "changes": task_card_files,
        }


def _task_cycle_args(tmp_path: Path, *, feature: str) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(tmp_path),
        feature=feature,
        tier="heavy",
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=True,
        enable_peer_review=False,
        task_label=None,
        task_scope=None,
    )


def _assert_stale_reset_payload(payload: dict[str, object]) -> None:
    assert payload["status"] == "ok"
    assert payload["run_reason"] == "PROCEED_TO_GATE"
    workflow_chain = payload["workflow_chain"]
    assert workflow_chain["upstream"]["status"] == "PASS"
    assert workflow_chain["upstream"]["stop_reason"] == "PASS"
    assert workflow_chain["final_outcome"]["status"] == "PASS"


def _write_contract_first_task_bundle(planning_dir: Path) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    task_specs = [
        ("T1", "Prepare schema contract", [], ["src/prepare_schema.py", "tests/test_prepare_schema.py"]),
        ("T3", "Publish API docs", ["T2"], ["src/publish_docs.py", "tests/test_publish_docs.py"]),
        ("T2", "Add integration tests", ["T1"], ["src/add_integration.py", "tests/test_add_integration.py"]),
    ]
    task_graph = {
        "schema_version": "contract_first.task_graph.v1",
        "generated_at": "2026-04-22T00:00:00+00:00",
        "business_outcome": "Ship contract-first backlog to task_cycle",
        "coverage_hints": ["path:read", "layer:service", "sot:db.items"],
        "boundary_debt": {
            "status": "PASS",
            "details": "Planner output already splits files cleanly.",
            "items": [],
        },
        "executability": {"status": "PASS", "issues": []},
        "tasks": [
            {
                "task_id": task_id,
                "task_name": task_name,
                "depends_on": depends_on,
                "layer_owner": "service",
                "core_files": core_files,
                "invariants": ["single source of truth"],
                "test_proof": "pytest -q",
                "coverage_hints": ["layer:service", "path:read", "sot:db.items"],
                "executability": {"status": "PASS", "issues": []},
            }
            for task_id, task_name, depends_on, core_files in task_specs
        ],
    }
    (planning_dir / "TASK_GRAPH.json").write_text(json.dumps(task_graph), encoding="utf-8")
    for task_id, task_name, _depends_on, files_to_change in task_specs:
        card = {
            "schema_version": "contract_first.task_card.v1",
            "generated_at": "2026-04-22T00:00:00+00:00",
            "task_id": task_id,
            "task_name": task_name,
            "why_this_layer": "Task cycle should honor per-task contract scope.",
            "files_to_change": files_to_change,
            "invariants": ["scope only"],
            "test_plan": "pytest -q",
            "forbidden_changes": ["Do not refactor unrelated modules."],
        }
        (planning_dir / f"TASK_CARD_{task_id}.json").write_text(json.dumps(card), encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        (planning_dir / "TASK_CARD_T1.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


def test_ensure_planning_contract_artifacts_syncs_tasks_from_contract_first_graph(tmp_path: Path) -> None:
    feature = "contract-first-backlog-sync"
    planning_dir = tmp_path / "planning" / feature
    _write_contract_first_task_bundle(planning_dir)
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [x] T1: Prepare schema contract",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    ensure_planning_contract_artifacts(
        planning_dir=planning_dir,
        feature=feature,
        task_label="T1: Prepare schema contract",
        requirements_text="Contract-first backlog sync",
    )

    tasks_text = (planning_dir / "TASKS.md").read_text(encoding="utf-8")
    assert "- [x] T1: Prepare schema contract" in tasks_text
    assert "- [ ] T2: Add integration tests" in tasks_text
    assert "- [ ] T3: Publish API docs" in tasks_text
    assert tasks_text.index("T2: Add integration tests") < tasks_text.index("T3: Publish API docs")


def test_task_cycle_uses_contract_first_graph_backlog_and_task_cards(tmp_path: Path) -> None:
    feature = "contract-first-task-cycle-demo"
    planning_dir = tmp_path / "planning" / feature
    _write_contract_first_task_bundle(planning_dir)
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [ ] T1: Prepare schema contract",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for relative_path in (
        "src/prepare_schema.py",
        "tests/test_prepare_schema.py",
        "src/add_integration.py",
        "tests/test_add_integration.py",
        "src/publish_docs.py",
        "tests/test_publish_docs.py",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if "tests/" in relative_path:
            path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
        else:
            path.write_text("VALUE = 1\n", encoding="utf-8")

    adapter = _RecordingTaskCardAdapter()
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature=feature, max_cycles=1),
        adapter=adapter,
    )
    args = argparse.Namespace(task_cycle=True, enable_peer_review=False)
    upstream_payload = {
        "reason": "PROCEED_TO_GATE",
        "unified_status": {"stop_reason": "PASS", "is_blocked": False},
        "loop_outcome": {"stop_reason": "PASS", "blocked": False, "round_outcome": "ready_for_gate"},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS", "blocking_violations": 0},
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 0, "approved_count": 0},
    }

    workflow_chain, _ = build_workflow_chain_runtime(
        args=args,
        engine=engine,
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature=feature,
        upstream_task_label="T1: Prepare schema contract",
        upstream_payload=upstream_payload,
    )

    assert workflow_chain is not None
    assert workflow_chain["task_cycle"]["tasks_total"] == 2
    assert workflow_chain["task_cycle"]["tasks_completed"] == 2
    assert [task["task_id"] for task in workflow_chain["task_cycle"]["tasks"]] == ["T2", "T3"]
    assert [item["task_id"] for item in adapter.contexts] == ["T2", "T3"]
    assert adapter.contexts[0]["task_card_files"] == ["src/add_integration.py", "tests/test_add_integration.py"]
    assert adapter.contexts[1]["task_card_files"] == ["src/publish_docs.py", "tests/test_publish_docs.py"]
    active_card = json.loads((planning_dir / "TASK_CARD_ACTIVE.json").read_text(encoding="utf-8"))
    assert active_card["task_id"] == "T3"
    assert workflow_chain["task_cycle"]["entered"] is True


def test_task_cycle_skips_state_completed_contract_first_tasks(tmp_path: Path) -> None:
    feature = "contract-first-skip-state-completed"
    planning_dir = tmp_path / "planning" / feature
    _write_contract_first_task_bundle(planning_dir)
    state_payload = _stale_state_payload(project_root=tmp_path, feature=feature)
    state_payload["completed_tasks"] = ["T1: Prepare schema contract"]
    (planning_dir / ".autopilot_state.json").write_text(json.dumps(state_payload), encoding="utf-8")
    for relative_path in (
        "src/add_integration.py",
        "tests/test_add_integration.py",
        "src/publish_docs.py",
        "tests/test_publish_docs.py",
    ):
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    adapter = _RecordingTaskCardAdapter()
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature=feature, max_cycles=1),
        adapter=adapter,
    )
    args = argparse.Namespace(task_cycle=True, enable_peer_review=False)
    upstream_payload = {
        "reason": "PROCEED_TO_GATE",
        "unified_status": {"stop_reason": "PASS", "is_blocked": False},
        "loop_outcome": {"stop_reason": "PASS", "blocked": False, "round_outcome": "ready_for_gate"},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS", "blocking_violations": 0},
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 0, "approved_count": 0},
    }

    workflow_chain, _ = build_workflow_chain_runtime(
        args=args,
        engine=engine,
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature=feature,
        upstream_task_label="T2: Add integration tests",
        upstream_payload=upstream_payload,
    )

    assert workflow_chain is not None
    assert [task["task_id"] for task in workflow_chain["task_cycle"]["tasks"]] == ["T3"]
    assert [item["task_id"] for item in adapter.contexts] == ["T3"]


def test_autopilot_cmd_runs_stateful_simulation(tmp_path: Path, capsys) -> None:
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("Build ranking API with stable scoring\n", encoding="utf-8")

    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature="demo",
        tier="heavy",
        requirements_file=str(requirements_file),
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
    )

    rc = run_autopilot_command(args)
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    planning_dir = Path(payload["planning_dir"])
    state_path = Path(payload["state_path"])
    rounds_path = Path(payload["rounds_path"])

    assert planning_dir.exists()
    assert state_path.exists()
    assert rounds_path.exists()
    assert payload["run_reason"] == "PROCEED_TO_GATE"
    assert payload["planning_contract_version"] == "ws115.v1"
    assert payload["changed_files_source"].startswith(
        (
            "task_delta_changed_files",
            "execution_result_changed_files",
            "runtime_changed_files",
            "state_changed_files",
            "subtask_changed_files",
            "none",
        )
    )
    assert payload["planning_contract"]["version"] == "ws115.v1"
    assert payload["planning_contract"]["complete"] is False
    assert payload["planning_contract"]["required_artifacts"] == [
        "PLAN.md",
        "TASKS.md",
        "ACCEPTANCE.md",
        "GATE.md",
    ]
    assert payload["planning_artifacts"]["PLAN.md"]["exists"] is True
    assert payload["planning_artifacts"]["TASKS.md"]["exists"] is True
    assert payload["planning_artifacts"]["ACCEPTANCE.md"]["exists"] is True
    assert payload["planning_artifacts"]["GATE.md"]["exists"] is False
    assert payload["planning_artifacts"][".worktree_baseline.json"]["exists"] is True
    assert payload["workflow_chain"]["task_cycle_enabled"] is True
    assert payload["workflow_chain"]["final_outcome"]["status"] == "PASS"
    assert payload["worktree_preflight"]["mode"] == "warn"

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["current_stage"] == "GATE"
    assert state_payload["changed_files"] == sorted(state_payload["changed_files"])
    assert state_payload["active_task"] == "T001: Implement feature demo"
    assert payload["unified_status"]["current_phase"] == "GATE"


def test_build_engine_config_preserves_rollback_retry_settings(tmp_path: Path) -> None:
    args = argparse.Namespace(
        feature="demo",
        tier="heavy",
        task="",
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        rollback_on_failure=True,
        max_verify_retries=7,
    )

    config = build_engine_config(
        args=args,
        project_root=tmp_path,
        requirements_file=None,
        task_card_path=None,
        config_cls=AutopilotConfig,
    )

    assert config.rollback_on_failure is True
    assert config.max_verify_retries == 7


def test_autopilot_cmd_can_materialize_workflow_chain_snapshot(tmp_path: Path, capsys) -> None:
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("Fast workflow chain\n", encoding="utf-8")

    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature="chain-demo",
        tier="heavy",
        requirements_file=str(requirements_file),
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=True,
        enable_peer_review=False,
        task_label=None,
        task_scope=None,
    )

    rc = run_autopilot_command(args)
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    planning_dir = Path(payload["planning_dir"])
    chain_path = planning_dir / ".workflow_chain.json"
    state_path = Path(payload["state_path"])

    assert chain_path.exists()
    assert payload["workflow_chain"]["mode"] == "single_pass"
    assert payload["workflow_chain"]["peer_review_enabled"] is False
    assert payload["workflow_chain"]["upstream"]["verify"]["status"] == "PASS"
    assert payload["workflow_chain"]["upstream"]["gate"]["total_status"] == "PASS"
    assert payload["workflow_chain"]["upstream"]["approvals"]["all_passed"] is True
    assert payload["workflow_chain"]["upstream"]["peer_review_runtime"]["mode"] == ""
    assert payload["workflow_chain"]["upstream"]["peer_review_runtime"]["source"] == ""
    assert payload["workflow_chain"]["upstream"]["peer_review_runtime"]["real_requested"] is False
    assert payload["workflow_chain"]["task_cycle"]["entered"] is True
    task_cycle_tasks = payload["workflow_chain"]["task_cycle"]["tasks"]
    assert all(item["blocking_reason"] == "" for item in task_cycle_tasks)
    assert payload["workflow_chain"]["final_quality_review"]["phase"] == "FINAL_QUALITY_REVIEW"
    assert payload["workflow_chain"]["final_quality_review"]["phase_status"] == "passed"
    assert payload["workflow_chain"]["chain_final_outcome"]["status"] == "PASS"
    assert payload["workflow_chain"]["final_outcome"]["status"] == "PASS"
    assert payload["workflow_chain"]["final_outcome"]["reason"] in {"ALL_TASKS_COMPLETE", "NO_TASKS_FOUND"}
    assert payload["planning_artifacts"][".workflow_chain.json"]["exists"] is True

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    task_cycle_subtasks = [item for key, item in dict(state_payload.get("subtasks") or {}).items() if key.endswith(".TASK_CYCLE")]
    if task_cycle_tasks:
        assert task_cycle_subtasks
        assert all(item["status"] in {"DONE", "FAILED"} for item in task_cycle_subtasks)


def test_autopilot_cmd_task_cycle_skips_completed_checklist_items(tmp_path: Path, capsys) -> None:
    feature = "skip-completed-demo"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [x] T001: Already completed",
                "- [ ] T002: Execute now",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature=feature,
        tier="heavy",
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=True,
        enable_peer_review=False,
        task_label=None,
        task_scope=None,
    )

    rc = run_autopilot_command(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    task_cycle = payload["workflow_chain"]["task_cycle"]
    assert task_cycle["tasks_total"] == 1
    assert task_cycle["tasks"][0]["task_id"] == "T002"


def test_autopilot_cmd_task_cycle_accepts_implicit_backlog_when_no_explicit_ids(
    tmp_path: Path,
    capsys,
) -> None:
    feature = "implicit-backlog-demo"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [ ] Prepare migration scripts",
                "- [x] Already completed item",
                "- [ ] Add API tests",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature=feature,
        tier="heavy",
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=True,
        enable_peer_review=False,
        task_label=None,
        task_scope=None,
    )

    rc = run_autopilot_command(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    task_cycle = payload["workflow_chain"]["task_cycle"]
    assert task_cycle["tasks_total"] == 2
    assert task_cycle["tasks_completed"] == 2
    assert [task["task_id"] for task in task_cycle["tasks"]] == ["TASK001", "TASK003"]


def test_workflow_chain_task_block_propagates_blocking_reason() -> None:
    tasks = [{"task_id": "T001", "label": "T001: Fix verify", "scope": "Fix verify"}]
    task_results = [
        {
            "task_id": "T001",
            "task_label": "T001: Fix verify",
            "task_scope": "Fix verify",
            "peer_review_enabled": True,
            "autopilot_status": "BLOCKED",
            "autopilot_reason": "VERIFY_BLOCKED",
            "verify": {"status": "BLOCKED"},
            "gate": {"total_status": "PASS"},
            "outcome": "BLOCKED",
            "blocking_reason": "fixture missing during scoped verify",
        }
    ]
    upstream = {"passed": True}

    task_cycle = build_task_cycle_result(upstream_passed=True, tasks=tasks, task_results=task_results)
    final_review = build_final_quality_review(upstream=upstream, task_cycle=task_cycle)
    final_outcome = build_final_outcome(
        peer_review_enabled=True,
        upstream=upstream,
        task_cycle=task_cycle,
        final_review=final_review,
    )

    assert task_cycle["blocked_reason"] == "fixture missing during scoped verify"
    assert final_review["blocking_reason"] == "fixture missing during scoped verify"
    assert final_outcome["blocking_reason"] == "fixture missing during scoped verify"


def test_workflow_chain_task_entry_prefers_executor_blocking_reason_over_verify_fallback() -> None:
    task = {"task_id": "T008", "label": "T008: Executor stall", "scope": "Executor stall"}
    autopilot_payload = {
        "reason": "EXECUTOR_RECOVERY_REQUIRED",
        "last_error": "executor made no write progress",
        "loop_outcome": {
            "stop_reason": "STUCK",
            "blocked": True,
            "blocking_reason": "executor made no write progress",
        },
        "unified_status": {"stop_reason": "STUCK", "is_blocked": True},
        "peer_review_summary": {"approved": False},
    }

    task_result = build_task_entry_result(
        task=task,
        autopilot_payload=autopilot_payload,
        peer_review_enabled=False,
        gate_payload=None,
    )

    assert task_result["outcome"] == "BLOCKED"
    assert task_result["blocking_reason"] == "executor made no write progress"


def test_workflow_chain_task_entry_exposes_stop_reason_and_verify_targeting() -> None:
    task = {"task_id": "T001", "label": "T001: Verify targeting", "scope": "Verify targeting"}
    autopilot_payload = {
        "reason": "PROCEED_TO_GATE",
        "unified_status": {"stop_reason": "PASS", "is_blocked": False},
        "loop_outcome": {"stop_reason": "PASS", "blocked": False, "round_outcome": "ready_for_gate"},
        "verify_check": {
            "status": "PASS",
            "mode": "command",
            "source": "verify_command",
            "verify_cmd": "pytest -q",
            "verify_cmd_resolved": "pytest -q tests/test_scope.py",
            "verify_target_source": "changed_test_files",
            "verify_targets": ["tests/test_scope.py"],
            "artifacts": ["src/app.py", "tests/test_scope.py"],
            "summary": "ok",
            "blocking_reason": "",
            "command_executed": True,
            "returncode": 0,
        },
        "gate_check": {
            "total_status": "PASS",
            "blocking_violations": 0,
            "total_violations": 0,
            "profile": {"name": "blocking"},
        },
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 1, "approved_count": 1},
    }

    task_result = build_task_entry_result(
        task=task,
        autopilot_payload=autopilot_payload,
        peer_review_enabled=True,
        gate_payload=None,
    )

    assert task_result["outcome"] == "PASS"
    assert task_result["stop_reason"] == "PASS"
    assert task_result["blocked"] is False
    assert task_result["round_outcome"] == "ready_for_gate"
    assert task_result["verify"]["verify_cmd_resolved"] == "pytest -q tests/test_scope.py"
    assert task_result["verify"]["verify_target_source"] == "changed_test_files"
    assert task_result["verify"]["verify_targets"] == ["tests/test_scope.py"]
    assert task_result["approvals"]["all_passed"] is True


def test_workflow_chain_task_entry_exposes_peer_review_runtime_semantics() -> None:
    task = {"task_id": "T009", "label": "T009: Runtime semantics", "scope": "Runtime semantics"}
    autopilot_payload = {
        "reason": "PROCEED_TO_GATE",
        "runtime_semantics": {
            "peer_review": {
                "mode": "real_opus_gateway",
                "source": "kodawari.real_opus_gateway",
                "real_requested": True,
                "real_required": True,
                "fallback_used": False,
                "error": "",
            }
        },
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 1, "approved_count": 1},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS"},
    }
    task_result = build_task_entry_result(
        task=task,
        autopilot_payload=autopilot_payload,
        peer_review_enabled=True,
        gate_payload=None,
    )
    assert task_result["peer_review_runtime"]["mode"] == "real_opus_gateway"
    assert task_result["peer_review_runtime"]["source"] == "kodawari.real_opus_gateway"
    assert task_result["peer_review_runtime"]["real_requested"] is True
    assert task_result["peer_review_runtime"]["real_required"] is True


def test_workflow_chain_task_entry_blocks_when_self_review_missing_under_peer_review() -> None:
    task = {"task_id": "T002", "label": "T002: Missing self review", "scope": "Missing self review"}
    autopilot_payload = {
        "reason": "PROCEED_TO_GATE",
        "unified_status": {"stop_reason": "PASS", "is_blocked": False},
        "loop_outcome": {"stop_reason": "PASS", "blocked": False, "round_outcome": "ready_for_gate"},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS", "blocking_violations": 0},
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 0, "approved_count": 0},
    }

    task_result = build_task_entry_result(
        task=task,
        autopilot_payload=autopilot_payload,
        peer_review_enabled=True,
        gate_payload=None,
    )

    assert task_result["outcome"] == "BLOCKED"
    assert task_result["approvals"]["peer_review"] is True
    assert task_result["approvals"]["self_review"] is False
    assert task_result["blocking_reason"] == "Self review not approved"


def test_workflow_chain_upstream_allows_backend_without_self_review_contract() -> None:
    payload = {
        "reason": "PROCEED_TO_GATE",
        "execution_backend": "claude_code",
        "collaboration_context": {"self_review_required": False},
        "unified_status": {"stop_reason": "PASS", "is_blocked": False},
        "loop_outcome": {"stop_reason": "PASS", "blocked": False, "round_outcome": "ready_for_gate"},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS", "blocking_violations": 0},
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 0, "approved_count": 0},
    }

    upstream = build_upstream_result(
        task_label="T003: Claimed schema contract is ready",
        peer_review_enabled=True,
        payload=payload,
    )

    assert upstream["passed"] is True
    assert upstream["status"] == "PASS"
    assert upstream["approvals"]["peer_review"] is True
    assert upstream["approvals"]["self_review"] is True
    assert upstream["approvals"]["all_passed"] is True


def test_task_cycle_resets_cycle_budget_per_backlog_item(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "demo-feature"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                "# TASKS",
                "",
                "- [x] T1: Prepare schema contract",
                "- [ ] T2: Add integration tests",
                "- [ ] T3: Update API docs",
            ]
        ),
        encoding="utf-8",
    )
    app_file = tmp_path / "app" / "main.py"
    test_file = tmp_path / "tests" / "test_main.py"
    app_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    app_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    test_file.write_text("def test_handler():\n    assert True\n", encoding="utf-8")

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="demo-feature", max_cycles=1),
        adapter=_TaskCyclePassAdapter(),
    )
    args = argparse.Namespace(task_cycle=True, enable_peer_review=False)
    upstream_payload = {
        "reason": "PROCEED_TO_GATE",
        "unified_status": {"stop_reason": "PASS", "is_blocked": False},
        "loop_outcome": {"stop_reason": "PASS", "blocked": False, "round_outcome": "ready_for_gate"},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS", "blocking_violations": 0},
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 0, "approved_count": 0},
    }

    workflow_chain, _ = build_workflow_chain_runtime(
        args=args,
        engine=engine,
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature="demo-feature",
        upstream_task_label="T1: Prepare schema contract",
        upstream_payload=upstream_payload,
    )

    assert workflow_chain is not None
    assert workflow_chain["task_cycle"]["entered"] is True
    assert workflow_chain["task_cycle"]["tasks_completed"] == 2
    assert workflow_chain["task_cycle"]["blocked"] is False
    assert workflow_chain["final_outcome"]["status"] == "PASS"


def test_workflow_chain_upstream_result_uses_unified_stop_reason_when_loop_outcome_missing() -> None:
    payload = {
        "reason": "MAX_CYCLES_REACHED",
        "unified_status": {"stop_reason": "MAX_CYCLES", "is_blocked": True},
        "verify_check": {
            "status": "BLOCKED",
            "mode": "command",
            "source": "verify_command",
            "verify_cmd": "pytest -q",
            "artifacts": ["src/app.py"],
            "summary": "blocked",
            "blocking_reason": "MAX_CYCLES",
            "command_executed": True,
            "returncode": 2,
        },
        "gate_check": {"total_status": "PASS", "blocking_violations": 0, "total_violations": 0, "profile": {"name": "blocking"}},
        "peer_review_summary": {"approved": True},
        "self_review_summary": {"review_count": 0, "approved_count": 0},
        "rounds": [],
    }

    upstream = build_upstream_result(
        task_label="PLAN: Prepare workflow",
        peer_review_enabled=True,
        payload=payload,
    )

    assert upstream["status"] == "BLOCKED"
    assert upstream["passed"] is False
    assert upstream["stop_reason"] == "MAX_CYCLES"
    assert upstream["blocked"] is True
    assert upstream["round_outcome"] == "blocked"
    assert upstream["peer_review_runtime"]["mode"] == ""
    assert upstream["peer_review_runtime"]["source"] == ""


def test_workflow_chain_upstream_exposes_summary_peer_review_runtime_when_runtime_semantics_missing() -> None:
    payload = {
        "reason": "COLLABORATION_ROUND_LIMIT",
        "unified_status": {"stop_reason": "STUCK", "is_blocked": True},
        "peer_review_summary": {
            "approved": False,
            "latest_review_mode": "real_required_failed",
            "latest_source": "kodawari.real_peer_review_required",
            "real_review_requested": True,
            "real_review_required": True,
            "real_review_fallback_used": False,
            "real_review_error": "openai: http 500",
        },
        "self_review_summary": {"review_count": 0, "approved_count": 0},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS"},
    }
    upstream = build_upstream_result(
        task_label="PLAN: Prepare workflow",
        peer_review_enabled=True,
        payload=payload,
    )
    assert upstream["peer_review_runtime"]["mode"] == "real_required_failed"
    assert upstream["peer_review_runtime"]["source"] == "kodawari.real_peer_review_required"
    assert upstream["peer_review_runtime"]["real_requested"] is True
    assert upstream["peer_review_runtime"]["real_required"] is True
    assert upstream["peer_review_runtime"]["fallback_used"] is False
    assert "http 500" in upstream["peer_review_runtime"]["error"]


def test_workflow_chain_upstream_and_final_review_surface_real_opus_blocking_reason() -> None:
    payload = {
        "reason": "OPUS_REVIEW_BLOCKED",
        "unified_status": {
            "stop_reason": "HARD_ERROR",
            "is_blocked": True,
            "blocking_reason": "WORKFLOW_REVIEWER_BASE_URL (or WORKFLOW_OPUS_GATEWAY) is empty",
        },
        "loop_outcome": {
            "reason": "OPUS_REVIEW_BLOCKED",
            "stop_reason": "HARD_ERROR",
            "blocked": True,
            "blocking_reason": "WORKFLOW_REVIEWER_BASE_URL (or WORKFLOW_OPUS_GATEWAY) is empty",
            "round_outcome": "blocked",
        },
        "peer_review_summary": {
            "approved": False,
            "latest_review_mode": "real_required_failed",
            "latest_source": "kodawari.real_peer_review_required",
            "real_review_requested": True,
            "real_review_required": True,
            "real_review_fallback_used": False,
            "real_review_error": "WORKFLOW_REVIEWER_BASE_URL (or WORKFLOW_OPUS_GATEWAY) is empty",
        },
        "self_review_summary": {"review_count": 0, "approved_count": 0},
        "verify_check": {"status": "PASS"},
        "gate_check": {"total_status": "PASS"},
    }

    upstream = build_upstream_result(
        task_label="PLAN: Prepare workflow",
        peer_review_enabled=True,
        payload=payload,
    )
    task_cycle = build_task_cycle_result(
        upstream_passed=False,
        tasks=[{"task_id": "T001", "label": "T001: blocked upstream", "scope": "blocked upstream"}],
        task_results=[],
    )
    final_review = build_final_quality_review(upstream=upstream, task_cycle=task_cycle)
    final_outcome = build_final_outcome(
        peer_review_enabled=True,
        upstream=upstream,
        task_cycle=task_cycle,
        final_review=final_review,
    )

    assert upstream["stop_reason"] == "HARD_ERROR"
    assert upstream["peer_review_runtime"]["mode"] == "real_required_failed"
    assert "WORKFLOW_OPUS_GATEWAY" in upstream["peer_review_runtime"]["error"]
    assert "WORKFLOW_OPUS_GATEWAY" in final_review["blocking_reason"]
    assert "WORKFLOW_OPUS_GATEWAY" in final_outcome["blocking_reason"]


def test_autopilot_cmd_task_cycle_prioritizes_instinct_matching_tasks(tmp_path: Path, capsys) -> None:
    feature = "instinct-task-priority-demo"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [ ] T001: docs/notes.md",
                "- [ ] T002: src/ranking/service.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    learn_from_globs(tmp_path, ["src/ranking/*"])

    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature=feature,
        tier="heavy",
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=True,
        enable_peer_review=False,
        task_label=None,
        task_scope=None,
    )

    rc = run_autopilot_command(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    tasks = payload["workflow_chain"]["task_cycle"]["tasks"]
    assert [task["task_id"] for task in tasks] == ["T002", "T001"]
    assert tasks[0]["instinct_match_score"] >= 1
    assert "src/ranking/*" in tasks[0]["instinct_patterns"]
    assert tasks[1]["instinct_match_score"] == 0


def test_autopilot_cmd_resets_stale_terminal_state_before_new_run(tmp_path: Path, capsys) -> None:
    feature = "stale-state-reset-demo"
    planning_dir = tmp_path / "planning" / feature
    _write_stale_autopilot_state(
        planning_dir,
        project_root=tmp_path,
        feature=feature,
    )
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [ ] T001: refresh stale state",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    rc = run_autopilot_command(_task_cycle_args(tmp_path, feature=feature))
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    _assert_stale_reset_payload(payload)
    state_payload = json.loads((planning_dir / ".autopilot_state.json").read_text(encoding="utf-8"))
    assert int(state_payload["cycle"]) < 99
    assert state_payload["stop_reason"] is None
    assert state_payload["final_status"] is None


def test_autopilot_cmd_prefers_task_scope_changed_files_for_status_truth(tmp_path: Path, capsys) -> None:
    feature = "changed-files-scope-truth"
    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature=feature,
        tier="heavy",
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=False,
        enable_peer_review=False,
        task_label="Implement scoped changes",
        task_scope="files_to_change=['app/main.py', 'app/schemas.py', 'tests/test_api.py']",
    )

    rc = run_autopilot_command(args)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    state_path = Path(payload["state_path"])
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["changed_files"] == ["app/main.py", "app/schemas.py", "tests/test_api.py"]


def test_reliable_changed_files_prefers_task_delta_truth(tmp_path: Path) -> None:
    for relative in ("app/main.py", "app/provider.py", "tests/test_provider.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# changed\n", encoding="utf-8")
    state = SimpleNamespace(changed_files={"app/stale.py"}, subtasks={})
    run_result = {
        "execution_result": {"changed_files": ["app/provider.py"]},
        "changed_files": ["app/provider.py"],
        "task_delta_changed_files": ["app/main.py", "app/provider.py", "tests/test_provider.py"],
    }

    changed_files, source = resolve_reliable_changed_files(
        project_root=tmp_path,
        state=state,
        run_result=run_result,
    )

    assert changed_files == ["app/main.py", "app/provider.py", "tests/test_provider.py"]
    assert source == "task_delta_changed_files:existing"


def test_primary_task_pass_clears_stale_current_error() -> None:
    state = SimpleNamespace(
        completed_tasks=[],
        last_error="executor stalled before recovery",
        verify_setup_recovery_last_error="old verify setup issue",
        task_claim={"task_id": "T007"},
    )

    _mark_primary_task_complete_if_pass(
        state,
        task_label="T007: recovered task",
        run_result={"reason": "PROCEED_TO_GATE"},
    )

    assert state.completed_tasks == ["T007: recovered task"]
    assert state.last_error is None
    assert state.verify_setup_recovery_last_error is None
    assert state.task_claim == {}


def test_clear_task_claim_handles_pre_execution_block() -> None:
    state = SimpleNamespace(task_claim={"task_id": "T108", "claimed_by": "pid:123"})

    _clear_task_claim(state)

    assert state.task_claim == {}


def test_claim_is_active_ignores_dead_local_pid_before_ttl_expiry() -> None:
    now = datetime.now(timezone.utc)
    claim = {
        "task_id": "T108",
        "claimed_by": "pid:99999999",
        "claim_expires_at": (now + timedelta(minutes=30)).isoformat(),
    }

    assert _claim_is_active(claim, now=now) is False


def test_autopilot_interaction_uses_current_pass_result_over_stale_state(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "interaction-pass"
    planning_dir.mkdir(parents=True)

    class _StaleState:
        def get_unified_status(self) -> dict[str, object]:
            return {
                "final_status": None,
                "stop_reason": None,
                "blocking_reason": "old executor stall",
                "is_blocked": True,
                "is_terminal": False,
            }

    snapshot = autopilot_interaction_snapshot(
        planning_dir=planning_dir,
        state=_StaleState(),
        run_result={
            "reason": "PROCEED_TO_GATE",
            "unified_status": {
                "final_status": "PASS",
                "stop_reason": "PASS",
                "blocking_reason": "",
                "is_blocked": False,
                "is_terminal": True,
            },
            "execution_result": {"status": "PASS"},
        },
    )

    assert snapshot["interaction_state"] == "PASS"
    assert snapshot["next_action_type"] == "completed"


def test_autopilot_cmd_task_cycle_disabled_clears_stale_workflow_chain(
    tmp_path: Path,
    capsys,
) -> None:
    feature = "stale-chain-disabled-task-cycle"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True)
    chain_path = planning_dir / ".workflow_chain.json"
    chain_path.write_text(
        json.dumps({"final_outcome": {"status": "BLOCKED", "reason": "stale"}}),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature=feature,
        tier="heavy",
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
        task_cycle=False,
        enable_peer_review=False,
        task_label="T007: recovered primary task",
        task_scope="Implement recovered primary task",
    )

    rc = run_autopilot_command(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["interaction_state"] == "PASS"
    assert payload["planning_artifacts"][".workflow_chain.json"]["exists"] is False
    assert not chain_path.exists()


def test_autopilot_cmd_uses_requirements_alias_for_contract_first_planning(tmp_path: Path, capsys) -> None:
    requirements_file = tmp_path / "requirements.md"
    requirements_file.write_text("Build a small FastAPI style service.\n", encoding="utf-8")
    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature="alias-contract-first",
        tier="heavy",
        requirements_file=str(requirements_file),
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
    )

    rc = run_autopilot_command(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planning_artifact_mode"] == "contract_first"
    assert payload["planning_snapshot"]["task_card_path"]
    assert payload["interaction_state"] != "AWAITING_DECISION"


def test_autopilot_cmd_prd_flow_advances_from_architecture_to_release_decision(tmp_path: Path, capsys) -> None:
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(
        "\n".join(
            [
                "Business outcome:",
                "- expose a backend endpoint and add tests.",
                "Source of truth:",
                "- db.rankings",
                "Flow type:",
                "- read",
                "Layers:",
                "- route",
                "- service",
                "- schema",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature="prd-product-flow",
        tier="heavy",
        prd=str(prd_path),
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=8,
        token_budget=300000,
    )

    rc = run_autopilot_command(args)

    assert rc == 0
    first_payload = json.loads(capsys.readouterr().out)
    assert first_payload["status"] == "awaiting_decision"
    assert first_payload["decision_kind"] in {"intent_clarification", "architecture_freeze"}
    planning_dir = Path(first_payload["planning_dir"])
    write_decision_response(
        planning_dir,
        build_decision_response(
            decision_id=first_payload["decision_id"],
            selected_option="approve",
            rationale="architecture looks good",
        ),
    )

    rc = run_autopilot_command(args)

    assert rc == 0
    second_payload = json.loads(capsys.readouterr().out)
    assert second_payload["status"] == "awaiting_decision"
    assert second_payload["decision_kind"] in {"architecture_freeze", "release_approval"}


def test_get_unified_status_safe_returns_dict_for_valid_state() -> None:
    """Helper returns the dict produced by state.get_unified_status()."""
    from kodawari.cli.runtime.autopilot_cmd import _get_unified_status_safe

    class _Stub:
        def get_unified_status(self) -> dict[str, int]:
            return {"completed_tasks_total": 2}

    assert _get_unified_status_safe(_Stub()) == {"completed_tasks_total": 2}


def test_get_unified_status_safe_returns_none_when_state_has_no_getter() -> None:
    from kodawari.cli.runtime.autopilot_cmd import _get_unified_status_safe

    assert _get_unified_status_safe(object()) is None


def test_get_unified_status_safe_returns_none_when_getter_raises() -> None:
    from kodawari.cli.runtime.autopilot_cmd import _get_unified_status_safe

    class _Boom:
        def get_unified_status(self) -> dict[str, int]:
            raise RuntimeError("boom")

    assert _get_unified_status_safe(_Boom()) is None


def test_get_unified_status_safe_returns_none_when_getter_returns_non_dict() -> None:
    from kodawari.cli.runtime.autopilot_cmd import _get_unified_status_safe

    class _Bad:
        def get_unified_status(self) -> list[int]:  # type: ignore[override]
            return [1, 2, 3]

    assert _get_unified_status_safe(_Bad()) is None


def test_maybe_warn_unexecuted_tasks_warns_when_cycle_off_and_tasks_remain(
    caplog,
) -> None:
    """Warning fires + payload/run_truth get task_cycle_warning text."""
    import logging
    from kodawari.cli.runtime.autopilot_cmd import _maybe_warn_unexecuted_tasks

    caplog.set_level(logging.WARNING, logger="kodawari.cli.runtime.autopilot_cmd")
    payload: dict[str, Any] = {}
    run_truth: dict[str, Any] = {"unexecuted_task_ids": ["T2_HELPER", "T3_DOCS"]}
    args = SimpleNamespace(task_cycle=False)

    _maybe_warn_unexecuted_tasks(args=args, run_truth=run_truth, payload=payload)

    assert "T2_HELPER" in payload["task_cycle_warning"]
    assert "T3_DOCS" in payload["task_cycle_warning"]
    assert "rerun with --task-cycle" in payload["task_cycle_warning"]
    assert run_truth["task_cycle_warning"] == payload["task_cycle_warning"]
    assert any("task_cycle is disabled" in r.message for r in caplog.records)


def test_maybe_warn_unexecuted_tasks_silent_when_cycle_on() -> None:
    """task_cycle=True suppresses the warning (the loop will pick up T2 itself)."""
    from kodawari.cli.runtime.autopilot_cmd import _maybe_warn_unexecuted_tasks

    payload: dict[str, Any] = {}
    run_truth: dict[str, Any] = {"unexecuted_task_ids": ["T2_HELPER"]}
    args = SimpleNamespace(task_cycle=True)

    _maybe_warn_unexecuted_tasks(args=args, run_truth=run_truth, payload=payload)

    assert "task_cycle_warning" not in payload
    assert "task_cycle_warning" not in run_truth


def test_maybe_warn_unexecuted_tasks_silent_when_no_tasks_remain() -> None:
    """Empty unexecuted_task_ids -> no warning noise on single-task lite runs."""
    from kodawari.cli.runtime.autopilot_cmd import _maybe_warn_unexecuted_tasks

    payload: dict[str, Any] = {}
    run_truth: dict[str, Any] = {"unexecuted_task_ids": []}
    args = SimpleNamespace(task_cycle=False)

    _maybe_warn_unexecuted_tasks(args=args, run_truth=run_truth, payload=payload)

    assert "task_cycle_warning" not in payload

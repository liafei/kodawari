import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.autopilot.repo_inventory import build_repo_inventory
from kodawari.cli.autopilot_decision_runtime import (
    build_decision_request,
    build_decision_response,
    write_decision_request,
    write_decision_response,
)
from kodawari.cli import main as cli_main
from kodawari.cli.main import build_parser
from kodawari.infra.contract_version import MERGED_CONTRACT_VERSION
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


def _base_state_payload(project_root: Path, feature: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "autopilot.state.v2",
        "revision": 1,
        "feature": feature,
        "project_root": str(project_root),
        "current_stage": "COMPLETED",
        "cycle": 2,
        "tokens_used": 500,
        "error_history": [],
        "last_error": None,
        "changed_files": [],
        "completed_tasks": [],
        "task_timings": {},
        "active_task": None,
        "active_pid": None,
        "active_attempt": None,
        "stage_started_at": None,
        "heartbeat_at": None,
        "last_stage_status": "PASS",
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
        "updated_at": "2026-03-16T00:00:00+00:00",
        "stop_reason": "PASS",
        "final_status": "PASS",
    }
    payload.update(overrides)
    return payload


def _write_autopilot_state(planning_dir: Path, payload: dict[str, Any]) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _write_stale_develop_state(planning_dir: Path, *, project_root: Path, feature: str) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            project_root,
            feature,
            current_stage="COMPLETED",
            cycle=99,
            last_error="legacy failure",
            last_stage_status="blocked",
            error_history=["legacy failure"],
            stop_reason="MAX_CYCLES",
            final_status="BLOCKED",
            subtasks={"T001.1": dict(_STALE_FAILED_SUBTASK)},
            active_subtask="T001.1",
        ),
    )


def _write_single_task_backlog(planning_dir: Path, *, feature: str, task_scope: str) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASKS.md").write_text(
        "\n".join([f"# TASKS ({feature})", "", f"- [ ] T001: {task_scope}"]) + "\n",
        encoding="utf-8",
    )


def _assert_develop_stale_reset(payload: dict[str, Any]) -> None:
    assert payload["flow"]["autopilot_run_reason"] == "PROCEED_TO_GATE"
    assert payload["workflow_chain"]["upstream"]["status"] == "PASS"
    assert payload["workflow_chain"]["final_outcome"]["status"] == "PASS"
    assert payload["flow"]["final_outcome"]["status"] == "PASS"


def _write_round_record(
    planning_dir: Path,
    *,
    stage: str,
    stage_status: str,
    last_error: str = "",
) -> None:
    (planning_dir / ".autopilot_rounds.jsonl").write_text(
        json.dumps(
            {
                "stage": stage,
                "stage_status": stage_status,
                "last_error": last_error,
                "details": {"status": stage_status},
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _run_cli(parser: Any, capsys: Any, argv: list[str]) -> dict[str, Any]:
    args = parser.parse_args(argv)
    rc = args.handler(args)
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def _run_cli_with_rc(parser: Any, capsys: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    args = parser.parse_args(argv)
    rc = args.handler(args)
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _write_pass_workflow_chain_snapshot(planning_dir: Path, feature: str) -> None:
    (planning_dir / ".workflow_chain.json").write_text(
        json.dumps(
            {
                "version": "ws115.chain.v1",
                "feature": feature,
                "planning_dir": str(planning_dir.resolve()),
                "peer_review_enabled": True,
                "task_cycle_enabled": True,
                "mode": "peer_review",
                "upstream": {"passed": True},
                "task_cycle": {"entered": True, "tasks_completed": 1, "tasks_total": 1},
                "final_quality_review": {"review_source": "workflow_chain_aggregation", "status": "PASS", "summary": "All tasks completed", "blocking_reason": ""},
                "chain_final_outcome": {"status": "PASS", "reason": "ALL_TASKS_COMPLETE", "blocking_reason": ""},
                "final_outcome": {"status": "PASS", "reason": "ALL_TASKS_COMPLETE", "blocking_reason": ""},
            }
        ),
        encoding="utf-8",
    )


def _write_blocked_gate_chain_artifacts(planning_dir: Path, project_root: Path) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    _write_autopilot_state(planning_dir, _base_state_payload(project_root, planning_dir.name))
    for name in ("PLAN.md", "TASKS.md", "ACCEPTANCE.md", "GATE.md"):
        (planning_dir / name).write_text(f"# {name}\n", encoding="utf-8")
    (planning_dir / ".gate_result.json").write_text(
        json.dumps(
            {
                "total_status": "BLOCKED",
                "profile": {"name": "advisory"},
                "blocking_violations": 1,
                "total_violations": 1,
                "blocking_reason": "src/demo.py: file length exceeds advisory threshold",
            }
        ),
        encoding="utf-8",
    )
    _write_pass_workflow_chain_snapshot(planning_dir, planning_dir.name)


class _FakeGateReport:
    def __init__(self, total_status: str) -> None:
        self._payload = {
            "profile": {"name": "advisory"},
            "total_status": total_status,
            "blocking_violations": 1 if total_status == "BLOCKED" else 0,
            "total_violations": 1 if total_status == "BLOCKED" else 0,
            "items": _fake_gate_items(total_status),
        }

    def to_dict(self) -> dict[str, Any]:
        return dict(self._payload)


def _fake_gate_items(total_status: str) -> list[dict[str, Any]]:
    if total_status != "BLOCKED":
        return []
    return [{"violations": [{"path": "src/demo.py", "message": "advisory gate blocked on purpose"}]}]


def _patch_advisory_gate_block(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_evaluate(self, *, targets: list[Path], profile_name: str):  # type: ignore[no-untyped-def]
        del self, targets
        if profile_name == "advisory":
            return _FakeGateReport("BLOCKED")
        return _FakeGateReport("PASS")

    monkeypatch.setattr(cli_main.GateEngine, "evaluate", _fake_evaluate)


def _assert_provenance_payload(payload: dict[str, Any], *, command: str) -> None:
    provenance = payload["provenance"]
    assert provenance["command"] == command
    assert provenance["cwd"]
    assert "module_repo_root" in provenance
    assert "wrapper_is_canonical" in provenance
    assert "repo_alignment" in provenance
    assert "module_vs_cwd_repo" in provenance["repo_alignment"]
    assert "entrypoint_resolution" in provenance
    resolution = provenance["entrypoint_resolution"]
    assert "target_repo_root" in resolution
    assert "module_matches_target_repo" in resolution
    assert "cwd_matches_target_repo" in resolution
    assert "wrapper_matches_target_repo" in resolution
    assert "likely_old_install_mis_hit" in resolution
    assert "recommended_entrypoint" in resolution


def test_repo_local_wrapper_is_canonical_entrypoint() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = repo_root / "scripts" / "kodawari.ps1"
    assert wrapper.exists()
    text = wrapper.read_text(encoding="utf-8")
    assert "kodawari.cli.main" in text
    assert "src" in text


def test_cli_help_includes_migration_release_and_incident_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "execution-evidence" in help_text
    assert "migrate-artifacts" in help_text
    assert "replay-gate" in help_text
    assert "canary-gate" in help_text
    assert "incident-ingest" in help_text


def test_cli_help_includes_setup_plan_work_release_facades() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "setup" in help_text
    assert "plan" in help_text
    assert "work" in help_text
    assert "work-all" in help_text
    assert "release" in help_text


def test_cli_global_verbose_flag_is_available() -> None:
    parser = build_parser()
    args = parser.parse_args(["-v", "status", "--project-root", ".", "--feature", "demo"])
    assert args.verbose == 1


def test_cli_setup_writes_repo_inventory_artifacts(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "setup-demo"
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    payload = _run_cli(
        parser,
        capsys,
        ["setup", "--project-root", str(tmp_path), "--feature", feature],
    )

    planning_dir = tmp_path / "planning" / feature
    assert payload["entrypoint"] == "kodawari setup"
    assert payload["status"] == "PASS"
    assert (planning_dir / "REPO_INVENTORY.json").exists()
    assert (planning_dir / "REPO_INVENTORY.md").exists()
    assert payload["artifacts"]["REPO_INVENTORY.json"].endswith("REPO_INVENTORY.json")


def test_cli_plan_facade_materializes_contract_truth_and_plans_md(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "plan-facade"
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(
        "\n".join(
            [
                "Build ranking API read flow.",
                "source of truth: db.articles",
                "layer: route service repository",
            ]
        ),
        encoding="utf-8",
    )

    payload = _run_cli(
        parser,
        capsys,
        ["plan", "--project-root", str(tmp_path), "--feature", feature, "--prd", str(prd_path)],
    )

    planning_dir = tmp_path / "planning" / feature
    assert payload["entrypoint"] == "kodawari plan"
    assert payload["status"] == "PASS"
    assert (planning_dir / "PRD_INTAKE.json").exists()
    assert (planning_dir / "TASK_GRAPH.json").exists()
    assert (planning_dir / "TASK_CARD_ACTIVE.json").exists()
    assert (planning_dir / "Plans.md").exists()
    assert payload["artifacts"]["Plans.md"].endswith("Plans.md")
    task_graph_path = planning_dir / "TASK_GRAPH.json"
    task_graph = json.loads(task_graph_path.read_text(encoding="utf-8"))
    plans_md = (planning_dir / "Plans.md").read_text(encoding="utf-8")
    assert "## Mirror Provenance" in plans_md
    assert f"- generated_at: {task_graph['generated_at']}" in plans_md
    assert f"- source_digest: {hashlib.sha256(task_graph_path.read_bytes()).hexdigest()}" in plans_md


@pytest.mark.parametrize("command_argv", (["work", "all"], ["work-all"]))
def test_cli_work_all_writes_run_manifest(
    command_argv: list[str],
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    feature = "work-all-demo"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text("work all fullchain prd\n", encoding="utf-8")

    def _fake_autopilot(args: Any) -> int:
        (planning_dir / ".autopilot_state.json").write_text(
            json.dumps({"final_status": "PASS", "stop_reason": "PASS"}),
            encoding="utf-8",
        )
        print(json.dumps({"status": "ok", "entrypoint": "kodawari autopilot", "feature": args.feature}, ensure_ascii=False, indent=2))
        return 0

    monkeypatch.setattr("kodawari.cli.runtime.work_all_runtime.run_autopilot_command", _fake_autopilot)

    args = parser.parse_args([*command_argv, "--project-root", str(tmp_path), "--feature", feature, "--prd", str(prd_path)])
    rc = args.handler(args)
    payload = json.loads(capsys.readouterr().out)

    manifest_path = planning_dir / ".work_all_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert rc in {0, 2}
    assert payload["entrypoint"] == "kodawari work all"
    assert payload["status"] in {"PASS", "BLOCKED", "FAIL"}
    assert payload["steps"]
    assert payload["steps"][0]["name"] == "plan"
    assert payload["steps"][1]["name"] == "work"
    assert any(step["name"] == "review" for step in payload["steps"])
    assert manifest["entrypoint"] == "kodawari work all"
    assert manifest["_rc"] in {0, 2}
    assert manifest["status"] in {"PASS", "BLOCKED", "FAIL"}
    assert manifest["steps"]
    assert manifest["steps"][0]["name"] == "plan"
    assert manifest["steps"][1]["name"] == "work"
    assert any(step["name"] == "review" for step in manifest["steps"])


def test_cli_work_all_replan_does_not_replan_work_step(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    feature = "work-all-replan-demo"
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text("work all replan prd\n", encoding="utf-8")
    seen: dict[str, Any] = {}

    def _fake_autopilot(args: Any) -> int:
        seen["replan"] = bool(getattr(args, "replan", False))
        print(json.dumps({"status": "ok", "entrypoint": "kodawari autopilot", "feature": args.feature}, ensure_ascii=False))
        return 0

    monkeypatch.setattr("kodawari.cli.runtime.work_all_runtime.run_autopilot_command", _fake_autopilot)

    args = parser.parse_args(
        [
            "work",
            "all",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--prd",
            str(prd_path),
            "--replan",
        ]
    )
    rc = args.handler(args)
    json.loads(capsys.readouterr().out)

    assert rc in {0, 2}
    assert seen["replan"] is False


def test_cli_work_all_blocks_without_prd(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    args = parser.parse_args(["work-all", "--project-root", str(tmp_path), "--feature", "work-all-blocked"])
    rc = args.handler(args)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["error_code"] == "work_all_prd_required"


def test_cli_status_enriches_autopilot_state_with_unified_status(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "demo"
    subtasks = {
        "t002.1": {
            "subtask_id": "t002.1",
            "title": "Add scoring",
            "parent_task_id": "T002",
            "status": "PENDING",
            "depends_on": [],
            "changed_files": [],
            "tokens_used": 0,
            "duration_seconds": 0,
            "verify_cmd": None,
            "verify_status": None,
            "verify_output": None,
            "error": None,
            "attempt": 0,
            "started_at": None,
            "completed_at": None,
        }
    }
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            "demo",
            current_stage="IMPLEMENT",
            cycle=1,
            tokens_used=100,
            stop_reason=None,
            final_status=None,
            last_stage_status="running",
            active_task="T002: Implement ranking rules",
            active_subtask="t002.1",
            subtasks=subtasks,
        ),
    )

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "demo"])
    assert payload["contract_version"] == "ws115.v1"
    assert payload["planning_contract"]["version"] == "ws115.v1"
    assert payload["planning_contract"]["complete"] is False
    assert payload["planning_contract"]["required_artifacts"] == ["PLAN.md", "TASKS.md", "ACCEPTANCE.md", "GATE.md"]
    assert payload["artifacts"]["PLAN.md"]["semantic"] == "planning_scope_and_strategy"
    assert payload["artifacts"]["GATE.md"]["semantic"] == "human_readable_gate_decision_summary"
    assert payload["state"]["unified_status"]["current_phase"] == "IMPLEMENT"
    assert payload["state"]["unified_status"]["pending_subtasks"] == ["t002.1"]
    _assert_provenance_payload(payload, command="status")
    assert payload["provenance"]["planning_dir"] == str(planning_dir.resolve())


def test_cli_status_writes_status_snapshot_and_markdown_mirror(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "status-mirror-demo"
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            "status-mirror-demo",
            current_stage="GATE",
            cycle=2,
            stop_reason=None,
            final_status=None,
            last_stage_status="running",
        ),
    )

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "status-mirror-demo"])
    snapshot_path = planning_dir / ".status_snapshot.json"
    markdown_path = planning_dir / "STATUS.md"
    assert snapshot_path.exists()
    assert markdown_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["status_truth_source"] == ".status_snapshot.json"
    assert snapshot["provenance"]["command"] == "status"
    assert snapshot["parallel_merge_status"] == ""
    assert snapshot["worker_statuses"] == []
    assert snapshot["reasoning_tier"] in {"economy", "standard", "deep_reasoning"}
    assert isinstance(snapshot["effort_score"], int)
    assert isinstance(snapshot["effort_reasons"], list)
    assert snapshot["payload_digest"]
    assert snapshot["payload_digest"] == payload["payload_digest"]
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "## Mirror Provenance" in markdown
    assert ".status_snapshot.json" in markdown
    assert "reasoning_tier:" in markdown
    assert "parallel_merge_status:" in markdown
    assert payload["payload_digest"] in markdown


def test_cli_status_exposes_pending_decision_interaction_state(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "decision-demo"
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            "decision-demo",
            current_stage="PLANNING",
            cycle=0,
            tokens_used=0,
            stop_reason=None,
            final_status=None,
            last_stage_status="pending",
            active_task=None,
        ),
    )
    write_decision_request(
        planning_dir,
        build_decision_request(
            decision_id="decision-demo:architecture_freeze",
            decision_kind="architecture_freeze",
            question="Freeze architecture?",
            context_summary="greenfield",
            options=[{"option_id": "approve", "label": "Approve"}],
            recommended_option="approve",
            blocking_reason="needs approval",
        ),
    )

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "decision-demo"])

    assert payload["interaction_state"] == "AWAITING_DECISION"
    assert payload["decision_kind"] == "architecture_freeze"
    assert payload["decision_id"] == "decision-demo:architecture_freeze"
    assert payload["decision_request_present"] is True
    assert payload["next_action_type"] == "await_decision"


def test_cli_status_clears_decision_request_present_after_matching_response(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "decision-resolved"
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            "decision-resolved",
            current_stage="COMPLETED",
            cycle=1,
            tokens_used=10,
            stop_reason="PASS",
            final_status="PASS",
            last_stage_status="PASS",
            active_task=None,
        ),
    )
    write_decision_request(
        planning_dir,
        build_decision_request(
            decision_id="decision-resolved:release_approval",
            decision_kind="release_approval",
            question="Approve release?",
            context_summary="qa pass",
            options=[{"option_id": "ship", "label": "Ship"}],
            recommended_option="ship",
            blocking_reason="needs approval",
        ),
    )
    write_decision_response(
        planning_dir,
        build_decision_response(
            decision_id="decision-resolved:release_approval",
            selected_option="ship",
            rationale="approved",
        ),
    )

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "decision-resolved"])

    assert payload["interaction_state"] == "PASS"
    assert payload["decision_request_present"] is False
    assert payload["next_action_type"] == "completed"


def test_cli_status_falls_back_to_semantic_compact_loop_outcome_without_state(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "blocked-no-state"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "semantic_compact.json").write_text(
        json.dumps(
            {
                "schema_version": "semantic_compact.v1",
                "loop_outcome": {
                    "reason": "OPUS_REVIEW_BLOCKED",
                    "stop_reason": "HARD_ERROR",
                    "final_status": "BLOCKED",
                    "is_blocked": True,
                },
            }
        ),
        encoding="utf-8",
    )

    payload = _run_cli(
        parser,
        capsys,
        ["status", "--project-root", str(tmp_path), "--feature", "blocked-no-state"],
    )

    assert payload["state_source"] == "none"
    assert payload["interaction_state"] == "BLOCKED"


def test_cli_status_falls_back_to_task_run_result_without_state(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "blocked-from-task-run"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".task_run_result.json").write_text(
        json.dumps({"reason": "OPUS_REVIEW_BLOCKED"}),
        encoding="utf-8",
    )

    payload = _run_cli(
        parser,
        capsys,
        ["status", "--project-root", str(tmp_path), "--feature", "blocked-from-task-run"],
    )

    assert payload["state_source"] == "none"
    assert payload["interaction_state"] == "BLOCKED"


def test_cli_status_requires_versioned_autopilot_state(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "legacy"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps(
            {
                "feature": "legacy",
                "project_root": str(tmp_path),
                "current_stage": "COMPLETED",
            }
        ),
        encoding="utf-8",
    )

    rc, payload = _run_cli_with_rc(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "legacy"])
    assert rc == 2
    assert payload["error_code"] == "artifact_schema_version_invalid"
    assert "migrate-artifacts" in payload["remediation"][0]


def test_cli_status_contract_first_uses_contract_artifacts_not_legacy_docs(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "contract-demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps(
            {
                "schema_version": "contract_first.prd_intake.v1",
                "generated_at": "2026-03-25T00:00:00+00:00",
                "feature": "contract-demo",
                "business_outcome": "Return hydration goal history in one response.",
                "source_of_truth": ["db.hydration_goals"],
                "source_of_truth_canonical": ["db.hydration_goals"],
                "path_type": "read",
                "layers": ["route", "service", "repository"],
                "coverage_hints": ["layer:route"],
                "out_of_scope": [],
                "confidence": "high",
                "confidence_issues": [],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "TASK_GRAPH.json").write_text(
        json.dumps(
            {
                "schema_version": "contract_first.task_graph.v1",
                "generated_at": "2026-03-25T00:00:00+00:00",
                "business_outcome": "Return hydration goal history in one response.",
                "project_layout": {"kind": "app", "code_roots": ["app"], "test_roots": ["tests"], "workspace_roots": []},
                "project_profile": "fastapi",
                "coverage_hints": ["layer:route"],
                "executability": {"status": "PASS", "issues": []},
                "tasks": [
                    {
                        "task_id": "T1",
                        "task_name": "Update route",
                        "core_files": ["app/main.py"],
                        "layer_owner": "route",
                        "invariants": ["No second source of truth is introduced."],
                        "test_proof": "Run route tests.",
                        "executability": {"status": "PASS", "issues": []},
                    }
                ],
                "boundary_debt": {"status": "PASS", "details": "", "items": []},
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "generated_at": "2026-03-25T00:00:00+00:00",
                "task_id": "T1",
                "task_name": "Update route",
                "task_kind": "modify",
                "layer": "route",
                "why_this_layer": "Route owns binding.",
                "files_to_change": ["app/main.py"],
                "allowed_new_files": [],
                "acceptance": ["Route exposes hydration history."],
                "invariants": ["No second source of truth is introduced."],
                "test_plan": "Run route tests.",
                "requires": [],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "REPO_INVENTORY.json").write_text(
        json.dumps(build_repo_inventory(project_root=tmp_path, archetype="fastapi_api", mode="existing")),
        encoding="utf-8",
    )
    (planning_dir / ".execution_result.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.result.v1",
                "feature": "contract-demo",
                "task": "T1: Update route",
                "backend": "external_cli",
                "backend_capabilities": {
                    "backend": "external_cli",
                    "maturity": "stable",
                    "implemented": True,
                    "executor_selectable": True,
                    "self_review_selectable": True,
                    "requires_command": True,
                    "supports_agent_teams": False,
                    "supports_worktree_isolation": False,
                    "supports_hooks": False,
                    "supports_memory": False,
                    "supports_deterministic_changed_files": False,
                },
                "status": "PASS",
                "changed_files": ["app/main.py"],
                "stdout_excerpt": "",
                "stderr_excerpt": "",
                "returncode": 0,
                "artifacts": ["app/main.py"],
                "error_code": "",
                "blocking_reason": "",
                "summary": "ok",
                "host_probe": {
                    "status": "degraded",
                    "surface": "claude_cli",
                    "reason": "command_override",
                    "executable": "claude",
                    "executable_available": False,
                },
                "guard_action": "deny",
                "guard_policy": "Sudo commands blocked",
                "guard_pattern": "sudo\\s",
                "guard_command": "sudo rm -rf /",
                "guard_decision": {
                    "action": "deny",
                    "reason": "Sudo commands blocked",
                    "pattern": "sudo\\s",
                    "command": "sudo rm -rf /",
                },
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".review_result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "changed_files": {
                    "source": ".review_result.json.changed_files",
                    "items": ["app/main.py"],
                    "count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".review_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": "review.evidence.v1",
                "feature": "contract-demo",
                "generated_at": "2026-03-25T00:00:00+00:00",
                "planning_dir": str(planning_dir.resolve()),
                "entrypoint": "kodawari review-evidence",
                "status": "PASS",
                "blocking_reason": "",
                "checks": {
                    "self_review_count": 1,
                    "peer_review_count": 1,
                    "must_fix_remaining": 0,
                },
                "issues": [],
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".verify_report.json").write_text(
        json.dumps(
            {
                "schema_version": "verify.report.v1",
                "generated_at": "2026-03-25T00:00:00+00:00",
                "feature": "contract-demo",
                "planning_dir": str(planning_dir.resolve()),
                "entrypoint": "kodawari verify",
                "requested_command": "pytest -q",
                "requested_command_kind": "default",
                "changed_files": {"source": ".review_result.json.changed_files", "items": ["app/main.py"], "count": 1},
                "input_confidence": "curated",
                "status": "PASS",
                "verify_scope_mode": "surface_plan",
                "surface_results": [{"surface": "backend", "status": "PASS"}],
                "surface_summary": {"required_surfaces": ["backend"], "available_surfaces": ["backend"]},
                "verify_check": {
                    "status": "PASS",
                    "passed": True,
                    "mode": "command",
                    "source": "verify_command",
                    "verify_cmd": "pytest -q",
                    "verify_cmd_resolved": "pytest -q",
                    "verify_target_source": "default",
                    "verify_targets": [],
                    "summary": "ok",
                    "blocking_reason": "",
                    "command_executed": True,
                    "returncode": 0,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "",
                    "artifacts": [],
                },
            }
        ),
        encoding="utf-8",
    )
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            "contract-demo",
            stop_reason="TOKEN_BUDGET",
            final_status="BLOCKED",
            tokens_used=320,
        ),
    )
    (planning_dir / "semantic_compact.json").write_text(
        json.dumps({"token_budget_snapshot": {"tokens_used": 320, "token_budget": 300, "budget_exhausted": True}}),
        encoding="utf-8",
    )
    (planning_dir / ".decision_request.json").write_text(
        json.dumps(
            build_decision_request(
                decision_id="decision-contract-demo",
                decision_kind="architecture_freeze",
                question="Freeze architecture?",
                context_summary="multi-surface project requires approval",
                options=[{"option_id": "approve", "label": "Approve"}],
            )
        ),
        encoding="utf-8",
    )

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "contract-demo"])

    assert payload["planning_artifact_mode"] == "contract_first"
    assert payload["planning_complete"] is True
    assert payload["planning_contract"]["required_artifacts"] == [
        "PRD_INTAKE.json",
        "REPO_INVENTORY.json",
        "TASK_GRAPH.json",
        "TASK_CARD_ACTIVE.json",
    ]
    assert payload["planning_contract"]["complete"] is True
    assert payload["repo_inventory_present"] is True
    assert payload["architecture_plan_present"] is False
    assert payload["planning_truth_source"] == "PRD_INTAKE.json+REPO_INVENTORY.json+TASK_GRAPH.json+TASK_CARD_ACTIVE.json"
    assert payload["execution_truth_source"] == ".execution_result.json"
    assert payload["review_truth_source"] == ".review_evidence.json"
    assert payload["verify_truth_source"] == ".verify_report.json"
    assert payload["execution_complete"] is True
    assert payload["execution_backend"] == "external_cli"
    assert payload["execution_backend_capabilities"]["backend"] == "external_cli"
    assert payload["execution_backend_capabilities"]["implemented"] is True
    assert payload["execution_host_probe"]["status"] == "degraded"
    assert payload["execution_host_probe"]["reason"] == "command_override"
    assert payload["execution_guard"]["action"] == "deny"
    assert payload["execution_guard"]["policy"] == "Sudo commands blocked"
    assert payload["execution_guard"]["command"] == "sudo rm -rf /"
    assert payload["reasoning_tier"] in {"economy", "standard", "deep_reasoning"}
    assert isinstance(payload["effort_score"], int)
    assert isinstance(payload["effort_reasons"], list)
    assert payload["review_complete"] is True
    assert payload["verify_complete"] is True
    assert payload["tokens_used"] == 320
    assert payload["token_budget"] == 300
    assert payload["budget_exhausted"] is True
    assert payload["interaction_state"] == "AWAITING_DECISION"
    assert payload["decision_kind"] == "architecture_freeze"
    assert payload["decision_id"] == "decision-contract-demo"
    assert payload["decision_request_present"] is True
    assert payload["next_action_type"] == "await_decision"
    assert payload["artifacts"]["PLAN.md"]["exists"] is False
    status_md = (planning_dir / "STATUS.md").read_text(encoding="utf-8")
    assert "## Execution Guard" in status_md
    assert "## Host Probe" in status_md
    assert "command_override" in status_md
    assert "Sudo commands blocked" in status_md


def test_cli_migrate_artifacts_upgrades_unversioned_autopilot_state(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "legacy-migrate"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps(
            {
                "feature": "legacy-migrate",
                "project_root": str(tmp_path),
                "current_stage": "COMPLETED",
                "cycle": 1,
                "tokens_used": 10,
                "error_history": [],
                "changed_files": [],
                "completed_tasks": [],
                "task_timings": {},
                "warning_noise_by_task": {},
                "subtasks": {},
                "architecture_decisions": [],
            }
        ),
        encoding="utf-8",
    )

    dry_rc, dry_payload = _run_cli_with_rc(
        parser,
        capsys,
        ["migrate-artifacts", "--project-root", str(tmp_path), "--feature", "legacy-migrate"],
    )
    assert dry_rc == 0
    assert dry_payload["artifacts_changed"] == 1

    write_rc, write_payload = _run_cli_with_rc(
        parser,
        capsys,
        ["migrate-artifacts", "--project-root", str(tmp_path), "--feature", "legacy-migrate", "--write"],
    )
    assert write_rc == 0
    assert write_payload["artifacts_changed"] == 1
    upgraded = json.loads((planning_dir / ".autopilot_state.json").read_text(encoding="utf-8"))
    assert upgraded["schema_version"] == "autopilot.state.v2"
    assert upgraded["revision"] == 0


def test_cli_status_prefers_existing_git_changed_files_when_state_entries_are_stale(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "demo"
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            "demo",
            changed_files=["src/migrations/001_add_field.sql", "tests/test_schema_migration.py"],
        ),
    )
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("print('demo')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_api.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_git_changed_files", lambda _root: ["app/main.py", "tests/test_api.py"])

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "demo"])
    assert payload["state"]["changed_files"] == ["app/main.py", "tests/test_api.py"]
    assert payload["state"]["changed_files_source"] == "git_worktree:existing"


def test_cli_status_preserves_legacy_changed_files_and_exposes_task_delta(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "demo"
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            "demo",
            changed_files=["src/legacy.py"],
        ),
    )
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("print('demo')\n", encoding="utf-8")
    (tmp_path / "tests" / "test_api.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(cli_main, "_git_changed_files", lambda _root: ["app/main.py", "tests/test_api.py"])
    monkeypatch.setattr(
        cli_main,
        "resolve_task_delta_changed_files",
        lambda **_kwargs: (["tests/test_api.py"], "baseline_delta:git_worktree"),
    )

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "demo"])
    assert payload["state"]["changed_files"] == ["app/main.py", "tests/test_api.py"]
    assert payload["state"]["changed_files_source"] == "git_worktree:existing"
    assert payload["state"]["task_delta_changed_files"] == ["tests/test_api.py"]
    assert payload["state"]["task_delta_changed_files_source"] == "baseline_delta:git_worktree"


def test_cli_status_marks_stale_review_and_verify_artifacts_incomplete(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "status-stale-artifacts"
    planning_dir = tmp_path / "planning" / feature
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            tmp_path,
            feature,
            changed_files=["app/main.py", "tests/test_api.py"],
        ),
    )
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("print('demo')\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (planning_dir / ".review_result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "changed_files": {
                    "source": "git_diff:raw",
                    "items": ["newsapp/app.py"],
                    "count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".review_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": "review.evidence.v1",
                "generated_at": "2026-03-25T00:00:00+00:00",
                "feature": feature,
                "planning_dir": str(planning_dir.resolve()),
                "entrypoint": "kodawari review-evidence",
                "status": "PASS",
                "blocking_reason": "",
                "checks": {"self_review_count": 1, "peer_review_count": 1, "must_fix_remaining": 0},
                "issues": [],
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".verify_report.json").write_text(
        json.dumps(
            {
                "schema_version": "verify.report.v1",
                "generated_at": "2026-03-25T00:00:00+00:00",
                "feature": feature,
                "planning_dir": str(planning_dir.resolve()),
                "entrypoint": "kodawari verify",
                "requested_command": "pytest -q",
                "requested_command_kind": "default",
                "changed_files": {
                    "source": ".review_result.json.changed_files",
                    "items": ["newsapp/app.py"],
                    "count": 1,
                },
                "input_confidence": "curated",
                "status": "PASS",
                "verify_check": {"status": "PASS", "source": "verify_command"},
            }
        ),
        encoding="utf-8",
    )

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", feature])

    assert payload["review_complete"] is False
    assert payload["verify_complete"] is False
    assert payload["review_truth_source"] == "none(stale_review_artifacts)"
    assert payload["verify_truth_source"] == "none(stale_verify_artifacts)"
    assert payload["artifact_truth"]["review_result"]["stale"] is True
    assert payload["artifact_truth"]["review_evidence"]["stale"] is True
    assert payload["artifact_truth"]["verify_report"]["stale"] is True


def test_cli_status_overlays_workflow_chain_when_advisory_gate_blocks(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "demo"
    _write_blocked_gate_chain_artifacts(planning_dir, tmp_path)

    payload = _run_cli(parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "demo"])
    assert payload["gate"]["total_status"] == "BLOCKED"
    assert payload["gate"]["blocking_reason"] == "src/demo.py: file length exceeds advisory threshold"
    assert payload["workflow_chain"]["final_quality_review"]["status"] == "BLOCKED"
    assert payload["workflow_chain"]["final_quality_review"]["review_source"] == "advisory_gate_overlay"
    assert payload["workflow_chain"]["chain_final_outcome"]["status"] == "PASS"
    assert payload["workflow_chain"]["final_outcome"]["status"] == "BLOCKED"
    assert payload["workflow_chain"]["final_outcome"]["reason"] == "ADVISORY_GATE_BLOCKED"
    assert payload["workflow_chain"]["final_outcome"]["blocking_reason"] == payload["gate"]["blocking_reason"]


def test_cli_gate_syncs_effective_workflow_chain_snapshot_when_advisory_gate_blocks(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    planning_dir = tmp_path / "planning" / "gate-sync-demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    _write_pass_workflow_chain_snapshot(planning_dir, "gate-sync-demo")
    _write_autopilot_state(planning_dir, _base_state_payload(tmp_path, "gate-sync-demo", current_stage="GATE", stop_reason=None, final_status=None))
    _patch_advisory_gate_block(monkeypatch)

    payload = _run_cli(
        parser,
        capsys,
        ["gate", "--project-root", str(tmp_path), "--planning-dir", str(planning_dir), "--profile", "advisory"],
    )

    chain_payload = json.loads((planning_dir / ".workflow_chain.json").read_text(encoding="utf-8"))
    state_payload = json.loads((planning_dir / ".autopilot_state.json").read_text(encoding="utf-8"))
    assert payload["total_status"] == "BLOCKED"
    assert payload["blocking_reason"] == "src/demo.py: advisory gate blocked on purpose"
    assert chain_payload["final_quality_review"]["status"] == "BLOCKED"
    assert chain_payload["chain_final_outcome"]["status"] == "PASS"
    assert chain_payload["final_outcome"]["status"] == "BLOCKED"
    assert chain_payload["final_outcome"]["reason"] == "ADVISORY_GATE_BLOCKED"
    assert state_payload["current_stage"] == "COMPLETED"
    assert state_payload["final_status"] == "BLOCKED"
    assert state_payload["stop_reason"] == "HARD_ERROR"


def test_cli_replay_and_canary_gate_commands_consume_frozen_inputs(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    replay_input = tmp_path / "REPLAY_GATE_INPUT.json"
    replay_input.write_text(
        json.dumps(
            {
                "schema_version": "release.replay.input.v1",
                "samples": [
                    {"name": "baseline-pass", "status": "PASS", "details": "ok"},
                    {"name": "regression", "expected_status": "PASS", "actual_status": "BLOCKED", "details": "regressed"},
                ],
            }
        ),
        encoding="utf-8",
    )
    canary_input = tmp_path / "CANARY_GATE_INPUT.json"
    canary_input.write_text(
        json.dumps(
            {
                "schema_version": "release.canary.input.v1",
                "max_failed_samples": 0,
                "samples": [
                    {"name": "shadow-1", "status": "PASS"},
                    {"name": "shadow-2", "status": "FAIL", "details": "error spike"},
                ],
            }
        ),
        encoding="utf-8",
    )

    replay_rc, replay_payload = _run_cli_with_rc(
        parser,
        capsys,
        ["replay-gate", "--project-root", str(tmp_path), "--input", str(replay_input), "--fail-on-block"],
    )
    assert replay_rc == 2
    assert replay_payload["status"] == "BLOCKED"

    canary_rc, canary_payload = _run_cli_with_rc(
        parser,
        capsys,
        ["canary-gate", "--project-root", str(tmp_path), "--input", str(canary_input), "--fail-on-block"],
    )
    assert canary_rc == 2
    assert canary_payload["status"] == "BLOCKED"


def _write_run_artifacts(
    planning_dir: Path,
    *,
    project_root: Path,
    stop_reason: str,
    error_message: str,
) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASKS.md").write_text("## T001: Implement ranking rules\n", encoding="utf-8")
    completed_tasks = _completed_tasks(stop_reason)
    error_history, recovery_attempted, recovery_last_error = _error_recovery_fields(error_message)
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(
            project_root,
            planning_dir.name,
            cycle=3,
            tokens_used=900,
            stop_reason=stop_reason,
            final_status=stop_reason,
            last_stage_status=stop_reason,
            completed_tasks=completed_tasks,
            error_history=error_history,
            last_error=recovery_last_error,
            verify_setup_recovery_attempted=recovery_attempted,
            verify_setup_recovery_last_error=recovery_last_error,
        ),
    )
    _write_round_record(
        planning_dir,
        stage="VERIFY",
        stage_status="pass" if stop_reason == "PASS" else "setup_error",
        last_error=error_message,
    )


def _completed_tasks(stop_reason: str) -> list[str]:
    if stop_reason == "PASS":
        return ["T001: Implement ranking rules"]
    return []


def _error_recovery_fields(error_message: str) -> tuple[list[str], int, str | None]:
    if not error_message:
        return [], 0, None
    return [error_message], 1, error_message


def test_cli_stability_report_generates_markdown_and_supports_all_runs(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    _write_run_artifacts(
        planning_root / "run-pass",
        project_root=tmp_path,
        stop_reason="PASS",
        error_message="",
    )
    _write_run_artifacts(
        planning_root / "run-blocked",
        project_root=tmp_path,
        stop_reason="HARD_ERROR",
        error_message="VERIFY setup failed: fixture not found",
    )

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
    payload = _run_cli(
        parser,
        capsys,
        [
            "stability-report",
            "--project-root",
            str(tmp_path),
            "--all-runs",
            "--task-max-cycles",
            "8",
            "--token-budget-target",
            "300000",
            "--output",
            str(output_path),
        ],
    )
    assert payload["total_runs"] == 2
    assert payload["project_root"] == str(tmp_path.resolve())
    assert sorted(payload["resolved_planning_dirs"]) == sorted(
        [
            str((planning_root / "run-pass").resolve()),
            str((planning_root / "run-blocked").resolve()),
        ]
    )
    assert payload["run_outcome_counts"]["pass"] == 1
    assert payload["run_outcome_counts"]["stopped:hard_error"] == 1
    assert payload["root_cause_bucket_counts"]["stable_pass"] == 1
    assert payload["root_cause_bucket_counts"]["verify_setup"] == 1
    _assert_provenance_payload(payload, command="stability-report")
    report = output_path.read_text(encoding="utf-8")
    assert "# kodawari 自动化稳定性报告" in report
    assert "## 三、主要阻塞点" in report
    assert "VERIFY Setup Error" in report
    assert "root_cause_bucket_distribution" in report
    assert "merged_absorption_status(sample)" in report
    assert "run-blocked" in report


def test_cli_stability_report_skips_corrupted_runs(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    planning_root = tmp_path / "planning"
    good_dir = planning_root / "run-good"
    bad_dir = planning_root / "run-bad"
    good_dir.mkdir(parents=True)
    bad_dir.mkdir(parents=True)
    (good_dir / "TASKS.md").write_text("## T001: Implement ranking rules\n", encoding="utf-8")
    _write_autopilot_state(good_dir, _base_state_payload(tmp_path, "run-good", completed_tasks=["T001: Implement ranking rules"]))
    (bad_dir / ".autopilot_state.json").write_bytes(b"\x00\xff\x00broken")

    output_path = tmp_path / "AUTOMATION_STABILITY_REPORT.md"
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
    assert payload["total_runs"] == 1
    assert payload["skipped_runs"] == 1
    assert "run-bad" in payload["warnings"][0]
    _assert_provenance_payload(payload, command="stability-report")
    report = output_path.read_text(encoding="utf-8")
    assert "## 数据质量说明" in report
    assert "已跳过 1 个损坏或不可解析的 run" in report


def test_cli_registers_legacy_command_shells() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "compact" in help_text
    assert "research" in help_text
    assert "develop" in help_text
    assert "quick-develop" in help_text
    assert "optimize-existing-develop" in help_text


def _assert_compact_shim_payload(payload: dict[str, Any], *, feature: str) -> None:
    assert payload["command"] == "compact"
    assert payload["compatibility"]["status"] == "COMPAT_SHIM"
    assert payload["deprecation"]["status"] == "deprecated"
    assert payload["deprecation"]["entrypoint"] == "kodawari compact"
    assert payload["deprecation"]["remove_after"] == "2026-08-01"
    assert payload["absorption_status"]["planning_summary"]["status"] == "absorbed"
    assert payload["absorption_status"]["planning_summary"]["status_judgment"] == "已吸收"
    assert payload["absorption_status"]["context_compact"]["status"] == "partial"
    assert payload["absorption_status"]["context_compact"]["status_judgment"] == "部分吸收"
    assert payload["absorption_status"]["instincts"]["status"] == "partial"
    assert payload["absorption_status"]["instincts"]["status_judgment"] == "部分吸收"
    assert payload["merged_absorption_status"] == {"planning_summary": "已吸收", "context_compact": "部分吸收", "instincts": "部分吸收"}
    assert payload["context_compact"]["requested"] is True
    assert payload["context_compact"]["runtime_triggered"] is False
    assert payload["context_compact"]["entrypoint_scope"] == "compat_shim_only"
    assert payload["context_compact"]["status"] == "partial"
    assert payload["context_compact"]["mode"] == "compat"
    assert payload["context_compact"]["merged_absorption_status"] == payload["merged_absorption_status"]
    assert payload["instincts"]["requested"] is True
    assert payload["instincts"]["loaded"] is False
    assert payload["instincts"]["status"] == "store_not_found"
    compact_md = Path(payload["artifacts"]["COMPACT_CONTEXT.md"])
    compact_json = Path(payload["artifacts"]["compact_context.json"])
    assert compact_md.exists()
    assert compact_json.exists()
    assert "Compact Context" in compact_md.read_text(encoding="utf-8")
    compact_payload = json.loads(compact_json.read_text(encoding="utf-8"))
    assert compact_payload["feature"] == feature
    assert compact_payload["compact_status"] == "partial"
    assert compact_payload["compact_mode"] == "compat"
    assert compact_payload["instincts_loaded"] is False
    assert compact_payload["instincts_status"] == "store_not_found"
    assert compact_payload["merged_absorption_status"] == payload["merged_absorption_status"]
    assert "runtime_trigger_event" not in compact_payload
    _assert_provenance_payload(payload, command="compact")


def test_cli_compact_compat_shim_writes_artifacts(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    rc, payload = _run_cli_with_rc(
        parser,
        capsys,
        [
            "compact",
            "--project-root",
            str(tmp_path),
            "--feature",
            "compat-demo",
            "--log-tail-lines",
            "40",
        ],
    )
    assert rc == 0
    assert payload["log_tail_lines"] == 40
    _assert_compact_shim_payload(payload, feature="compat-demo")
    assert payload["provenance"]["project_root"] == str(tmp_path.resolve())


def test_cli_compact_compat_shim_loads_instinct_hints_when_store_exists(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    learn_from_globs(tmp_path, ["planning/*", "src/**/*.py"])

    rc, payload = _run_cli_with_rc(
        parser,
        capsys,
        [
            "compact",
            "--project-root",
            str(tmp_path),
            "--feature",
            "compat-with-instincts",
        ],
    )

    assert rc == 0
    assert payload["compatibility"]["status"] == "COMPAT_SHIM"
    assert payload["context_compact"]["runtime_triggered"] is False
    assert payload["instincts"]["requested"] is True
    assert payload["instincts"]["loaded"] is True
    assert payload["instincts"]["status"] == "loaded"
    assert payload["instincts"]["hints_count"] >= 1
    compact_payload = payload["compact_preview"]["compact_json"]
    assert compact_payload["instincts_loaded"] is True
    assert compact_payload["instincts_status"] == "loaded"
    assert compact_payload["instinct_hints_count"] >= 1
    assert any(item["pattern"] == "planning/*" for item in compact_payload["instinct_hints"])
    _assert_provenance_payload(payload, command="compact")


def test_cli_compact_compat_shim_can_disable_instinct_loading(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    learn_from_globs(tmp_path, ["planning/*"])

    rc, payload = _run_cli_with_rc(
        parser,
        capsys,
        [
            "compact",
            "--project-root",
            str(tmp_path),
            "--feature",
            "compat-no-instincts",
            "--no-include-instincts",
        ],
    )

    assert rc == 0
    assert payload["context_compact"]["runtime_triggered"] is False
    assert payload["instincts"]["requested"] is False
    assert payload["instincts"]["loaded"] is False
    assert payload["instincts"]["status"] == "disabled_by_request"
    compact_payload = payload["compact_preview"]["compact_json"]
    assert compact_payload["instincts_loaded"] is False
    assert compact_payload["instincts_status"] == "disabled_by_request"
    assert compact_payload["instinct_hints_count"] == 0
    _assert_provenance_payload(payload, command="compact")


def _legacy_step(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in payload["steps"] if step["name"] == name)


def _legacy_runtime_has_chain(command: str) -> bool:
    return command in {"research", "develop", "quick-develop", "optimize-existing-develop"}


def _legacy_expected_chain_status(command: str) -> str:
    del command
    return "PASS"


def _assert_autopilot_step_result(
    *,
    payload: dict[str, Any],
    expected_autopilot_status: str,
    autopilot_step: dict[str, Any],
) -> None:
    assert autopilot_step["payload"]["status"] == expected_autopilot_status
    if expected_autopilot_status == "ok":
        assert autopilot_step["rc"] == 0
        assert payload["flow"]["autopilot_run_reason"] == "PROCEED_TO_GATE"
        return
    assert autopilot_step["rc"] != 0
    assert payload["flow"]["autopilot_run_reason"]


def _assert_legacy_runtime_chain_payload(payload: dict[str, Any], *, command: str, workflow_chain: Any) -> None:
    if not _legacy_runtime_has_chain(command):
        assert workflow_chain is None or workflow_chain == {}
        return
    assert isinstance(workflow_chain, dict)
    assert workflow_chain["task_cycle_enabled"] is True
    assert workflow_chain["upstream"]["verify"]["status"] == "PASS"
    assert workflow_chain["upstream"]["gate"]["total_status"] == "PASS"
    assert workflow_chain["upstream"]["approvals"]["all_passed"] is True
    assert payload["workflow_chain"]["final_outcome"]["status"] == _legacy_expected_chain_status(command)
    assert payload["workflow_chain"]["chain_final_outcome"]["status"] == "PASS"


def _assert_legacy_status_step(payload: dict[str, Any], *, command: str) -> None:
    status_step = _legacy_step(payload, "status")
    assert status_step["payload"]["planning_contract"]["version"] == MERGED_CONTRACT_VERSION
    assert status_step["payload"]["state"]["unified_status"]["current_phase"] in {"GATE", "COMPLETED"}
    if _legacy_runtime_has_chain(command):
        assert (
            status_step["payload"]["workflow_chain"]["final_outcome"]["status"]
            == _legacy_expected_chain_status(command)
        )


def _assert_legacy_runtime_base(
    *,
    payload: dict[str, Any],
    command: str,
    feature: str,
    expected_steps: list[str],
    expected_rc: int,
    expected_max_cycles: int,
    expected_autopilot_status: str,
) -> None:
    assert payload["command"] == command
    assert payload["compatibility"]["status"] == "COMPAT_RUNTIME_SHIM"
    assert payload["deprecation"]["status"] == "deprecated"
    assert payload["deprecation"]["entrypoint"] == f"kodawari {command}"
    assert payload["deprecation"]["remove_after"] == "2026-08-01"
    assert payload["max_cycles"] == expected_max_cycles
    assert payload["flow"]["terminal_rc"] == expected_rc
    assert payload["flow"]["executed_steps"] == expected_steps
    assert payload["flow"]["skipped_steps"] == []
    assert [step["name"] for step in payload["steps"]] == expected_steps
    autopilot_step = _legacy_step(payload, "autopilot")
    _assert_autopilot_step_result(
        payload=payload,
        expected_autopilot_status=expected_autopilot_status,
        autopilot_step=autopilot_step,
    )
    workflow_chain = autopilot_step["payload"].get("workflow_chain")
    _assert_legacy_runtime_chain_payload(payload, command=command, workflow_chain=workflow_chain)
    _assert_legacy_status_step(payload, command=command)
    assert "autopilot" in payload["canonical_replacement"]["primary"]
    assert payload["feature"] == feature


def _assert_legacy_runtime_optional_steps(payload: dict[str, Any], expected_steps: list[str]) -> None:
    if "gate" in expected_steps:
        gate_step = _legacy_step(payload, "gate")
        assert gate_step["payload"]["total_status"] in {"PASS", "BLOCKED"}
    if "stability-report" in expected_steps:
        report_step = _legacy_step(payload, "stability-report")
        assert report_step["payload"]["status"] == "ok"
        assert report_step["payload"]["total_runs"] == 1


@pytest.mark.parametrize(
    ("command", "expected_steps", "expected_max_cycles", "expected_rc", "expected_autopilot_status"),
    [
        ("research", ["autopilot", "gate", "status"], 8, 0, "ok"),
        ("develop", ["autopilot", "gate", "status"], 8, 0, "ok"),
        ("quick-develop", ["autopilot", "gate", "status"], 3, 0, "ok"),
        ("optimize-existing-develop", ["autopilot", "gate", "stability-report", "status"], 8, 0, "ok"),
    ],
)
def test_cli_legacy_shells_route_to_canonical_runtime(
    command: str,
    expected_steps: list[str],
    expected_max_cycles: int,
    expected_rc: int,
    expected_autopilot_status: str,
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("legacy runtime shell\n", encoding="utf-8")
    feature = f"{command}-compat-demo"
    rc, payload = _run_cli_with_rc(
        parser,
        capsys,
        [
            command,
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--requirements-file",
            str(requirements_file),
        ],
    )
    assert rc == expected_rc
    _assert_legacy_runtime_base(
        payload=payload,
        command=command,
        feature=feature,
        expected_steps=expected_steps,
        expected_rc=expected_rc,
        expected_max_cycles=expected_max_cycles,
        expected_autopilot_status=expected_autopilot_status,
    )
    _assert_legacy_runtime_optional_steps(payload, expected_steps)
    _assert_provenance_payload(payload, command=command)

    if command == "develop":
        assert payload["workflow_chain"]["mode"] == "peer_review"
        assert payload["workflow_chain"]["task_cycle"]["entered"] is True
        assert payload["workflow_chain"]["task_cycle"]["tasks_completed"] >= 0
    if command == "quick-develop":
        assert payload["workflow_chain"]["mode"] == "single_pass"
        assert payload["workflow_chain"]["peer_review_enabled"] is False
        assert payload["workflow_chain"]["task_cycle"]["entered"] is True
        assert payload["flow"]["final_outcome"]["status"] == "PASS"
        assert payload["flow"]["final_outcome"]["reason"] in {"ALL_TASKS_COMPLETE", "NO_TASKS_FOUND"}
        assert payload["flow"]["chain_final_outcome"]["status"] == "PASS"


def test_cli_develop_overlays_effective_final_outcome_when_gate_blocks(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("legacy runtime shell\n", encoding="utf-8")
    _patch_advisory_gate_block(monkeypatch)

    rc, payload = _run_cli_with_rc(
        parser,
        capsys,
        [
            "develop",
            "--project-root",
            str(tmp_path),
            "--feature",
            "develop-blocked-demo",
            "--requirements-file",
            str(requirements_file),
        ],
    )

    assert rc == 0
    assert payload["flow"]["gate_total_status"] == "BLOCKED"
    assert payload["flow"]["final_outcome"]["status"] == "BLOCKED"
    assert payload["flow"]["final_outcome"]["reason"] == "ADVISORY_GATE_BLOCKED"
    assert payload["flow"]["chain_final_outcome"]["status"] == "PASS"
    assert payload["workflow_chain"]["final_quality_review"]["status"] == "BLOCKED"
    assert payload["workflow_chain"]["final_outcome"]["blocking_reason"] == "src/demo.py: advisory gate blocked on purpose"


def test_cli_develop_resets_stale_autopilot_state_before_runtime(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "develop-stale-reset-demo"
    planning_dir = tmp_path / "planning" / feature
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("legacy stale reset\n", encoding="utf-8")
    _write_single_task_backlog(planning_dir, feature=feature, task_scope="refresh stale runtime")
    _write_stale_develop_state(planning_dir, project_root=tmp_path, feature=feature)

    rc, payload = _run_cli_with_rc(
        parser,
        capsys,
        [
            "develop",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--requirements-file",
            str(requirements_file),
        ],
    )

    assert rc == 0
    _assert_develop_stale_reset(payload)
    state_payload = json.loads((planning_dir / ".autopilot_state.json").read_text(encoding="utf-8"))
    assert int(state_payload["cycle"]) < 99
    assert state_payload["stop_reason"] != "MAX_CYCLES"


def _blocked_autopilot_payload() -> dict[str, Any]:
    return {
        "status": "blocked",
        "run_reason": "MAX_CYCLES_REACHED",
        "workflow_chain": {
            "final_outcome": {
                "status": "BLOCKED",
                "reason": "UPSTREAM_BLOCKED",
                "blocking_reason": "MAX_CYCLES",
            },
            "chain_final_outcome": {
                "status": "BLOCKED",
                "reason": "UPSTREAM_BLOCKED",
                "blocking_reason": "MAX_CYCLES",
            },
            "task_cycle_enabled": True,
            "upstream": {"passed": False},
            "task_cycle": {"entered": False},
        },
    }


def _patch_autopilot_block(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_autopilot(args: Any) -> int:
        del args
        print(json.dumps(_blocked_autopilot_payload()))
        return 1

    monkeypatch.setattr(cli_main, "run_autopilot_command", _fake_autopilot)


def _assert_skipped_gate_and_report(payload: dict[str, Any]) -> None:
    assert payload["flow"]["terminal_rc"] == 1
    assert payload["flow"]["executed_steps"] == ["autopilot", "status"]
    assert payload["flow"]["skipped_steps"] == ["gate", "stability-report"]
    gate_step = _legacy_step(payload, "gate")
    report_step = _legacy_step(payload, "stability-report")
    assert gate_step["skipped"] is True
    assert report_step["skipped"] is True
    assert gate_step["reason"] == "autopilot_not_successful"
    assert report_step["reason"] == "autopilot_not_successful"


def test_cli_optimize_existing_develop_skips_gate_and_report_when_autopilot_blocks(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = build_parser()
    requirements_file = tmp_path / "requirements.txt"
    requirements_file.write_text("legacy runtime shell\n", encoding="utf-8")
    _patch_autopilot_block(monkeypatch)

    rc, payload = _run_cli_with_rc(
        parser,
        capsys,
        [
            "optimize-existing-develop",
            "--project-root",
            str(tmp_path),
            "--feature",
            "optimize-blocked-demo",
            "--requirements-file",
            str(requirements_file),
        ],
    )

    assert rc == 1
    _assert_skipped_gate_and_report(payload)


def test_cli_main_blocks_mutating_command_when_repo_resolution_mismatched(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repo = tmp_path / "target-repo"
    (target_repo / "scripts").mkdir(parents=True, exist_ok=True)
    (target_repo / "scripts" / "kodawari.ps1").write_text("param()\n", encoding="utf-8")
    module_repo = tmp_path / "old-install-repo"

    monkeypatch.setattr(cli_main, "_warn_if_repo_resolution_mismatch", lambda: None)
    monkeypatch.setattr(cli_main, "find_kodawari_repo_root", lambda _: target_repo)
    monkeypatch.setattr(cli_main, "_mismatched_module_repo", lambda _: module_repo)
    monkeypatch.setattr(cli_main, "resolved_wrapper_repo_root", lambda: None)

    rc = cli_main.main(["autopilot", "--project-root", str(tmp_path), "--feature", "guard-demo"])
    payload = json.loads(capsys.readouterr().out)

    assert rc == 2
    assert payload["error"] == "repo_resolution_mismatch"
    assert payload["command"] == "autopilot"
    assert payload["cwd_repo_root"] == str(target_repo)
    assert payload["module_repo_root"] == str(module_repo)
    assert payload["recommended_entrypoint"].endswith("scripts\\kodawari.ps1")


def test_cli_status_handles_partial_contract_first_planning_dir_without_crashing(
    tmp_path: Path, capsys: Any
) -> None:
    """Regression: status must not KeyError on a partial contract-first planning
    dir (e.g. one that contains only PLANNING_CONVERSATION.json from a failed
    autopilot run). The two artifact registries (_STATUS_ARTIFACT_ORDER and
    required_artifacts from build_contract_first_planning_status) used to drift
    and crashed when required_artifacts referenced a name missing from the
    status order tuple."""
    import json as _json

    parser = build_parser()
    planning_dir = tmp_path / "planning" / "partial-planning"
    _write_autopilot_state(
        planning_dir,
        _base_state_payload(tmp_path, "partial-planning"),
    )
    # Write a minimal but schema-valid PLANNING_CONVERSATION.json so that
    # detect_status_planning_mode classifies this dir as contract_first.
    (planning_dir / "PLANNING_CONVERSATION.json").write_text(
        _json.dumps(
            {
                "schema_version": "planning.conversation.v1",
                "input_fingerprint": "sha256:partial",
                "status": "error",
                "final_plan": {"tasks": []},
                "approval": {
                    "decision": "human_required",
                    "reason": "partial_run",
                    "checks": {},
                },
            }
        ),
        encoding="utf-8",
    )

    payload = _run_cli(
        parser, capsys, ["status", "--project-root", str(tmp_path), "--feature", "partial-planning"]
    )

    # Status must succeed and surface PLANNING_CONVERSATION.json semantic.
    planning_contract = payload.get("planning_contract") or {}
    semantics = planning_contract.get("artifact_semantics") or {}
    assert "PLANNING_CONVERSATION.json" in (planning_contract.get("required_artifacts") or [])
    assert semantics.get("PLANNING_CONVERSATION.json") == "model_driven_planning_conversation_payload"

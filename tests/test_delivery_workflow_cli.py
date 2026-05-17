import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.architecture_plan import build_architecture_plan
from kodawari.autopilot.prd_contract import build_prd_intake
from kodawari.autopilot.repo_inventory import build_repo_inventory
from kodawari.cli import delivery_review
from kodawari.cli import delivery_workflow
from kodawari.cli.main import build_parser


def _default_verify_check(*, status: str = "PASS", verify_cmd: str = "pytest -q") -> dict[str, Any]:
    return {
        "status": status,
        "passed": status == "PASS",
        "mode": "command",
        "source": "verify_command",
        "verify_cmd": verify_cmd,
        "verify_cmd_resolved": verify_cmd,
        "verify_target_source": "explicit_command" if verify_cmd != "pytest -q" else "default",
        "verify_targets": [],
        "summary": "" if status == "PASS" else "verify blocked",
        "blocking_reason": "" if status == "PASS" else "verify blocked",
        "command_executed": verify_cmd != "pytest -q",
        "artifacts": [],
        "returncode": 0 if status == "PASS" else 2,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }


def _surface_verify_check(*, verify_cmd: str, changed_files: list[str]) -> dict[str, Any]:
    return {
        "status": "PASS",
        "passed": True,
        "mode": "command",
        "source": "verify_command",
        "verify_cmd": verify_cmd,
        "verify_cmd_resolved": verify_cmd,
        "verify_target_source": "surface_recipe",
        "verify_targets": list(changed_files),
        "summary": "ok",
        "blocking_reason": "",
        "command_executed": True,
        "artifacts": list(changed_files),
        "returncode": 0,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }


def _write_verify_report(
    planning_dir: Path,
    *,
    feature: str,
    status: str = "PASS",
    verify_cmd: str = "pytest -q",
    changed_files: list[str] | None = None,
    input_confidence: str = "curated",
    requested_command_kind: str | None = None,
) -> None:
    items = list(changed_files or [])
    _write_json(
        planning_dir / ".verify_report.json",
        {
            "schema_version": "verify.report.v1",
            "generated_at": "2026-03-25T00:00:00+00:00",
            "feature": feature,
            "planning_dir": str(planning_dir.resolve()),
            "entrypoint": "kodawari verify",
            "requested_command": verify_cmd,
            "requested_command_kind": requested_command_kind or ("default" if verify_cmd == "pytest -q" else "inline"),
            "changed_files": {
                "source": "cli_override" if items else "default",
                "items": items,
                "count": len(items),
            },
            "input_confidence": input_confidence,
            "status": status,
            "verify_check": _default_verify_check(status=status, verify_cmd=verify_cmd),
        },
    )


def _write_review_evidence_artifact(
    planning_dir: Path,
    *,
    feature: str,
    status: str = "PASS",
    self_reviews: int = 1,
    peer_reviews: int = 1,
) -> None:
    _write_json(
        planning_dir / ".review_evidence.json",
        {
            "schema_version": "review.evidence.v1",
            "generated_at": "2026-03-25T00:00:00+00:00",
            "feature": feature,
            "planning_dir": str(planning_dir.resolve()),
            "entrypoint": "kodawari review-evidence",
            "status": status,
            "blocking_reason": "" if status == "PASS" else "review evidence blocked",
            "checks": {
                "self_review_count": self_reviews,
                "peer_review_count": peer_reviews,
                "must_fix_remaining": 0,
            },
            "issues": [] if status == "PASS" else ["review evidence blocked"],
            "evidence": [],
        },
    )


def _write_execution_result(
    planning_dir: Path,
    *,
    feature: str,
    status: str = "PASS",
    backend: str = "external_cli",
    changed_files: list[str] | None = None,
    guard_action: str = "",
    guard_policy: str = "",
    guard_pattern: str = "",
    guard_command: str = "",
) -> None:
    guard_decision = {}
    if any((guard_action, guard_policy, guard_pattern, guard_command)):
        guard_decision = {
            "action": guard_action,
            "reason": guard_policy,
            "pattern": guard_pattern,
            "command": guard_command,
        }
    _write_json(
        planning_dir / ".execution_result.json",
        {
            "schema_version": "execution.result.v1",
            "feature": feature,
            "task": "T1: scoped task",
            "backend": backend,
            "status": status,
            "changed_files": list(changed_files or []),
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "returncode": 0 if status == "PASS" else 2,
            "artifacts": list(changed_files or []),
            "error_code": "",
            "blocking_reason": "" if status == "PASS" else "execution blocked",
            "summary": "executor completed" if status == "PASS" else "execution blocked",
            "guard_action": guard_action,
            "guard_policy": guard_policy,
            "guard_pattern": guard_pattern,
            "guard_command": guard_command,
            "guard_decision": guard_decision,
        },
    )


def _run_cli(parser: Any, capsys: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    args = parser.parse_args(argv)
    rc = int(args.handler(args))
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_worktree_baseline(
    planning_dir: Path,
    *,
    feature: str,
    dirty_files: list[str],
    core_dirty_files: list[str] | None = None,
) -> None:
    payload = {
        "schema_version": "worktree.baseline.v1",
        "captured_at": "2026-03-23T00:00:00+00:00",
        "feature": feature,
        "planning_dir": str(planning_dir.resolve()),
        "command": "task-run",
        "mode": "warn",
        "status": "WARN" if dirty_files else "PASS",
        "dirty_files": dirty_files,
        "tracked_dirty_files": dirty_files,
        "untracked_files": [],
        "allowed_files": [],
        "core_dirty_files": list(core_dirty_files or []),
        "details": "Pre-existing dirty worktree files detected.",
    }
    _write_json(planning_dir / ".worktree_baseline.json", payload)


def _write_review_result(
    planning_dir: Path,
    *,
    status: str = "PASS",
    evidence_status: str = "PASS",
    self_reviews: int = 1,
    peer_reviews: int = 1,
    issues: list[str] | None = None,
    blocking_reason: str | None = None,
    changed_files: list[str] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "review_evidence": {
            "status": evidence_status,
            "checks": {
                "self_review_count": self_reviews,
                "peer_review_count": peer_reviews,
            },
        },
    }
    if changed_files:
        payload["changed_files"] = {
            "source": ".review_result.json.changed_files",
            "items": list(changed_files),
            "count": len(changed_files),
        }
    if blocking_reason:
        payload["review_evidence"]["blocking_reason"] = blocking_reason
    if issues:
        payload["review_evidence"]["issues"] = issues
    _write_json(planning_dir / ".review_result.json", payload)


def _prepare_planning_core(planning_dir: Path, *, feature: str) -> None:
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "PLAN.md").write_text(f"# PLAN ({feature})\n", encoding="utf-8")
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [ ] T001: Update src/service.py behavior",
                "- [ ] T002: Add tests/tests_service.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (planning_dir / "ACCEPTANCE.md").write_text(
        "\n".join(
            [
                f"# ACCEPTANCE ({feature})",
                "",
                "- [ ] Behavior updated",
                "- [ ] Scoped tests pass",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (planning_dir / "GATE.md").write_text(f"# GATE ({feature})\n", encoding="utf-8")


def _prepare_contract_first_planning_core(planning_dir: Path, *, feature: str, files_to_change: list[str] | None = None) -> None:
    files = list(files_to_change or ["app/main.py", "tests/test_api.py"])
    planning_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        planning_dir / "PRD_INTAKE.json",
        {
            "schema_version": "contract_first.prd_intake.v1",
            "generated_at": "2026-03-24T00:00:00+00:00",
            "feature": feature,
            "business_outcome": "Return hydration goal history in one response.",
            "source_of_truth": ["db.hydration_goals", "db.goal_change_events"],
            "source_of_truth_canonical": ["db.hydration_goals", "db.goal_change_events"],
            "path_type": "read",
            "layers": ["repository", "service", "route"],
            "coverage_hints": [
                "path:read",
                "layer:repository",
                "layer:service",
                "layer:route",
                "sot:db.hydration_goals",
                "sot:db.goal_change_events",
            ],
            "out_of_scope": ["Do not change reminder generation"],
            "confidence": "high",
            "confidence_issues": [],
        },
    )
    _write_json(
        planning_dir / "TASK_GRAPH.json",
        {
            "schema_version": "contract_first.task_graph.v1",
            "generated_at": "2026-03-24T00:00:00+00:00",
            "business_outcome": "Return hydration goal history in one response.",
            "project_layout": {"kind": "app", "code_roots": ["app"], "test_roots": ["tests"], "workspace_roots": []},
            "project_profile": "fastapi",
            "coverage_hints": [
                "path:read",
                "layer:repository",
                "layer:service",
                "layer:route",
                "sot:db.hydration_goals",
                "sot:db.goal_change_events",
            ],
            "boundary_debt": {"status": "PASS", "details": "Logical layer ownership maps cleanly to distinct source files.", "items": []},
            "executability": {"status": "PASS", "issues": []},
            "tasks": [
                {
                    "task_id": "T1",
                    "task_name": "Implement service business logic",
                    "depends_on": [],
                    "core_files": files[:3],
                    "layer_owner": "service",
                    "invariants": ["No second source of truth is introduced."],
                    "test_proof": "Run scoped tests.",
                    "coverage_hints": ["layer:service", "path:read", "sot:db.hydration_goals", "sot:db.goal_change_events"],
                    "executability": {"status": "PASS", "issues": []},
                }
            ],
        },
    )
    _write_json(
        planning_dir / "TASK_CARD_ACTIVE.json",
        {
            "schema_version": "contract_first.task_card.v1",
            "generated_at": "2026-03-24T00:00:00+00:00",
            "task_id": "T1",
            "task_name": "Implement service business logic",
            "why_this_layer": "This task belongs to service layer because it owns the behavior.",
            "files_to_change": files[:3],
            "invariants": ["No second source of truth is introduced."],
            "test_plan": "Run scoped tests.",
            "forbidden_changes": ["Do not refactor unrelated modules."],
            "requires": [],
        },
    )


def _prepare_fullstack_contract_first_repo(tmp_path: Path, *, feature: str) -> Path:
    planning_dir = tmp_path / "planning" / feature
    backend_files = ["backend/app/main.py", "backend/tests/test_api.py"]
    frontend_files = ["web/src/App.js", "web/src/App.test.js"]
    _prepare_contract_first_planning_core(
        planning_dir,
        feature=feature,
        files_to_change=backend_files + frontend_files,
    )
    (tmp_path / "backend" / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "app" / "main.py").write_text("def handler():\n    return 'ok'\n", encoding="utf-8")
    (tmp_path / "backend" / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    (tmp_path / "web" / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "web" / "src" / "App.js").write_text("export function App() { return null; }\n", encoding="utf-8")
    (tmp_path / "web" / "src" / "App.test.js").write_text("console.log('ok')\n", encoding="utf-8")
    (tmp_path / "web" / "package.json").write_text(
        json.dumps({"name": "demo", "private": True, "scripts": {"test": "node src/App.test.js"}}),
        encoding="utf-8",
    )
    prd_intake = build_prd_intake(
        "\n".join(
            [
                "Return backend data and show it in frontend.",
                "source of truth: db.widgets",
                "layer: service route frontend",
            ]
        ),
        feature=feature,
    )
    repo_inventory = build_repo_inventory(
        project_root=tmp_path,
        archetype="fullstack_fastapi_react",
        capabilities=[],
        mode="existing",
    )
    architecture_plan = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=prd_intake,
        repo_inventory=repo_inventory,
        planning_mode="existing",
    )
    _write_json(planning_dir / "REPO_INVENTORY.json", repo_inventory)
    _write_json(planning_dir / "ARCHITECTURE_PLAN.json", architecture_plan)
    return planning_dir


def _prepare_task_granularity_verify_repo(tmp_path: Path, *, feature: str) -> Path:
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(
        planning_dir,
        feature=feature,
        files_to_change=["app/schemas.py", "tests/test_api.py"],
    )
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "schemas.py").write_text("class MedicationAdherenceSummary: ...\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text(
        "\n".join(
            [
                "def test_medication_adherence_summary_schema_contract():",
                "    assert True",
                "",
                "def test_medication_adherence_summary_schema_defaults():",
                "    assert True",
                "",
                "def test_hydration_unrelated_case():",
                "    assert False",
                "",
            ]
        ),
        encoding="utf-8",
    )
    prd_intake = build_prd_intake(
        "\n".join(
            [
                "Return medication adherence summary schema in one response.",
                "source of truth: db.medications",
                "layer: schema route",
            ]
        ),
        feature=feature,
    )
    repo_inventory = build_repo_inventory(
        project_root=tmp_path,
        archetype="fastapi_api",
        capabilities=[],
        mode="existing",
    )
    architecture_plan = build_architecture_plan(
        project_root=tmp_path,
        prd_intake=prd_intake,
        repo_inventory=repo_inventory,
        planning_mode="existing",
    )
    _write_json(planning_dir / "REPO_INVENTORY.json", repo_inventory)
    _write_json(planning_dir / "ARCHITECTURE_PLAN.json", architecture_plan)
    return planning_dir


def _write_minimal_telemetry_snapshot(planning_dir: Path, *, feature: str) -> None:
    snapshot = {
        "schema_version": "telemetry.snapshot.v1",
        "captured_at": "2026-03-21T00:00:00+00:00",
        "feature": feature,
        "run_id": feature,
        "status": "PASS",
        "metrics": {
            "tokens_used": 100,
            "cycle": 2,
            "changed_files_count": 1,
            "error_events_count": 0,
            "review_rounds_used": 0,
            "history_events_considered": 0,
        },
        "signals": {
            "stop_reason": "PASS",
            "gate_status": "PASS",
            "verify_status": "PASS",
            "round_outcome": "ready_for_gate",
            "reasoning_tier": "standard",
            "effort_score": 0,
            "effort_reasons": ["minimal fixture"],
        },
        "source_artifacts": {
            "autopilot_state": str((planning_dir / ".autopilot_state.json").resolve()),
            "autopilot_rounds": str((planning_dir / ".autopilot_rounds.jsonl").resolve()),
            "workflow_chain": str((planning_dir / ".workflow_chain.json").resolve()),
            "gate_result": str((planning_dir / ".gate_result.json").resolve()),
        },
        "changed_files": ["app/main.py"],
    }
    _write_json(planning_dir / ".telemetry_snapshot.json", snapshot)


def test_cli_help_includes_delivery_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "review" in help_text
    assert "qa" in help_text
    assert "ship-readiness" in help_text


def test_cli_verify_selects_frontend_surface_from_changed_files(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "verify-frontend-surface"
    planning_dir = _prepare_fullstack_contract_first_repo(tmp_path, feature=feature)
    _write_review_result(
        planning_dir,
        changed_files=["web/src/App.js", "web/src/App.test.js"],
    )

    def _fake_build_verify_check(*, verify_cmd: str, changed_files: list[str], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return _surface_verify_check(verify_cmd=verify_cmd, changed_files=changed_files)

    monkeypatch.setattr("kodawari.autopilot.verify_surfaces.build_verify_check", _fake_build_verify_check)
    rc, payload = _run_cli(
        parser,
        capsys,
        ["verify", "--project-root", str(tmp_path), "--feature", feature],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["verify_scope_mode"] == "surface_plan"
    assert [item["surface"] for item in payload["surface_results"]] == ["frontend"]
    assert payload["surface_summary"]["required_surfaces"] == ["frontend"]


def test_cli_verify_selects_multiple_surfaces_when_changed_files_span_backend_and_frontend(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "verify-multi-surface"
    planning_dir = _prepare_fullstack_contract_first_repo(tmp_path, feature=feature)
    _write_review_result(
        planning_dir,
        changed_files=["backend/app/main.py", "web/src/App.js"],
    )

    def _fake_build_verify_check(*, verify_cmd: str, changed_files: list[str], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return _surface_verify_check(verify_cmd=verify_cmd, changed_files=changed_files)

    monkeypatch.setattr("kodawari.autopilot.verify_surfaces.build_verify_check", _fake_build_verify_check)
    rc, payload = _run_cli(
        parser,
        capsys,
        ["verify", "--project-root", str(tmp_path), "--feature", feature],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert {item["surface"] for item in payload["surface_results"]} == {"backend", "frontend"}


def test_cli_verify_uses_custom_surface_for_command_file(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "verify-command-file"
    planning_dir = _prepare_fullstack_contract_first_repo(tmp_path, feature=feature)
    _write_review_result(planning_dir, changed_files=["web/src/App.js"])
    script_path = tmp_path / "scripts" / "verify.cmd"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    def _fake_build_verify_check(*, verify_cmd: str, changed_files: list[str], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return _surface_verify_check(verify_cmd=verify_cmd, changed_files=changed_files)

    monkeypatch.setattr("kodawari.autopilot.verify_surfaces.build_verify_check", _fake_build_verify_check)
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--command-file",
            "scripts/verify.cmd",
        ],
    )

    assert rc == 0
    assert payload["requested_command_kind"] == "file"
    assert payload["verify_scope_mode"] == "custom"
    assert payload["surface_results"][0]["surface"] == "custom"


def test_cli_verify_narrows_backend_surface_to_feature_keyword(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "medication-adherence-summary"
    planning_dir = _prepare_task_granularity_verify_repo(tmp_path, feature=feature)
    _write_review_result(
        planning_dir,
        changed_files=["app/schemas.py", "tests/test_api.py"],
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["verify", "--project-root", str(tmp_path), "--feature", feature],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["verify_scope_mode"] == "surface_plan"
    surface = payload["surface_results"][0]
    assert surface["surface"] == "backend"
    assert surface["verify_target_source"] == "task_keyword_match"
    assert surface["verify_keyword_expression"] == "medication_adherence_summary"
    assert '-k "medication_adherence_summary"' in surface["verify_cmd_resolved"]


def test_cli_verify_blocks_when_surface_mapping_is_ambiguous(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "verify-surface-ambiguous"
    planning_dir = _prepare_fullstack_contract_first_repo(tmp_path, feature=feature)
    (planning_dir / "TASK_CARD_ACTIVE.json").unlink()

    rc, payload = _run_cli(
        parser,
        capsys,
        ["verify", "--project-root", str(tmp_path), "--feature", feature],
    )

    assert rc == 0
    assert payload["status"] == "BLOCKED"
    assert payload["verify_check"]["source"] == "verify_surface_ambiguous"


def test_cli_qa_blocks_when_verify_surface_coverage_is_inconsistent(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-surface-mismatch"
    planning_dir = _prepare_fullstack_contract_first_repo(tmp_path, feature=feature)
    _write_review_result(planning_dir, changed_files=["web/src/App.js"])
    _write_execution_result(
        planning_dir,
        feature=feature,
        changed_files=["web/src/App.js"],
    )
    _write_review_evidence_artifact(planning_dir, feature=feature)
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(
        planning_dir / ".verify_report.json",
        {
            "schema_version": "verify.report.v1",
            "generated_at": "2026-03-25T00:00:00+00:00",
            "feature": feature,
            "planning_dir": str(planning_dir.resolve()),
            "entrypoint": "kodawari verify",
            "requested_command": "pytest -q",
            "requested_command_kind": "default",
            "changed_files": {"source": ".review_result.json.changed_files", "items": ["web/src/App.js"], "count": 1},
            "input_confidence": "curated",
            "status": "PASS",
            "verify_scope_mode": "surface_plan",
            "surface_results": [{"surface": "backend", "status": "PASS"}],
            "surface_summary": {"required_surfaces": ["backend"], "available_surfaces": ["backend", "frontend"]},
            "verify_check": _default_verify_check(status="PASS"),
        },
    )

    rc, payload = _run_cli(parser, capsys, ["qa", "--project-root", str(tmp_path), "--feature", feature])

    assert rc == 0
    assert payload["status"] == "BLOCKED"
    assert payload["checks"]["surface_coverage_consistency"]["status"] == "FAIL"


def test_cli_qa_accepts_execution_scoped_verify_custom_evidence(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-execution-scoped-verify"
    planning_dir = _prepare_fullstack_contract_first_repo(tmp_path, feature=feature)
    changed_files = ["web/src/App.js"]
    _write_review_result(planning_dir, changed_files=changed_files)
    _write_execution_result(planning_dir, feature=feature, changed_files=changed_files)
    _write_review_evidence_artifact(planning_dir, feature=feature)
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    scoped_command = "python -m pytest tests/test_t077_external_trends_frontend_contract.py -q"
    _write_json(
        planning_dir / ".verify_report.json",
        {
            "schema_version": "verify.report.v1",
            "generated_at": "2026-03-25T00:00:00+00:00",
            "feature": feature,
            "planning_dir": str(planning_dir.resolve()),
            "entrypoint": "kodawari verify",
            "requested_command": scoped_command,
            "requested_command_kind": "inline",
            "changed_files": {"source": ".execution_result.json.changed_files", "items": changed_files, "count": 1},
            "input_confidence": "curated",
            "status": "PASS",
            "verify_scope_mode": "custom",
            "surface_results": [],
            "surface_summary": {"selection_source": ".execution_result.json.verify_summary"},
            "verify_check": _default_verify_check(status="PASS", verify_cmd=scoped_command),
        },
    )

    rc, payload = _run_cli(parser, capsys, ["qa", "--project-root", str(tmp_path), "--feature", feature])

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["checks"]["surface_coverage_consistency"]["status"] == "PASS"
    assert "custom command" in payload["checks"]["surface_coverage_consistency"]["details"]


def test_cli_review_blocks_when_source_changes_without_tests(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-blocked-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "feature": feature,
            "architecture_decisions": [
                {
                    "id": "D-1",
                    "decision": "service keeps write-path logic",
                    "rationale": "prevent route layer leakage",
                }
            ],
            "changed_files": ["src/service.py"],
        },
    )
    _write_execution_result(
        planning_dir,
        feature=feature,
        changed_files=["src/service.py"],
        guard_action="ask",
        guard_policy="Push requires confirmation",
        guard_pattern="git\\s+push",
        guard_command="git push origin main",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "src/service.py",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["checks"]["missing_tests"]["status"] == "FAIL"
    assert payload["execution_guard"]["action"] == "ask"
    assert payload["execution_guard"]["policy"] == "Push requires confirmation"
    assert (planning_dir / "REVIEW.md").exists()
    assert (planning_dir / ".review_result.json").exists()
    assert (planning_dir / "DESIGN.md").exists()
    review_result = json.loads((planning_dir / ".review_result.json").read_text(encoding="utf-8"))
    assert review_result["provenance"]["command"] == "review"
    assert review_result["payload_digest"]
    review_md = (planning_dir / "REVIEW.md").read_text(encoding="utf-8")
    assert "## Mirror Provenance" in review_md
    assert review_result["payload_digest"] in review_md
    assert "execution_guard_action: ask" in review_md
    assert "execution_guard_command: git push origin main" in review_md


def test_cli_review_recognizes_app_layout_source_and_tests(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-app-layout-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature, "changed_files": ["app/main.py", "tests/test_api.py"]})

    rc, payload = _run_cli(
        parser,
        capsys,
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
            "--scope-allow",
            "app/main.py",
            "--scope-allow",
            "tests/test_api.py",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["checks"]["missing_tests"]["status"] == "PASS"
    assert payload["changed_files"]["source"] == "cli_override"
    assert "app/main.py" in payload["checks"]["missing_tests"]["changed_source_files"]
    assert "tests/test_api.py" in payload["checks"]["missing_tests"]["changed_test_files"]


def test_cli_review_recognizes_app_layout_source_with_tests(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-app-layout-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "feature": feature,
            "changed_files": ["app/main.py", "tests/test_api.py"],
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
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
            "--scope-allow",
            "app/main.py",
            "--scope-allow",
            "tests/test_api.py",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["checks"]["missing_tests"]["status"] == "PASS"
    assert payload["checks"]["missing_tests"]["changed_source_files"] == ["app/main.py"]
    assert payload["checks"]["missing_tests"]["changed_test_files"] == ["tests/test_api.py"]


def test_cli_review_changed_file_override_takes_precedence(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-override-precedence"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(
        planning_dir / ".autopilot_state.json",
        {"feature": feature, "changed_files": ["app/main.py", "tests/test_api.py"]},
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "app/main.py",
            "--scope-allow",
            "app/main.py",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["changed_files"]["source"] == "cli_override"
    assert payload["checks"]["missing_tests"]["status"] == "FAIL"
    assert payload["checks"]["missing_tests"]["changed_source_files"] == ["app/main.py"]
    assert payload["checks"]["missing_tests"]["changed_test_files"] == []


def test_cli_review_prefers_state_task_delta_before_override_or_gitdiff(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "review-state-task-delta"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "feature": feature,
            "task_delta_changed_files": ["src/new_service.py", "tests/test_new_service.py"],
            "changed_files": ["src/stale.py", "tests/test_stale.py"],
        },
    )
    monkeypatch.setattr(delivery_review, "git_base_branch_diff_files", lambda *_args, **_kwargs: ["src/git_diff_only.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "src/override_only.py",
            "--scope-allow",
            "src/new_service.py",
            "--scope-allow",
            "tests/test_new_service.py",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["changed_files"]["source"] == "state.task_delta_changed_files"
    assert payload["changed_files"]["items"] == ["src/new_service.py", "tests/test_new_service.py"]


def test_cli_review_prefers_execution_result_changed_files_over_state_override_and_gitdiff(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "review-execution-truth"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_handler():\n    assert 1 == 1\n", encoding="utf-8")
    _write_execution_result(planning_dir, feature=feature, changed_files=["app/main.py", "tests/test_api.py"])
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "feature": feature,
            "task_delta_changed_files": ["app/stale.py"],
            "changed_files": ["app/stale.py"],
        },
    )
    monkeypatch.setattr(delivery_review, "git_base_branch_diff_files", lambda *_args, **_kwargs: ["newsapp/app.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "kodawari/src/kodawari/cli/review_cmd.py",
            "--scope-allow",
            "app/main.py",
            "--scope-allow",
            "tests/test_api.py",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["changed_files"]["source"] == ".execution_result.json.changed_files"
    assert payload["changed_files"]["items"] == ["app/main.py", "tests/test_api.py"]


def test_cli_review_uses_state_changed_files_when_task_delta_is_missing(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "review-state-changed-fallback"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_handler():\n    assert 1 == 1\n", encoding="utf-8")
    _write_json(
        planning_dir / ".autopilot_state.json",
        {
            "feature": feature,
            "changed_files": ["app/main.py", "tests/test_api.py"],
        },
    )
    monkeypatch.setattr(delivery_review, "git_base_branch_diff_files", lambda *_args, **_kwargs: ["newsapp/app.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--scope-allow",
            "app/main.py",
            "--scope-allow",
            "tests/test_api.py",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["changed_files"]["source"] == "state.changed_files"
    assert payload["changed_files"]["items"] == ["app/main.py", "tests/test_api.py"]


def test_cli_review_falls_back_to_project_root_git_diff_without_cross_repo_pollution(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "review-project-root-gitdiff"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(
        delivery_review,
        "git_base_branch_diff_files",
        lambda *_args, **_kwargs: ["app/main.py"],
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--scope-allow",
            "app/main.py",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["changed_files"]["source"] == "git_diff:project_root"
    assert payload["changed_files"]["items"] == ["app/main.py"]


def test_cli_review_dirty_worktree_warns_without_blocking(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "review-dirty-worktree-warn"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature, "changed_files": ["app/main.py", "tests/test_api.py"]})
    _write_worktree_baseline(
        planning_dir,
        feature=feature,
        dirty_files=["app/main.py"],
        core_dirty_files=["app/main.py"],
    )

    rc, payload = _run_cli(
        parser,
        capsys,
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
            "--scope-allow",
            "app/main.py",
            "--scope-allow",
            "tests/test_api.py",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["checks"]["dirty_worktree"]["status"] == "WARN"
    assert payload["checks"]["dirty_worktree"]["dirty_files"] == ["app/main.py"]
    assert "dirty_worktree: WARN" in (planning_dir / "REVIEW.md").read_text(encoding="utf-8")


def test_cli_qa_generates_pass_report_and_artifact(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-pass-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS", "profile": {"name": "advisory"}})
    _write_json(
        planning_dir / ".workflow_chain.json",
        {
            "upstream": {"verify": {"status": "PASS"}},
            "final_quality_review": {"status": "PASS", "review_source": "workflow_chain_aggregation"},
        },
    )
    _write_json(planning_dir / ".review_result.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": [], "verify_check_status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_execution_result(planning_dir, feature=feature, changed_files=["src/service.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "qa",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["checks"]["execution"]["status"] == "PASS"
    assert payload["checks"]["verify"]["status"] == "PASS"
    assert payload["checks"]["gate"]["status"] == "PASS"
    assert (planning_dir / "QA_REPORT.md").exists()
    assert (planning_dir / ".qa_report.json").exists()


def test_cli_verify_materializes_canonical_verify_artifact(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "verify-canonical-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature, files_to_change=["tests/test_api.py"])
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature, "changed_files": ["tests/test_api.py"]})
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "tests/test_api.py",
            "--command",
            "python -c \"print('verify ok')\"",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert (planning_dir / ".verify_report.json").exists()
    verify_artifact = json.loads((planning_dir / ".verify_report.json").read_text(encoding="utf-8"))
    assert verify_artifact["schema_version"] == "verify.report.v1"
    assert verify_artifact["verify_check"]["status"] == "PASS"
    assert verify_artifact["input_confidence"] == "explicit"


def test_cli_review_evidence_writes_canonical_artifact(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-evidence-cli"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "review_evidence_input.json"
    _write_json(
        input_path,
        {
            "status": "PASS",
            "checks": {"self_review_count": 1, "peer_review_count": 1, "must_fix_remaining": 0},
            "issues": [],
            "evidence": [],
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review-evidence",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--input",
            str(input_path),
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert (planning_dir / ".review_evidence.json").exists()


def test_cli_verify_prefers_review_changed_files_truth(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "verify-review-truth"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    _write_json(
        planning_dir / ".task_run_result.json",
        {
            "task_delta_changed_files": ["app/other.py"],
            "changed_files": ["app/other.py"],
        },
    )
    _write_review_result(planning_dir, changed_files=["app/main.py"])
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature, "changed_files": ["app/stale.py"]})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
        ],
    )

    assert rc == 0
    assert payload["changed_files"]["source"] == ".review_result.json.changed_files"
    assert payload["changed_files"]["items"] == ["app/main.py"]
    assert payload["input_confidence"] == "curated"


def test_cli_verify_ignores_stale_review_result_and_uses_authoritative_state_changed_files(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "verify-stale-review-result"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    _write_json(
        planning_dir / ".autopilot_state.json",
        {"feature": feature, "changed_files": ["app/main.py"]},
    )
    _write_review_result(planning_dir, changed_files=["app/stale.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
        ],
    )

    assert rc == 0
    assert payload["changed_files"]["source"] == "state.changed_files"
    assert payload["changed_files"]["items"] == ["app/main.py"]
    assert payload["input_confidence"] == "fallback"


def test_cli_verify_blocks_on_weak_fallback_inputs(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "verify-fallback-block"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature, "changed_files": ["app/main.py"]})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["input_confidence"] == "fallback"
    assert payload["verify_check"]["source"] == "verify_input_determinism"


def test_cli_qa_prefers_canonical_verify_report_without_task_run_result(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-canonical-verify"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".review_result.json", {"status": "PASS"})
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_verify_report(planning_dir, feature=feature, status="PASS")
    _write_execution_result(planning_dir, feature=feature, changed_files=["app/main.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "qa",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["checks"]["execution"]["source"] == ".execution_result.json"
    assert payload["checks"]["verify"]["source"] == ".verify_report.json"


def test_cli_qa_blocks_when_execution_artifact_is_missing(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-missing-execution"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".review_result.json", {"status": "PASS"})
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_verify_report(planning_dir, feature=feature, status="PASS")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "qa",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["checks"]["execution"]["execution_status"] == "MISSING"
    assert "execution:" in payload["blocking_reason"]


def test_cli_review_prefers_canonical_review_evidence_artifact(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-canonical-evidence"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature, files_to_change=["app/main.py", "tests/test_api.py"])
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    _write_json(
        planning_dir / ".task_run_result.json",
        {
            "review_evidence": {
                "status": "FAIL",
                "checks": {"self_review_count": 0, "peer_review_count": 0},
                "issues": ["legacy payload should not win"],
            }
        },
    )
    _write_review_evidence_artifact(planning_dir, feature=feature, status="PASS")
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
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
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["review_evidence_source"] == ".review_evidence.json"
    assert payload["review_evidence_status"] == "PASS"


def test_cli_review_contract_first_uses_task_card_scope_truth(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-contract-first-scope"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature, files_to_change=["app/main.py", "tests/test_api.py"])
    _write_json(
        planning_dir / ".task_run_result.json",
        {
            "changed_files": ["app/other.py"],
            "task_delta_changed_files": ["app/other.py"],
            "verify_check": {"status": "PASS"},
            "gate_check": {"total_status": "PASS"},
        },
    )
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (tmp_path / "app" / "other.py").write_text("def other():\n    return 2\n", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        ["review", "--project-root", str(tmp_path), "--feature", feature, "--base-branch", "main", "--fail-on-block"],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["planning_artifact_mode"] == "contract_first"
    assert payload["changed_files"]["source"].startswith("task_run_result")
    assert payload["checks"]["scope_drift"]["scope_source"] == "TASK_GRAPH.json"
    assert any("TASK_GRAPH.json" in item for item in payload["remediation"])


def test_cli_review_contract_first_allows_task_graph_union_scope(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "review-contract-first-union"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        planning_dir / "PRD_INTAKE.json",
        {
            "schema_version": "contract_first.prd_intake.v1",
            "generated_at": "2026-03-24T00:00:00+00:00",
            "feature": feature,
            "business_outcome": "Union scope respects task graph core_files.",
            "source_of_truth": ["db.primary"],
            "source_of_truth_canonical": ["db.primary"],
            "path_type": "read",
            "layers": ["service", "route"],
            "coverage_hints": ["path:read", "layer:service", "layer:route", "sot:db.primary"],
            "out_of_scope": [],
            "confidence": "high",
            "confidence_issues": [],
        },
    )
    _write_json(
        planning_dir / "TASK_GRAPH.json",
        {
            "schema_version": "contract_first.task_graph.v1",
            "generated_at": "2026-03-24T00:00:00+00:00",
            "business_outcome": "Union scope respects task graph core_files.",
            "coverage_hints": ["path:read", "layer:service", "layer:route", "sot:db.primary"],
            "boundary_debt": {"status": "WARN", "details": "Multiple logical layers map to the same physical source file.", "items": [{"file": "app/main.py", "layers": ["route", "service"], "tasks": ["T1", "T2"]}]},
            "executability": {"status": "PASS", "issues": []},
            "tasks": [
                {
                    "task_id": "T1",
                    "task_name": "route update",
                    "core_files": ["app/main.py"],
                    "layer_owner": "route",
                    "invariants": ["single sot"],
                    "test_proof": "unit",
                    "coverage_hints": ["layer:route", "path:read"],
                    "executability": {"status": "PASS", "issues": []},
                },
                {
                    "task_id": "T2",
                    "task_name": "schema glue",
                    "core_files": ["app/schemas.py"],
                    "layer_owner": "service",
                    "invariants": ["single sot"],
                    "test_proof": "unit",
                    "coverage_hints": ["layer:service", "path:read", "sot:db.primary"],
                    "executability": {"status": "PASS", "issues": []},
                },
            ],
        },
    )
    _write_json(
        planning_dir / "TASK_CARD_ACTIVE.json",
        {
            "schema_version": "contract_first.task_card.v1",
            "generated_at": "2026-03-24T00:00:00+00:00",
            "task_id": "T1",
            "task_name": "route update only",
            "why_this_layer": "route only",
            "files_to_change": ["app/main.py"],
            "invariants": ["scope only"],
            "test_plan": "scoped tests",
            "forbidden_changes": ["Do not refactor unrelated modules."],
            "requires": [],
        },
    )
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    app_dir = tmp_path / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (app_dir / "schemas.py").write_text("def schema():\n    return 2\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--base-branch",
            "main",
            "--changed-file",
            "app/main.py",
            "--changed-file",
            "app/schemas.py",
            "--changed-file",
            "tests/test_api.py",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["planning_artifact_mode"] == "contract_first"
    assert payload["changed_files"]["source"] == "cli_override"
    assert payload["checks"]["scope_drift"]["status"] == "PASS"
    assert payload["checks"]["scope_drift"]["scope_source"] == "TASK_GRAPH.json"


def test_cli_qa_contract_first_reports_missing_verify_artifact_structured_reason(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-contract-first-missing-verify"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".review_result.json", {"status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_execution_result(planning_dir, feature=feature, changed_files=["app/main.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        ["qa", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["planning_artifact_mode"] == "contract_first"
    assert payload["checks"]["verify"]["verify_status"] == "MISSING"
    assert payload["checks"]["gate"]["gate_status"] == "MISSING"
    assert "verify:" in payload["blocking_reason"]
    assert any("kodawari task-run" in item for item in payload["remediation"])


def test_cli_qa_contract_first_blocks_when_planning_artifact_schema_is_invalid(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-contract-first-invalid-planning"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(
        planning_dir / "TASK_CARD_ACTIVE.json",
        {
            "task_id": "T1",
            "task_name": "Invalid card",
            "why_this_layer": "service owns behavior",
            "files_to_change": ["app/main.py"],
            "invariants": ["No second source of truth is introduced."],
            "test_plan": "Run scoped tests.",
        },
    )
    _write_json(planning_dir / ".review_result.json", {"status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        ["qa", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["checks"]["planning_artifacts"]["status"] == "FAIL"
    assert "TASK_CARD_ACTIVE.json" in payload["checks"]["planning_artifacts"]["invalid"]
    assert "planning_artifacts" in payload["blocking_reason"]


def test_cli_ship_readiness_blocks_when_must_fix_open(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-blocked-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(
        planning_dir / ".workflow_chain.json",
        {
            "upstream": {"verify": {"status": "PASS"}},
            "final_quality_review": {"status": "PASS"},
        },
    )
    _write_review_result(planning_dir)
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": ["add missing scoped test"]})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert "must_fix_closed" in payload["blocking_reason"]
    assert (planning_dir / "RELEASE.md").exists()
    assert (planning_dir / "Ship.md").exists()


def test_cli_ship_readiness_medium_blocks_when_explicit_review_evidence_missing(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-missing-explicit-review-evidence"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}, "final_quality_review": {"status": "PASS"}})
    _write_json(planning_dir / ".review_result.json", {"status": "PASS"})
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--risk-profile",
            "medium",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["review_evidence_status"] in {"MISSING", "WARN"}
    assert any(item["check"] == "risk_review_evidence_source" and item["status"] == "FAIL" for item in payload["checklist"])


def test_cli_ship_readiness_low_warns_when_explicit_review_evidence_missing(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-low-review-evidence-warn"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}, "final_quality_review": {"status": "PASS"}})
    _write_json(planning_dir / ".review_result.json", {"status": "PASS"})
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--risk-profile",
            "low",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert any(item["check"] == "risk_review_evidence_source" and item["status"] == "WARN" for item in payload["checklist"])


def test_cli_ship_readiness_blocks_when_self_review_evidence_missing(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-missing-self-review"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}, "final_quality_review": {"status": "PASS"}})
    _write_review_result(
        planning_dir,
        status="PASS",
        evidence_status="FAIL",
        self_reviews=0,
        peer_reviews=1,
        issues=["Missing Codex self-review evidence."],
        blocking_reason="Missing Codex self-review evidence.",
    )
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert "review_evidence" in payload["blocking_reason"]
    assert any("kodawari review" in item for item in payload["remediation"])


def test_cli_ship_readiness_blocks_when_peer_review_evidence_missing(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-missing-peer-review"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}, "final_quality_review": {"status": "PASS"}})
    _write_review_result(
        planning_dir,
        status="PASS",
        evidence_status="FAIL",
        self_reviews=1,
        peer_reviews=0,
        issues=["Missing Opus peer-review evidence."],
        blocking_reason="Missing Opus peer-review evidence.",
    )
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert "review_evidence" in payload["blocking_reason"]


def test_cli_ship_readiness_passes_when_all_checks_pass(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-pass-demo"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(
        planning_dir / ".workflow_chain.json",
        {
            "upstream": {"verify": {"status": "PASS"}},
            "final_quality_review": {"status": "PASS"},
        },
    )
    _write_review_result(planning_dir)
    _write_execution_result(
        planning_dir,
        feature=feature,
        changed_files=["src/service.py"],
        guard_action="deny",
        guard_policy="Force push blocked",
        guard_pattern="git\\s+push\\s+--force",
        guard_command="git push --force origin main",
    )
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["required_docs"]["all_present"] is True
    assert payload["execution_guard"]["action"] == "deny"
    assert payload["execution_guard"]["policy"] == "Force push blocked"
    assert (planning_dir / "RELEASE.md").exists()
    assert (planning_dir / "Ship.md").exists()
    assert payload["review_evidence_check"]["details"] == "Dual-review evidence present."
    ship_readiness = json.loads((planning_dir / ".ship_readiness.json").read_text(encoding="utf-8"))
    assert ship_readiness["provenance"]["command"] == "ship-readiness"
    assert ship_readiness["payload_digest"]
    release_md = (planning_dir / "RELEASE.md").read_text(encoding="utf-8")
    ship_md = (planning_dir / "Ship.md").read_text(encoding="utf-8")
    assert "## Mirror Provenance" in release_md
    assert ship_readiness["payload_digest"] in release_md
    assert "execution_guard_action: deny" in release_md
    assert "execution_guard_command: git push --force origin main" in release_md
    assert ship_md == release_md


def test_cli_ship_readiness_contract_first_accepts_contract_artifacts_without_legacy_docs(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-contract-first-pass"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(
        planning_dir / ".task_run_result.json",
        {
            "changed_files": ["app/main.py", "tests/test_api.py"],
            "task_delta_changed_files": ["app/main.py", "tests/test_api.py"],
            "verify_check": {"status": "PASS", "details": ""},
            "gate_check": {"total_status": "PASS", "details": ""},
            "review_evidence": {
                "status": "PASS",
                "checks": {"self_review_count": 1, "peer_review_count": 1},
                "issues": [],
                "evidence": [
                    {
                        "file": "planning/ship-contract-first-pass/.task_run_result.json",
                        "rule": "review_evidence.present",
                        "hit": "dual review evidence available",
                        "confidence": 1.0,
                    }
                ],
            },
        },
    )
    _write_json(
        planning_dir / ".review_result.json",
        {
            "status": "PASS",
            "review_evidence": {
                "status": "PASS",
                "checks": {"self_review_count": 1, "peer_review_count": 1},
                "issues": [],
            },
        },
    )
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})

    rc, payload = _run_cli(
        parser,
        capsys,
        ["ship-readiness", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["planning_artifact_mode"] == "contract_first"
    assert payload["required_docs"]["mode"] == "contract_first"
    assert "PLAN.md" not in payload["required_docs"]["required"]
    assert payload["required_docs"]["all_present"] is True
    assert not (planning_dir / "PLAN.md").exists()
    assert not (planning_dir / "TASKS.md").exists()
    assert not (planning_dir / "ACCEPTANCE.md").exists()
    assert not (planning_dir / "GATE.md").exists()


def test_cli_ship_readiness_contract_first_blocks_when_required_contract_artifact_is_invalid(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-contract-first-invalid-task-card"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(
        planning_dir / "TASK_CARD_ACTIVE.json",
        {
            "task_id": "T1",
            "task_name": "Invalid card",
            "why_this_layer": "service owns behavior",
            "files_to_change": ["app/main.py"],
            "invariants": ["No second source of truth is introduced."],
            "test_plan": "Run scoped tests.",
        },
    )
    _write_json(planning_dir / ".task_run_result.json", {"verify_check": {"status": "PASS"}, "gate_check": {"total_status": "PASS"}})
    _write_review_result(planning_dir)
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})

    rc, payload = _run_cli(
        parser,
        capsys,
        ["ship-readiness", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["required_docs"]["mode"] == "contract_first"
    assert "TASK_CARD_ACTIVE.json" in payload["required_docs"]["invalid"]
    assert "required_docs" in payload["blocking_reason"]


def test_cli_ship_readiness_blocks_when_eval_report_is_blocked(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-eval-blocked"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}})
    _write_review_result(planning_dir)
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "BLOCKED"})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert "eval_status" in payload["blocking_reason"]


def test_cli_ship_readiness_missing_eval_includes_remediation(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-missing-eval-guidance"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}})
    _write_review_result(planning_dir)
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert "eval_report=missing" in payload["blocking_reason"]
    assert any("kodawari eval-report" in item for item in payload["remediation"])
    assert any("--auto-eval" in item for item in payload["remediation"])


def test_cli_ship_readiness_missing_eval_default_no_auto_attempt(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-missing-eval-default"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}})
    _write_review_result(planning_dir)
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["auto_eval"] is False
    assert payload["auto_eval_result"] == {}
    assert not (tmp_path / "AUTOMATION_EVAL_REPORT.json").exists()


def test_cli_ship_readiness_auto_eval_generates_eval_artifact(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-auto-eval"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS", "profile": {"name": "advisory"}})
    _write_json(
        planning_dir / ".workflow_chain.json",
        {"upstream": {"verify": {"status": "PASS"}}, "final_quality_review": {"status": "PASS"}},
    )
    _write_review_result(planning_dir)
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    (planning_dir / ".autopilot_rounds.jsonl").write_text("", encoding="utf-8")
    _write_minimal_telemetry_snapshot(planning_dir, feature=feature)

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--auto-eval",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["auto_eval"] is True
    assert payload["auto_eval_result"]["status"] == "PASS"
    assert (tmp_path / "AUTOMATION_EVAL_REPORT.json").exists()


def test_cli_ship_readiness_missing_eval_includes_remediation_hints(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-eval-missing-guidance"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}})
    _write_review_result(planning_dir)
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert "eval_status" in payload["blocking_reason"]
    assert payload["remediation"]
    assert any("kodawari eval-report" in item for item in payload["remediation"])


def test_cli_ship_readiness_uses_eval_report_path_override(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-eval-override"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}})
    _write_review_result(planning_dir)
    _write_verify_report(planning_dir, feature=feature, status="PASS", input_confidence="curated")
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS"})
    override_path = tmp_path / "reports" / "alt_eval.json"
    _write_json(override_path, {"status": "BLOCKED"})

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "ship-readiness",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--eval-report-path",
            str(override_path),
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["eval_report_path"] == str(override_path.resolve())


def test_cli_ship_readiness_consumes_replay_gate_result_when_present(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "ship-replay-gate"
    planning_dir = tmp_path / "planning" / feature
    _prepare_planning_core(planning_dir, feature=feature)
    (planning_dir / "REVIEW.md").write_text(f"# REVIEW ({feature})\n", encoding="utf-8")
    (planning_dir / "QA_REPORT.md").write_text(f"# QA_REPORT ({feature})\n", encoding="utf-8")
    _write_json(tmp_path / "AUTOMATION_EVAL_REPORT.json", {"status": "PASS", "summary": {}, "warnings": []})
    _write_json(tmp_path / "REPLAY_GATE_RESULT.json", {"status": "BLOCKED", "summary": {"samples_failed": 1}})
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".workflow_chain.json", {"upstream": {"verify": {"status": "PASS"}}})
    _write_json(
        planning_dir / ".review_result.json",
        {"status": "PASS", "review_evidence": {"status": "PASS", "checks": {"self_review_count": 1, "peer_review_count": 1}}},
    )
    _write_json(planning_dir / ".qa_report.json", {"status": "PASS"})
    _write_json(planning_dir / "semantic_compact.json", {"must_fix": []})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})

    rc, payload = _run_cli(
        parser,
        capsys,
        ["ship-readiness", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["release_gates"]["replay"]["status"] == "BLOCKED"


def test_cli_execution_evidence_writes_canonical_artifact(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "execution-evidence-cli"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_review_result(planning_dir, changed_files=["app/main.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "execution-evidence",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--backend",
            "manual",
            "--changed-file",
            "app/main.py",
            "--summary",
            "manual implementation completed",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["execution_status"] == "PASS"
    artifact = json.loads((planning_dir / ".execution_result.json").read_text(encoding="utf-8"))
    assert artifact["backend"] == "manual"
    assert artifact["changed_files"] == ["app/main.py"]


def test_cli_execution_evidence_blocks_on_review_changed_files_mismatch(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "execution-evidence-review-mismatch"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_review_result(planning_dir, changed_files=["app/main.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "execution-evidence",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--backend",
            "manual",
            "--changed-file",
            "app/other.py",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["error_code"] == "execution_vs_review_changed_files"


def test_cli_execution_evidence_blocks_on_verify_changed_files_mismatch(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "execution-evidence-verify-mismatch"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_verify_report(planning_dir, feature=feature, status="PASS", changed_files=["app/main.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "execution-evidence",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--backend",
            "manual",
            "--changed-file",
            "app/other.py",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["error_code"] == "execution_vs_verify_changed_files"


def test_cli_verify_command_file_cmd_is_portable(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "verify-command-file-cmd"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    (tmp_path / "verify.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "tests/test_api.py",
            "--command-file",
            "verify.cmd",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["requested_command"] == "verify.cmd"
    assert payload["requested_command_kind"] == "file"
    verify_artifact = json.loads((planning_dir / ".verify_report.json").read_text(encoding="utf-8"))
    assert verify_artifact["requested_command_kind"] == "file"


def test_cli_verify_command_file_powershell_is_portable(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "verify-command-file-ps1"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    (tmp_path / "verify.ps1").write_text("exit 0\n", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "tests/test_api.py",
            "--command-file",
            "verify.ps1",
            "--fail-on-block",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["requested_command_kind"] == "file"


def test_cli_verify_rejects_command_file_and_command_together(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "verify-command-conflict"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    (tmp_path / "verify.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "verify",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--command-file",
            "verify.cmd",
            "--command",
            "pytest -q",
            "--fail-on-block",
        ],
    )

    assert rc == 2
    assert payload["error_code"] == "verify_failed"
    assert "--command-file and --command cannot be used together" in payload["error"]


def test_cli_qa_blocks_on_execution_review_changed_files_mismatch(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-execution-review-mismatch"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_review_result(planning_dir, status="PASS", changed_files=["app/main.py"])
    _write_verify_report(planning_dir, feature=feature, status="PASS", changed_files=["app/main.py"])
    _write_execution_result(planning_dir, feature=feature, changed_files=["app/other.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        ["qa", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["checks"]["execution_vs_review_changed_files"]["status"] == "FAIL"


def test_cli_qa_blocks_on_execution_verify_changed_files_mismatch(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "qa-execution-verify-mismatch"
    planning_dir = tmp_path / "planning" / feature
    _prepare_contract_first_planning_core(planning_dir, feature=feature)
    _write_json(planning_dir / ".gate_result.json", {"total_status": "PASS"})
    _write_json(planning_dir / ".autopilot_state.json", {"feature": feature})
    _write_review_result(planning_dir, status="PASS", changed_files=["app/main.py"])
    _write_verify_report(planning_dir, feature=feature, status="PASS", changed_files=["tests/test_api.py"])
    _write_execution_result(planning_dir, feature=feature, changed_files=["app/main.py"])

    rc, payload = _run_cli(
        parser,
        capsys,
        ["qa", "--project-root", str(tmp_path), "--feature", feature, "--fail-on-block"],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["checks"]["execution_vs_verify_changed_files"]["status"] == "FAIL"


def test_cli_review_scope_drift_includes_completed_upstream_task_scope(tmp_path: Path, capsys: Any) -> None:
    # Regression: after task_cycle completes upstream tasks (marked [x]),
    # the final delivery review must still allow their scope. Previously,
    # parse_task_backlog() skipped completed tasks, so accumulated
    # changed_files containing upstream-modified files were flagged as
    # out_of_scope even though an upstream task legitimately touched them.
    parser = build_parser()
    feature = "review-upstream-scope-demo"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "PLAN.md").write_text(f"# PLAN ({feature})\n", encoding="utf-8")
    (planning_dir / "TASKS.md").write_text(
        "\n".join(
            [
                f"# TASKS ({feature})",
                "",
                "- [x] T001: Update src/upstream.py",
                "- [ ] T002: Update src/current.py",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (planning_dir / "ACCEPTANCE.md").write_text(
        f"# ACCEPTANCE ({feature})\n\n- [ ] Behavior updated\n",
        encoding="utf-8",
    )
    (planning_dir / "GATE.md").write_text(f"# GATE ({feature})\n", encoding="utf-8")
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "upstream.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "current.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_upstream.py").write_text(
        "def test_upstream():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_current.py").write_text(
        "def test_current():\n    assert True\n", encoding="utf-8"
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "review",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--changed-file",
            "src/upstream.py",
            "--changed-file",
            "src/current.py",
            "--changed-file",
            "tests/test_upstream.py",
            "--changed-file",
            "tests/test_current.py",
        ],
    )

    scope = payload["checks"]["scope_drift"]
    assert scope["status"] == "PASS", scope
    assert scope["out_of_scope_files"] == []
    # Allowed hints must contain both the completed upstream task scope
    # and the remaining backlog scope.
    assert "src/upstream.py" in scope["allowed_hints"]
    assert "src/current.py" in scope["allowed_hints"]
    assert rc == 0

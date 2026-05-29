import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.autopilot.planning.task_card_file_preflight import (
    FilePreflightIssue,
    FilePreflightReport,
)
from kodawari.cli import contract_first_cmd
from kodawari.cli.main import build_parser


def _run_cli(parser: Any, capsys: Any, argv: list[str]) -> tuple[int, dict[str, Any]]:
    args = parser.parse_args(argv)
    rc = int(args.handler(args))
    payload = json.loads(capsys.readouterr().out)
    return rc, payload


def test_parser_help_contains_contract_first_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "prd-intake" in help_text
    assert "architecture-plan" in help_text
    assert "init" in help_text
    assert "task-plan" in help_text
    assert "task-prepare" in help_text
    assert "task-run" in help_text
    assert "compliance-check" in help_text
    assert "review-evidence" in help_text


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


def _task_card_payload(*, files_to_change: list[str], task_id: str = "T1", task_name: str = "Scoped task") -> dict[str, Any]:
    return {
        "schema_version": "contract_first.task_card.v1",
        "generated_at": "2026-03-24T00:00:00+00:00",
        "task_id": task_id,
        "task_name": task_name,
        "why_this_layer": "This task belongs to service layer because it owns the primary behavior.",
        "files_to_change": files_to_change,
        "invariants": ["scope only"],
        "test_plan": "scoped tests",
        "forbidden_changes": ["Do not refactor unrelated modules."],
        "requires": [],
    }


def _materialize_task_files(project_root: Path, files_to_change: list[str]) -> None:
    """Create placeholder files under ``project_root`` so task-run's file
    preflight sees them as existing. Tests that fake ``_run_task_card`` still
    need the files to exist because preflight runs before the executor.
    """
    for rel in files_to_change:
        abs_path = project_root / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        if not abs_path.exists():
            abs_path.write_text("", encoding="utf-8")


def _task_graph_payload(*, core_files: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "contract_first.task_graph.v1",
        "generated_at": "2026-03-24T00:00:00+00:00",
        "business_outcome": "Update service ranking",
        "coverage_hints": ["path:read", "layer:service", "sot:db.items"],
        "boundary_debt": {"status": "PASS", "details": "Logical layer ownership maps cleanly to distinct source files.", "items": []},
        "executability": {"status": "PASS", "issues": []},
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "service",
                "layer_owner": "service",
                "core_files": core_files,
                "invariants": ["single sot"],
                "test_proof": "unit",
                "coverage_hints": ["layer:service", "path:read", "sot:db.items"],
                "executability": {"status": "PASS", "issues": []},
            }
        ],
    }


def _prd_intake_payload(*, feature: str, source_of_truth: list[str]) -> dict[str, Any]:
    canonical = [source_of_truth[0].replace(".status", "")] if source_of_truth else ["db.items"]
    canonical = [item if item.startswith("db.") else f"db.{item.split('.', 1)[0]}" for item in canonical]
    return {
        "schema_version": "contract_first.prd_intake.v1",
        "generated_at": "2026-03-24T00:00:00+00:00",
        "feature": feature,
        "business_outcome": "Update service ranking",
        "source_of_truth": source_of_truth,
        "source_of_truth_canonical": canonical,
        "path_type": "read",
        "layers": ["service", "route"],
        "coverage_hints": ["path:read", "layer:service", "layer:route", *[f"sot:{item}" for item in canonical]],
        "out_of_scope": [],
        "confidence": "high",
        "confidence_issues": [],
    }


def test_prd_intake_task_plan_task_prepare_generate_artifacts(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-demo"
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\n", encoding="utf-8")
    (tmp_path / "app" / "schemas.py").write_text("class Payload: ...\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    prd = tmp_path / "PRD.md"
    prd.write_text(
        "\n".join(
            [
                "Build ranking API read/write flow.",
                "source of truth: db.articles",
                "layer: route service repository",
            ]
        ),
        encoding="utf-8",
    )

    rc_intake, intake_payload = _run_cli(
        parser,
        capsys,
        ["prd-intake", "--project-root", str(tmp_path), "--feature", feature, "--prd", str(prd)],
    )
    assert rc_intake == 0
    intake_path = Path(intake_payload["artifacts"]["PRD_INTAKE.json"])
    assert intake_path.exists()

    rc_graph, graph_payload = _run_cli(
        parser,
        capsys,
        [
            "task-plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--intake",
            str(intake_path),
        ],
    )
    assert rc_graph == 0
    graph_path = Path(graph_payload["artifacts"]["TASK_GRAPH.json"])
    assert graph_path.exists()
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert any(str(path).startswith("app/") for task in graph["tasks"] for path in task["core_files"])

    rc_card, card_payload = _run_cli(
        parser,
        capsys,
        [
            "task-prepare",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--graph",
            str(graph_path),
            "--task",
            "T1",
        ],
    )
    assert rc_card == 0
    card_path = Path(card_payload["artifacts"]["TASK_CARD.json"])
    assert card_path.exists()
    active_path = Path(card_payload["artifacts"]["TASK_CARD_ACTIVE.json"])
    assert active_path.exists()


def test_task_plan_writes_plans_markdown_mirror_from_contract_truth(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-plans-mirror"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    intake_path = planning_dir / "PRD_INTAKE.json"
    intake_path.write_text(
        json.dumps(_prd_intake_payload(feature=feature, source_of_truth=["db.articles"])),
        encoding="utf-8",
    )
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(_task_card_payload(files_to_change=["app/main.py"], task_id="T1")),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["task-plan", "--project-root", str(tmp_path), "--feature", feature, "--intake", str(intake_path)],
    )

    assert rc == 0
    plans_path = Path(payload["artifacts"]["Plans.md"])
    assert plans_path.exists()
    plans_markdown = plans_path.read_text(encoding="utf-8")
    assert "| Task | Content | DoD | Depends | Status |" in plans_markdown
    assert "T1 -" in plans_markdown
    assert "source_of_truth:" in plans_markdown
    assert "| active |" in plans_markdown


def test_prd_intake_fails_when_semantic_confidence_is_low(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-low-confidence"
    prd = tmp_path / "PRD.md"
    prd.write_text(
        "\n".join(
            [
                "1. business outcome",
                "- 不要顺手重构 UI。",
            ]
        ),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["prd-intake", "--project-root", str(tmp_path), "--feature", feature, "--prd", str(prd)],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["confidence"] == "low"
    assert payload["confidence_issues"]


def test_task_plan_blocks_when_input_intake_is_low_confidence(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-low-confidence-task-plan"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    intake_path = planning_dir / "PRD_INTAKE.json"
    intake_path.write_text(
        json.dumps(
            {
                "schema_version": "contract_first.prd_intake.v1",
                "generated_at": "2026-03-24T00:00:00+00:00",
                "feature": feature,
                "business_outcome": "不要顺手重构 UI。",
                "source_of_truth": ["db.primary"],
                "source_of_truth_canonical": ["db.primary"],
                "path_type": "read",
                "layers": ["service", "repository", "route"],
                "coverage_hints": ["path:read", "layer:service", "layer:repository", "layer:route", "sot:db.primary"],
                "out_of_scope": [],
                "confidence": "low",
                "confidence_issues": ["source_of_truth fell back to default value db.primary."],
            }
        ),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--intake",
            str(intake_path),
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["input_intake_confidence"] == "low"
    assert any("input PRD intake low confidence" in item for item in payload["validation_errors"])


def test_architecture_plan_generates_repo_inventory_and_architecture_truth(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "cf-architecture-demo"
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    prd = tmp_path / "PRD.md"
    prd.write_text(
        "\n".join(
            [
                "1. business outcome",
                "- Return a stable API response.",
                "2. source of truth",
                "- db.articles",
                "4. layer ownership",
                "- repository service route",
            ]
        ),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "architecture-plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--prd",
            str(prd),
        ],
    )

    assert rc == 0
    architecture_path = Path(payload["artifacts"]["ARCHITECTURE_PLAN.json"])
    inventory_path = Path(payload["artifacts"]["REPO_INVENTORY.json"])
    assert architecture_path.exists()
    assert inventory_path.exists()
    architecture_plan = json.loads(architecture_path.read_text(encoding="utf-8"))
    repo_inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    assert architecture_plan["archetype"] == "fastapi_api"
    assert architecture_plan["planning_mode"] == "existing"
    assert architecture_plan["surfaces"][0]["name"] == "backend"
    assert repo_inventory["verify_surfaces"][0]["name"] == "backend"


def test_greenfield_init_and_task_plan_can_run_from_architecture_plan_without_explicit_intake(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "cf-greenfield-generic"
    prd = tmp_path / "PRD.md"
    prd.write_text(
        "\n".join(
            [
                "1. business outcome",
                "- Let users see a simple frontend status widget.",
                "2. source of truth",
                "- db.widgets",
                "4. layer ownership",
                "- frontend",
            ]
        ),
        encoding="utf-8",
    )

    arch_rc, arch_payload = _run_cli(
        parser,
        capsys,
        [
            "architecture-plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--prd",
            str(prd),
            "--mode",
            "greenfield",
            "--archetype",
            "react_web",
            "--capability",
            "docs_runbook",
        ],
    )
    assert arch_rc == 0
    architecture_path = Path(arch_payload["artifacts"]["ARCHITECTURE_PLAN.json"])

    init_rc, init_payload = _run_cli(
        parser,
        capsys,
        [
            "init",
            "--project-root",
            str(tmp_path),
            "--architecture-plan",
            str(architecture_path),
        ],
    )
    assert init_rc == 0
    assert (tmp_path / "src" / "App.js").exists()
    assert (tmp_path / "docs" / "RUNBOOK.md").exists()

    graph_rc, graph_payload = _run_cli(
        parser,
        capsys,
        [
            "task-plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--architecture-plan",
            str(architecture_path),
            "--mode",
            "greenfield",
        ],
    )
    assert graph_rc == 0
    graph = json.loads(Path(graph_payload["artifacts"]["TASK_GRAPH.json"]).read_text(encoding="utf-8"))
    assert graph["archetype"] == "react_web"
    assert graph["surfaces"] == ["frontend", "docs"]
    assert graph["tasks"][0]["surface"] == "frontend"
    assert any(path.startswith("src/") for path in graph["tasks"][0]["core_files"])


def test_task_plan_single_surface_existing_repo_allows_missing_architecture_plan(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "cf-single-surface-existing"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    intake_path = planning_dir / "PRD_INTAKE.json"
    intake_path.write_text(
        json.dumps(_prd_intake_payload(feature=feature, source_of_truth=["db.articles"])),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["task-plan", "--project-root", str(tmp_path), "--feature", feature, "--intake", str(intake_path)],
    )

    assert rc == 0
    assert payload["planning_mode"] == "existing"
    assert payload["planning_requirements"]["requires_architecture_plan"] is False
    assert Path(payload["artifacts"]["REPO_INVENTORY.json"]).exists()


def test_task_plan_blocks_for_greenfield_when_architecture_plan_is_missing(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "cf-greenfield-missing-architecture"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    intake_path = planning_dir / "PRD_INTAKE.json"
    intake_path.write_text(
        json.dumps(_prd_intake_payload(feature=feature, source_of_truth=["db.widgets"])),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--intake",
            str(intake_path),
            "--mode",
            "greenfield",
            "--archetype",
            "react_web",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["error_code"] == "architecture_plan_required"
    assert payload["planning_requirements"]["requires_architecture_plan"] is True


def test_task_plan_blocks_for_multi_surface_existing_repo_when_architecture_plan_is_missing(
    tmp_path: Path,
    capsys: Any,
) -> None:
    parser = build_parser()
    feature = "cf-multi-surface-existing"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
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
    intake_path = planning_dir / "PRD_INTAKE.json"
    intake_path.write_text(
        json.dumps(_prd_intake_payload(feature=feature, source_of_truth=["db.articles"])),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-plan",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--intake",
            str(intake_path),
            "--archetype",
            "fullstack_fastapi_react",
        ],
    )

    assert rc == 2
    assert payload["status"] == "BLOCKED"
    assert payload["error_code"] == "architecture_plan_required"
    assert payload["planning_requirements"]["surface_count"] > 1


def test_task_plan_fails_when_input_intake_schema_version_is_missing(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-missing-intake-schema-version"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    intake_path = planning_dir / "PRD_INTAKE.json"
    intake_path.write_text(
        json.dumps(
            {
                "business_outcome": "Return hydration goal history.",
                "source_of_truth": ["db.hydration_goals"],
                "path_type": "read",
                "layers": ["service", "route"],
                "confidence": "high",
                "confidence_issues": [],
                "out_of_scope": [],
            }
        ),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["task-plan", "--project-root", str(tmp_path), "--feature", feature, "--intake", str(intake_path)],
    )

    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["error_code"] == "artifact_schema_version_invalid"


def test_compliance_check_writes_report(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-compliance-demo"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps({"changed_files": ["src/service.py"]}),
        encoding="utf-8",
    )
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(_task_card_payload(files_to_change=["src/service.py"], task_name="service")),
        encoding="utf-8",
    )
    (planning_dir / "TASK_GRAPH.json").write_text(
        json.dumps(_task_graph_payload(core_files=["src/service.py"])),
        encoding="utf-8",
    )
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps(_prd_intake_payload(feature=feature, source_of_truth=["db.items"])),
        encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    schema_dir = tmp_path / "contracts"
    schema_dir.mkdir(parents=True, exist_ok=True)
    (schema_dir / "a.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"phase_mode": {"type": "string"}}}),
        encoding="utf-8",
    )
    (schema_dir / "b.schema.json").write_text(
        json.dumps({"type": "object", "properties": {"phase_mode": {"type": "integer"}}}),
        encoding="utf-8",
    )
    (planning_dir / ".task_run_result.json").write_text(
        json.dumps(
            {
                "compliance_report": {
                    "checks": [
                        {
                            "check_name": "review_evidence",
                            "status": "PASS",
                            "details": "",
                            "evidence": [
                                {
                                    "file": "planning/cf-compliance-demo/.task_run_result.json",
                                    "rule": "review_evidence.present",
                                    "hit": "dual review evidence available",
                                    "confidence": 1.0,
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["compliance-check", "--project-root", str(tmp_path), "--feature", feature],
    )
    assert rc in {0, 2}
    report_path = Path(payload["artifacts"]["COMPLIANCE_REPORT.json"])
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "checks" in report
    checks = {item["check_name"]: item for item in report["checks"]}
    assert checks["review_evidence"]["status"] == "WARN"
    assert "explicit review evidence missing" in checks["review_evidence"]["details"]
    assert checks["review_evidence"]["evidence_count"] >= 1
    assert checks["runtime_contract_scatter"]["status"] == "FAIL"


def test_task_run_strict_scope_returns_non_zero_on_scope_drift(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    parser = build_parser()
    feature = "cf-task-run-demo"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    _materialize_task_files(tmp_path, ["src/allowed.py"])
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["src/allowed.py"])),
        encoding="utf-8",
    )

    def _fake_run_task_card(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # Simulate execution that touched a file outside files_to_change.
        return {
            "reason": "",
            "rounds": [],
            "changed_files": ["src/not_allowed.py"],
            "compliance_report": {"status": "PASS"},
        }

    monkeypatch.setattr("kodawari.cli.contract_first_cmd._run_task_card", _fake_run_task_card)

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--strict-scope",
            "--contract-mode",
            "strict",
            "--max-cycles",
            "3",
        ],
    )

    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["reason"] in {
        "PHASE_GUARD_BLOCKED",
        "IMPLEMENTATION_ERROR",
        "OPUS_REVIEW_BLOCKED",
        "MAX_CYCLES_REACHED",
        "SCOPE_DRIFT_BLOCKED",  # post-execution scope guard catch
    }


def test_task_run_fails_when_task_card_schema_version_is_missing(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-task-run-invalid-card"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(
            {
                "task_id": "T1",
                "task_name": "Scoped task",
                "files_to_change": ["src/allowed.py"],
                "invariants": ["scope only"],
                "test_plan": "scoped tests",
            }
        ),
        encoding="utf-8",
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["task-run", "--project-root", str(tmp_path), "--feature", feature, "--card", str(card_path)],
    )

    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["reason"] == "TASK_CARD_INVALID"
    assert payload["error_code"] == "artifact_schema_version_invalid"


def test_task_run_strict_scope_returns_zero_when_scope_and_compliance_pass(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "cf-task-run-pass"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    _materialize_task_files(tmp_path, ["src/allowed.py"])
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["src/allowed.py"])),
        encoding="utf-8",
    )

    def _fake_run_task_card(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "reason": "",
            "rounds": [
                {
                    "details": {
                        "scope_drift": {
                            "guard": {"blocked": False},
                        }
                    }
                }
            ],
            "compliance_report": {"status": "PASS"},
        }

    monkeypatch.setattr("kodawari.cli.contract_first_cmd._run_task_card", _fake_run_task_card)

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--strict-scope",
            "--contract-mode",
            "strict",
            "--max-cycles",
            "3",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"


def test_task_run_strict_scope_success_returns_zero(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    parser = build_parser()
    feature = "cf-task-run-success"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    _materialize_task_files(tmp_path, ["src/allowed.py"])
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["src/allowed.py"])),
        encoding="utf-8",
    )

    def _fake_run_task_card(
        args: Any,
        *,
        card: dict[str, Any],
        card_path: Path | None = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        del card_path, run_id
        return {
            "reason": "",
            "changed_files": ["src/allowed.py"],
            "execution_result": {
                "schema_version": "execution.result.v1",
                "feature": feature,
                "task": "T1: Scoped task",
                "backend": "codex_cli",
                "backend_capabilities": {
                    "backend": "codex_cli",
                    "maturity": "stable",
                    "implemented": True,
                    "executor_selectable": True,
                    "self_review_selectable": False,
                    "requires_command": False,
                    "supports_agent_teams": False,
                    "supports_worktree_isolation": False,
                    "supports_hooks": False,
                    "supports_memory": False,
                    "supports_deterministic_changed_files": True,
                },
                "status": "PASS",
                "changed_files": ["src/allowed.py"],
                "stdout_excerpt": "",
                "stderr_excerpt": "",
                "returncode": 0,
                "artifacts": ["src/allowed.py"],
                "error_code": "",
                "blocking_reason": "",
                "summary": "ok",
            },
            "rounds": [],
            "compliance_report": {"status": "PASS"},
        }

    monkeypatch.setattr(contract_first_cmd, "_run_task_card", _fake_run_task_card)
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--strict-scope",
            "--contract-mode",
            "strict",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["execution_backend"] == "codex_cli"
    assert payload["execution_backend_capabilities"]["backend"] == "codex_cli"
    assert payload["execution_backend_capabilities"]["supports_deterministic_changed_files"] is True
    assert payload["scope_summary"]["allowed_files"] == ["src/allowed.py"]
    assert payload["scope_summary"]["changed_files"] == ["src/allowed.py"]


def test_task_run_strict_mode_blocks_preexisting_dirty_core_files(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "cf-task-run-dirty-core"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["src/allowed.py"])),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        contract_first_cmd,
        "capture_worktree_baseline",
        lambda **_kwargs: {
            "schema_version": "worktree.baseline.v1",
            "captured_at": "2026-03-23T00:00:00+00:00",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "command": "task-run",
            "mode": "fail",
            "status": "FAIL",
            "dirty_files": ["src/allowed.py"],
            "tracked_dirty_files": ["src/allowed.py"],
            "untracked_files": [],
            "allowed_files": ["src/allowed.py"],
            "core_dirty_files": ["src/allowed.py"],
            "details": "Core task files already dirty before run: ['src/allowed.py']",
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--strict-scope",
            "--contract-mode",
            "strict",
        ],
    )

    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["reason"] == "DIRTY_WORKTREE_BLOCKED"
    assert payload["dirty_core_guard"]["blocked"] is True
    assert payload["worktree_preflight"]["core_dirty_files"] == ["src/allowed.py"]


def test_task_run_missing_card_returns_non_zero(tmp_path: Path, capsys: Any) -> None:
    parser = build_parser()
    feature = "cf-task-run-missing-card"
    missing = tmp_path / "planning" / feature / "TASK_CARD_T1.json"
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(missing),
            "--strict-scope",
            "--contract-mode",
            "strict",
        ],
    )

    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["reason"] == "TASK_CARD_MISSING"


@pytest.mark.parametrize(
    ("issue_kind", "expected_reason"),
    [
        ("missing_source", "TASK_CARD_MISSING_SOURCE"),
        ("new_file_already_exists", "TASK_CARD_NEW_FILE_EXISTS"),
        ("invalid_verify_cmd", "TASK_CARD_INVALID_VERIFY_CMD"),
        ("large_file_requires_target_symbols", "LARGE_FILE_TASK_REQUIRES_TARGET_SYMBOLS"),
        ("symbol_not_found", "TASK_CARD_SYMBOL_NOT_FOUND"),
        ("stale_task_card", "TASK_CARD_STALE"),
        ("unauthorized_mutation", "TASK_CARD_UNAUTHORIZED_MUTATION"),
    ],
)
def test_task_run_maps_preflight_issue_kind_to_reason(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
    issue_kind: str,
    expected_reason: str,
) -> None:
    parser = build_parser()
    feature = f"cf-task-run-preflight-reason-{issue_kind}"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["src/allowed.py"])),
        encoding="utf-8",
    )
    _materialize_task_files(tmp_path, ["src/allowed.py"])

    monkeypatch.setattr(
        contract_first_cmd,
        "_task_run_preflight",
        lambda **_kwargs: (
            {
                "schema_version": "worktree.baseline.v1",
                "status": "PASS",
                "core_dirty_files": [],
            },
            {"blocked": False, "status": "PASS", "reason": ""},
        ),
    )
    monkeypatch.setattr(
        contract_first_cmd,
        "run_file_preflight",
        lambda *_args, **_kwargs: FilePreflightReport(
            blocked=True,
            issues=(FilePreflightIssue(kind=issue_kind, path="src/allowed.py"),),
        ),
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--strict-scope",
            "--contract-mode",
            "strict",
        ],
    )

    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["reason"] == expected_reason
    assert payload["preflight_issues"][0]["kind"] == issue_kind


def test_task_run_surfaces_preflight_warnings_when_not_blocked(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "cf-task-run-preflight-warnings"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["src/allowed.py"])),
        encoding="utf-8",
    )
    _materialize_task_files(tmp_path, ["src/allowed.py"])

    monkeypatch.setattr(
        contract_first_cmd,
        "_task_run_preflight",
        lambda **_kwargs: (
            {
                "schema_version": "worktree.baseline.v1",
                "status": "PASS",
                "core_dirty_files": [],
            },
            {"blocked": False, "status": "PASS", "reason": ""},
        ),
    )
    monkeypatch.setattr(
        contract_first_cmd,
        "run_file_preflight",
        lambda *_args, **_kwargs: FilePreflightReport(
            blocked=False,
            issues=(),
            warnings=(
                FilePreflightIssue(
                    kind="large_file_symbol_map_deep_exempt",
                    path="src/allowed.py",
                    detail="deep exempt warning",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        contract_first_cmd,
        "_run_task_card",
        lambda *_args, **_kwargs: {
            "reason": "",
            "execution_backend": "codex_cli",
            "execution_backend_capabilities": {"backend": "codex_cli"},
            "changed_files": ["src/allowed.py"],
            "verify_check": {"status": "PASS", "verify_cmd": "pytest -q", "summary": "ok"},
            "execution_result": {"backend": "codex_cli", "backend_capabilities": {"backend": "codex_cli"}},
            "compliance_report": {"status": "PASS"},
            "rounds": [],
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--strict-scope",
            "--contract-mode",
            "strict",
        ],
    )
    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["preflight_warnings"][0]["kind"] == "large_file_symbol_map_deep_exempt"
    assert payload["file_preflight"]["warnings"][0]["kind"] == "large_file_symbol_map_deep_exempt"


def test_task_run_openai_tool_use_accepts_preexisting_new_file_after_verify_gate(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "cf-task-run-preexisting-new-file"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    card = _task_card_payload(files_to_change=["tests/test_existing.py"])
    card["new_files"] = ["tests/test_existing.py"]
    card["verify_cmd"] = "python -m pytest tests/test_existing.py"
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    _materialize_task_files(tmp_path, ["tests/test_existing.py"])

    monkeypatch.setattr(
        contract_first_cmd,
        "_run_task_card",
        lambda *_args, **_kwargs: {
            "reason": "",
            "execution_backend": "openai_tool_use",
            "execution_backend_capabilities": {"backend": "openai_tool_use"},
            "changed_files": ["tests/test_existing.py"],
            "verify_check": {"status": "PASS", "verify_cmd": card["verify_cmd"], "summary": "ok"},
            "execution_result": {
                "backend": "openai_tool_use",
                "backend_capabilities": {"backend": "openai_tool_use"},
            },
            "compliance_report": {"status": "PASS"},
            "rounds": [],
        },
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--strict-scope",
            "--contract-mode",
            "strict",
            "--executor-backend",
            "openai_tool_use",
        ],
    )

    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["file_preflight"]["warnings"][0]["kind"] == "new_file_already_exists_reused"


def test_task_run_returns_next_action_when_analyze_hits_cycle_limit(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "cf-task-run-analyze-guidance"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    _materialize_task_files(tmp_path, ["app/main.py"])
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["app/main.py"], task_name="Analyze guidance")),
        encoding="utf-8",
    )

    def _fake_run_task_card(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"reason": "MAX_CYCLES_REACHED", "changed_files": [], "rounds": [], "compliance_report": {"status": "PASS"}}

    monkeypatch.setattr(contract_first_cmd, "_run_task_card", _fake_run_task_card)
    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--phase-mode",
            "analyze",
            "--max-cycles",
            "1",
        ],
    )
    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["reason"] == "MAX_CYCLES_REACHED"
    assert "switch to --phase-mode implement" in payload["next_action"]


def test_task_run_blocks_without_real_executor_backend(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    parser = build_parser()
    feature = "cf-task-run-no-executor"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    card_path = planning_dir / "TASK_CARD_T1.json"
    card_path.write_text(
        json.dumps(_task_card_payload(files_to_change=["app/main.py", "tests/test_api.py"])),
        encoding="utf-8",
    )
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr("kodawari.autopilot.execution_artifacts.is_test_environment", lambda: False)
    monkeypatch.setattr("kodawari.autopilot.local_adapter.is_test_environment", lambda: False)

    rc, payload = _run_cli(
        parser,
        capsys,
        [
            "task-run",
            "--project-root",
            str(tmp_path),
            "--feature",
            feature,
            "--card",
            str(card_path),
            "--verify-cmd",
            "python -c \"print('verify ok')\"",
        ],
    )

    assert rc == 2
    assert payload["status"] == "FAIL"
    assert payload["reason"] in {"EXECUTION_BACKEND_BLOCKED", "EXECUTOR_BACKEND_MISSING"}
    assert ".execution_request.json" in payload["artifacts"]


def test_compliance_check_prefers_task_delta_changed_files_source(
    tmp_path: Path,
    capsys: Any,
    monkeypatch: Any,
) -> None:
    parser = build_parser()
    feature = "cf-compliance-delta"
    planning_dir = tmp_path / "planning" / feature
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text(
        json.dumps({"changed_files": ["src/stale.py"]}),
        encoding="utf-8",
    )
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(_task_card_payload(files_to_change=["src/service.py"], task_name="service")),
        encoding="utf-8",
    )
    (planning_dir / "TASK_GRAPH.json").write_text(
        json.dumps(_task_graph_payload(core_files=["src/service.py"])),
        encoding="utf-8",
    )
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps(_prd_intake_payload(feature=feature, source_of_truth=["db.items"])),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "service.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    monkeypatch.setattr(
        contract_first_cmd,
        "resolve_task_delta_changed_files",
        lambda **_kwargs: (["src/service.py"], "baseline_delta:git_worktree"),
    )

    rc, payload = _run_cli(
        parser,
        capsys,
        ["compliance-check", "--project-root", str(tmp_path), "--feature", feature],
    )

    assert rc in {0, 2}
    assert payload["changed_files"] == ["src/service.py"]
    assert payload["changed_files_source"] == "baseline_delta:git_worktree"


# ---------------------------------------------------------------------------
# Phase A2: task-run terminal state sync back to .autopilot_state.json
# ---------------------------------------------------------------------------
# When task-run finishes with a terminal loop reason (OPUS_REVIEW_BLOCKED,
# PROCEED_TO_GATE, SELF_REVIEW_BLOCKED, ...), _cmd_task_run must write that
# outcome into .autopilot_state.json. Without this sync, `kodawari status`
# reads a stale RUNNING left over from a prior autopilot session.


def test_derive_task_run_terminal_state_maps_review_blocked() -> None:
    from kodawari.cli.task_run_state_sync import derive_task_run_terminal_state

    terminal = derive_task_run_terminal_state(
        {"reason": "OPUS_REVIEW_BLOCKED"}
    )
    assert terminal == {"final_status": "BLOCKED", "stop_reason": "HARD_ERROR"}


def test_derive_task_run_terminal_state_maps_pass() -> None:
    from kodawari.cli.task_run_state_sync import derive_task_run_terminal_state

    terminal = derive_task_run_terminal_state(
        {"reason": "PROCEED_TO_GATE"}
    )
    assert terminal == {"final_status": "PASS", "stop_reason": "PASS"}


def test_derive_task_run_terminal_state_returns_none_for_unknown_reason() -> None:
    from kodawari.cli.task_run_state_sync import derive_task_run_terminal_state

    assert derive_task_run_terminal_state({"reason": ""}) is None
    assert derive_task_run_terminal_state({"reason": "WEIRD"}) is None


def test_sync_task_run_terminal_state_updates_state_json(tmp_path: Path) -> None:
    from kodawari.cli.task_run_state_sync import sync_task_run_terminal_state

    state_path = tmp_path / ".autopilot_state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "autopilot.state.v1",
                "feature": "demo",
                "project_root": str(tmp_path),
                "current_stage": "IMPLEMENT",
                "final_status": "",
                "stop_reason": None,
                "last_stage_status": "",
                "last_error": "",
            }
        ),
        encoding="utf-8",
    )

    sync_task_run_terminal_state(
        state_path=state_path,
        run_result={
            "reason": "OPUS_REVIEW_BLOCKED",
            "blocking_reason": "scoped tests required but task scope excludes test files",
        },
    )

    updated = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated["current_stage"] == "COMPLETED"
    assert updated["final_status"] == "BLOCKED"
    assert updated["stop_reason"] == "HARD_ERROR"
    assert updated["last_stage_status"] == "BLOCKED"
    assert "scoped tests required" in updated["last_error"]
    assert updated["updated_at"]


def test_sync_task_run_terminal_state_noop_when_state_missing(tmp_path: Path) -> None:
    from kodawari.cli.task_run_state_sync import sync_task_run_terminal_state

    state_path = tmp_path / ".autopilot_state.json"  # does not exist
    # Should not raise, should not create the file
    sync_task_run_terminal_state(
        state_path=state_path,
        run_result={"reason": "OPUS_REVIEW_BLOCKED"},
    )
    assert not state_path.exists()


def test_sync_task_run_terminal_state_noop_when_reason_unknown(tmp_path: Path) -> None:
    from kodawari.cli.task_run_state_sync import sync_task_run_terminal_state

    state_path = tmp_path / ".autopilot_state.json"
    original = {
        "schema_version": "autopilot.state.v1",
        "feature": "demo",
        "project_root": str(tmp_path),
        "current_stage": "IMPLEMENT",
        "final_status": "",
        "stop_reason": None,
    }
    state_path.write_text(json.dumps(original), encoding="utf-8")

    sync_task_run_terminal_state(
        state_path=state_path,
        run_result={"reason": "UNKNOWN_REASON"},
    )

    assert json.loads(state_path.read_text(encoding="utf-8")) == original

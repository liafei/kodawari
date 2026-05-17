import json
from pathlib import Path

from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine


def _write_task_card(path: Path, files: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "task_id": "T1",
                "task_name": "Contract task",
                "files_to_change": files,
                "invariants": ["single SoT"],
                "test_plan": "scoped tests",
            }
        ),
        encoding="utf-8",
    )


def test_phase_mode_analyze_blocks_implementation(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning" / "contract-analyze"
    card_path = planning_dir / "TASK_CARD_ACTIVE.json"
    _write_task_card(card_path, ["src/allowed.py"])
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="contract-analyze",
        contract_first_mode="strict",
        phase_mode="analyze",
        task_card_path=card_path,
    )
    engine = AutopilotEngine(config)

    result = engine.run_collaboration_loop(
        task_label="T1: Analyze phase block",
        task_scope="must not write code in analyze mode",
    )

    assert result["reason"] == "PHASE_GUARD_BLOCKED"
    assert result["loop_outcome"]["blocked"] is True


def test_phase_mode_implement_requires_task_card_in_strict_contract_mode(tmp_path: Path) -> None:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="contract-implement-no-card",
        contract_first_mode="strict",
        phase_mode="implement",
    )
    engine = AutopilotEngine(config)

    result = engine.run_collaboration_loop(
        task_label="T1: Implement requires card",
        task_scope="must have task card in strict mode",
    )

    assert result["reason"] == "PHASE_GUARD_BLOCKED"
    assert result["loop_outcome"]["blocked"] is True


def test_contract_mode_writes_compliance_report_artifacts(tmp_path: Path) -> None:
    feature = "contract-report"
    planning_dir = tmp_path / "planning" / feature
    card_path = planning_dir / "TASK_CARD_ACTIVE.json"
    _write_task_card(card_path, ["src/app.py"])
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps({"business_outcome": "Deliver stable API", "source_of_truth": ["db.primary"]}),
        encoding="utf-8",
    )
    (planning_dir / "TASK_GRAPH.json").write_text(
        json.dumps({"tasks": [{"task_id": "T1", "task_name": "service", "layer_owner": "service", "core_files": ["src/app.py"], "invariants": ["x"], "test_proof": "y"}]}),
        encoding="utf-8",
    )

    config = AutopilotConfig(
        project_root=tmp_path,
        feature=feature,
        contract_first_mode="warn",
        phase_mode="implement",
        task_card_path=card_path,
    )
    engine = AutopilotEngine(config)

    result = engine.run_collaboration_loop(
        task_label="T1: Contract report generation",
        task_scope="normal run",
    )

    assert "compliance_report" in result
    assert (planning_dir / "COMPLIANCE_REPORT.json").exists()
    assert (planning_dir / "COMPLIANCE_REPORT.md").exists()


def test_contract_mode_compliance_report_detects_project_schema_conflict(tmp_path: Path) -> None:
    feature = "contract-schema-conflict"
    planning_dir = tmp_path / "planning" / feature
    card_path = planning_dir / "TASK_CARD_ACTIVE.json"
    _write_task_card(card_path, ["src/app.py"])
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps({"business_outcome": "Protect runtime contract", "source_of_truth": ["db.primary"]}),
        encoding="utf-8",
    )
    (planning_dir / "TASK_GRAPH.json").write_text(
        json.dumps({"tasks": [{"task_id": "T1", "task_name": "service", "layer_owner": "service", "core_files": ["src/app.py"], "invariants": ["x"], "test_proof": "y"}]}),
        encoding="utf-8",
    )
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

    config = AutopilotConfig(
        project_root=tmp_path,
        feature=feature,
        contract_first_mode="warn",
        phase_mode="implement",
        task_card_path=card_path,
    )
    engine = AutopilotEngine(config)

    result = engine.run_collaboration_loop(
        task_label="T1: Detect runtime contract conflict",
        task_scope="normal run",
    )

    report = dict(result.get("compliance_report") or {})
    checks = {item["check_name"]: item for item in list(report.get("checks") or []) if isinstance(item, dict)}
    assert checks["runtime_contract_scatter"]["status"] == "FAIL"

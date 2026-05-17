import json
from pathlib import Path

from kodawari.gate.code_health import collect_code_health_snapshot


def test_collect_code_health_snapshot_aggregates_gate_and_compliance_metrics(tmp_path: Path) -> None:
    route_file = tmp_path / "src" / "routes" / "feed_route.py"
    route_file.parent.mkdir(parents=True, exist_ok=True)
    route_file.write_text("from app.repository.feed_repo import FeedRepo\n", encoding="utf-8")

    schema_a = tmp_path / "alpha.schema.json"
    schema_b = tmp_path / "beta.schema.json"
    schema_a.write_text(
        json.dumps({"type": "object", "properties": {"phase_mode": {"type": "string"}}}, ensure_ascii=False),
        encoding="utf-8",
    )
    schema_b.write_text(
        json.dumps({"type": "object", "properties": {"phase_mode": {"type": "integer"}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    snapshot = collect_code_health_snapshot(project_root=tmp_path, targets=[tmp_path / "src"])

    assert snapshot["schema_version"] == "code_health.baseline.v1"
    assert snapshot["metrics"]["files_over_1000_lines"] == 0
    assert snapshot["metrics"]["functions_over_50_lines"] == 0
    assert snapshot["metrics"]["functions_complexity_7_to_10"] == 0
    assert snapshot["metrics"]["functions_complexity_over_10"] == 0
    assert snapshot["metrics"]["files_large_and_complex_warn"] == 0
    assert snapshot["metrics"]["files_large_and_complex_block"] == 0
    assert snapshot["metrics"]["files_large_declarative_over_1500"] == 0
    assert snapshot["metrics"]["layer_boundary_violations"] is not None
    assert snapshot["metrics"]["layer_boundary_violations"] >= 1
    assert snapshot["metrics"]["runtime_contract_scatter_conflicts"] == 1
    assert "metric_deprecations" in snapshot
    assert "tool_versions" in snapshot
    assert "duplication" in snapshot


def test_collect_code_health_snapshot_ignores_pass_informational_evidence(tmp_path: Path) -> None:
    service_file = tmp_path / "src" / "service.py"
    service_file.parent.mkdir(parents=True, exist_ok=True)
    service_file.write_text("def run():\n    return 1\n", encoding="utf-8")

    snapshot = collect_code_health_snapshot(project_root=tmp_path, targets=[tmp_path / "src"])

    assert snapshot["metrics"]["layer_boundary_violations"] == 0
    assert snapshot["metrics"]["layer_boundary_debt_files"] == 0
    assert snapshot["metrics"]["sot_conflict_count"] == 0
    assert snapshot["metrics"]["import_rule_violations"] == 0
    assert snapshot["metrics"]["domain_sot_conflict_count"] == 0


def test_collect_code_health_snapshot_does_not_count_runtime_metadata_drift(
    tmp_path: Path,
) -> None:
    service_file = tmp_path / "src" / "service.py"
    service_file.parent.mkdir(parents=True, exist_ok=True)
    service_file.write_text("def run():\n    return 1\n", encoding="utf-8")

    schema_a = tmp_path / "alpha.schema.json"
    schema_b = tmp_path / "beta.schema.json"
    schema_a.write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["phase_mode"],
                "properties": {"phase_mode": {"type": "string"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    schema_b.write_text(
        json.dumps({"type": "object", "properties": {"phase_mode": {"type": "string"}}}, ensure_ascii=False),
        encoding="utf-8",
    )

    snapshot = collect_code_health_snapshot(project_root=tmp_path, targets=[tmp_path / "src"])

    assert snapshot["metrics"]["runtime_contract_scatter_conflicts"] == 0


def test_collect_code_health_snapshot_reads_duplicate_block_count_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service_file = tmp_path / "src" / "service.py"
    service_file.parent.mkdir(parents=True, exist_ok=True)
    service_file.write_text("def run():\n    return 1\n", encoding="utf-8")

    monkeypatch.setattr(
        "kodawari.gate.code_health._duplication_payload",
        lambda *_args, **_kwargs: {
            "checker": "duplication",
            "status": "WARN",
            "duplicate_block_count": 3,
            "tool_versions": {"python": "3.11.9"},
            "evidence": [],
        },
    )

    snapshot = collect_code_health_snapshot(project_root=tmp_path, targets=[tmp_path / "src"])

    assert snapshot["metrics"]["total_duplicate_blocks"] == 3

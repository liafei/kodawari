import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from kodawari.cli.contract.contract_first_schema import (
    ContractFirstSchemaValidationError,
    validate_contract_first_payload,
)
from kodawari.gate.checkers import (
    build_contract_compliance_report,
    check_cache_consistency,
    check_layer_boundary_simple,
    check_source_of_truth_conflict,
    check_runtime_contract_scatter,
    check_scope_drift,
)


def _init_git_baseline(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.local"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "baseline"], check=True, capture_output=True)


def test_scope_drift_pass_and_fail() -> None:
    passed = check_scope_drift(["src/service.py", "tests/test_service.py"], ["src/service.py"])
    failed = check_scope_drift(["src/other.py"], ["src/service.py"])

    assert passed["status"] == "PASS"
    assert failed["status"] == "FAIL"
    assert failed["out_of_scope_files"] == ["src/other.py"]


def test_scope_drift_rejects_unmapped_test_file() -> None:
    failed = check_scope_drift(["tests/test_unrelated.py"], ["src/service.py"])
    assert failed["status"] == "FAIL"
    assert failed["out_of_scope_files"] == ["tests/test_unrelated.py"]


def test_scope_drift_normalizes_absolute_changed_path(tmp_path: Path) -> None:
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def handler():\n    return {}\n", encoding="utf-8")
    changed = [str(source.resolve())]
    allowed = ["app/main.py"]
    result = check_scope_drift(changed, allowed, project_root=tmp_path)
    assert result["status"] == "PASS"
    assert result["out_of_scope_files"] == []


def test_scope_drift_normalizes_windows_style_relative_path(tmp_path: Path) -> None:
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def handler():\n    return {}\n", encoding="utf-8")
    changed = [".\\app\\..\\app\\main.py"]
    allowed = ["app/main.py"]
    result = check_scope_drift(changed, allowed, project_root=tmp_path)
    assert result["status"] == "PASS"
    assert result["out_of_scope_files"] == []


def test_layer_boundary_simple_detects_route_repository_import(tmp_path: Path) -> None:
    route_file = tmp_path / "src" / "routes" / "feed_route.py"
    route_file.parent.mkdir(parents=True, exist_ok=True)
    route_file.write_text("from app.repository.feed_repo import FeedRepo\n", encoding="utf-8")

    violations = check_layer_boundary_simple(["src/routes/feed_route.py"], tmp_path)
    assert violations
    assert "route layer cannot import repository directly" in violations[0]


def test_layer_boundary_simple_ignores_pre_existing_route_repository_import(tmp_path: Path) -> None:
    route_file = tmp_path / "src" / "routes" / "feed_route.py"
    route_file.parent.mkdir(parents=True, exist_ok=True)
    route_file.write_text(
        "from app.repository.feed_repo import FeedRepo\n\n"
        "def old_handler():\n"
        "    return FeedRepo\n",
        encoding="utf-8",
    )
    _init_git_baseline(tmp_path)
    route_file.write_text(route_file.read_text(encoding="utf-8") + "\ndef new_handler():\n    return {}\n", encoding="utf-8")

    violations = check_layer_boundary_simple(["src/routes/feed_route.py"], tmp_path)
    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/routes/feed_route.py"],
        task_card={"files_to_change": ["src/routes/feed_route.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "route", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Add route", "source_of_truth": [], "path_type": "read"},
        review_evidence={"status": "PASS"},
        schema_files=[],
    )

    assert violations == []
    layer_check = next(item for item in report["checks"] if item["check_name"] == "layer_boundary")
    assert layer_check["status"] == "PASS"


def test_layer_boundary_still_detects_new_route_repository_import(tmp_path: Path) -> None:
    route_file = tmp_path / "src" / "routes" / "feed_route.py"
    route_file.parent.mkdir(parents=True, exist_ok=True)
    route_file.write_text("def old_handler():\n    return {}\n", encoding="utf-8")
    _init_git_baseline(tmp_path)
    route_file.write_text(
        route_file.read_text(encoding="utf-8")
        + "\nfrom app.repository.feed_repo import FeedRepo\n"
        + "\ndef new_handler():\n    return FeedRepo\n",
        encoding="utf-8",
    )

    violations = check_layer_boundary_simple(["src/routes/feed_route.py"], tmp_path)
    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/routes/feed_route.py"],
        task_card={"files_to_change": ["src/routes/feed_route.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "route", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Add route", "source_of_truth": [], "path_type": "read"},
        review_evidence={"status": "PASS"},
        schema_files=[],
    )

    assert violations
    layer_check = next(item for item in report["checks"] if item["check_name"] == "layer_boundary")
    assert layer_check["status"] == "FAIL"


def test_cache_consistency_ast_association_passes_when_no_cache_semantics(tmp_path: Path) -> None:
    write_file = tmp_path / "src" / "service.py"
    write_file.parent.mkdir(parents=True, exist_ok=True)
    write_file.write_text("def run(item):\n    db.session.add(item)\n", encoding="utf-8")

    result = check_cache_consistency(["src/service.py"], tmp_path)
    assert result["status"] == "PASS"
    assert result["mode"] == "ast_association_v2"
    assert result["suspicious_files"] == []
    assert result["warn_files"] == []


def test_cache_consistency_warns_for_unattributed_existing_write_path(tmp_path: Path) -> None:
    service = tmp_path / "src" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(
        "cache = {}\n"
        "def read_cached():\n"
        "    return cache.get('x')\n"
        "def write_item(item):\n"
        "    db.session.add(item)\n",
        encoding="utf-8",
    )

    result = check_cache_consistency(["src/service.py"], tmp_path)

    assert result["status"] == "WARN"
    assert result["fail_files"] == []
    assert result["warn_files"] == ["src/service.py"]


def test_source_of_truth_alias_db_primary_allows_table_writes(tmp_path: Path) -> None:
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def upsert():\n    sql = 'INSERT INTO caregivers(id) VALUES (1)'\n", encoding="utf-8")
    violations = check_source_of_truth_conflict(["app/main.py"], tmp_path, ["db.primary"])
    assert violations == []


def test_source_of_truth_column_level_declared_sot_allows_table_write(tmp_path: Path) -> None:
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def upsert():\n    sql = 'UPDATE reminder_events SET status = \"taken\" WHERE id = 1'\n", encoding="utf-8")
    violations = check_source_of_truth_conflict(
        ["app/main.py"],
        tmp_path,
        ["reminder_events.status", "reminder_events.amount_ml"],
    )
    assert violations == []


def test_source_of_truth_canonical_aliases_align_for_gate_checks(tmp_path: Path) -> None:
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def upsert():\n    sql = 'UPDATE reminder_events SET status = \"taken\" WHERE id = 1'\n", encoding="utf-8")

    direct = check_source_of_truth_conflict(["app/main.py"], tmp_path, ["db.reminder_events"])
    column = check_source_of_truth_conflict(["app/main.py"], tmp_path, ["reminder_events.status"])
    qualified = check_source_of_truth_conflict(["app/main.py"], tmp_path, ["db.reminder_events.status"])

    assert direct == []
    assert column == []
    assert qualified == []


def test_source_of_truth_still_detects_undeclared_table_without_domain_alias(tmp_path: Path) -> None:
    source = tmp_path / "app" / "main.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def upsert():\n    sql = 'INSERT INTO caregivers(id) VALUES (1)'\n", encoding="utf-8")
    violations = check_source_of_truth_conflict(["app/main.py"], tmp_path, ["db.orders"])
    assert violations
    assert "db.caregivers" in violations[0]


def test_runtime_contract_scatter_detects_conflicting_fields(tmp_path: Path) -> None:
    schema_a = tmp_path / "a.schema.json"
    schema_b = tmp_path / "b.schema.json"
    schema_a.write_text(
        '{"type":"object","properties":{"phase_mode":{"type":"string","enum":["analyze","implement"]}}}',
        encoding="utf-8",
    )
    schema_b.write_text(
        '{"type":"object","properties":{"phase_mode":{"type":"integer"}}}',
        encoding="utf-8",
    )
    result = check_runtime_contract_scatter([str(schema_a), str(schema_b)])
    assert result["status"] == "FAIL"
    assert result["mode"] == "structural_rule_v3"
    assert "phase_mode" in result["conflict_fields"]
    assert result["conflict_files"]["phase_mode"] == [str(schema_a.resolve()), str(schema_b.resolve())]
    assert result["evidence"]


def test_build_contract_compliance_report_aggregates_checks(tmp_path: Path) -> None:
    service = tmp_path / "src" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("def run():\n    update_item()\n", encoding="utf-8")
    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/service.py"],
        task_card={"files_to_change": ["src/service.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "service", "layer_owner": "service", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={
            "business_outcome": "Update ranking service",
            "source_of_truth": ["db.items"],
            "source_of_truth_canonical": ["db.items"],
            "layers": ["service"],
            "path_type": "write",
        },
        review_evidence={"status": "PASS"},
        schema_files=[],
    )

    assert report["status"] in {"PASS", "FAIL"}
    names = {item["check_name"] for item in report["checks"]}
    assert names == {
        "scope_drift",
        "layer_boundary",
        "layer_boundary_debt",
        "source_of_truth_conflict",
        "invariant_proof",
        "prd_coverage",
        "cache_consistency",
        "runtime_contract_scatter",
        "duplication",
        "import_rules",
        "domain_source_of_truth",
        "review_evidence",
    }
    assert all("evidence" in item for item in report["checks"])
    for item in report["checks"]:
        status = str(item.get("status") or "").upper()
        if status in {"FAIL", "WARN"}:
            evidence = list(item.get("evidence") or [])
            assert evidence, f"{item.get('check_name')} missing evidence"
            for evidence_item in evidence:
                assert {"file", "rule", "hit", "confidence"} <= set(evidence_item)


def test_build_contract_compliance_report_prefers_task_graph_union_scope(tmp_path: Path) -> None:
    main_file = tmp_path / "app" / "main.py"
    schema_file = tmp_path / "app" / "schemas.py"
    test_file = tmp_path / "tests" / "test_api.py"
    main_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    main_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    schema_file.write_text("def shape():\n    return 2\n", encoding="utf-8")
    test_file.write_text("def test_api():\n    assert True\n", encoding="utf-8")

    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["app/main.py", "app/schemas.py", "tests/test_api.py"],
        task_card={"files_to_change": ["app/main.py", "tests/test_api.py"]},
        task_graph={
            "boundary_debt": {"status": "PASS", "details": "No physical layer-boundary debt detected.", "items": []},
            "tasks": [
                {"task_id": "T1", "task_name": "route", "layer_owner": "route", "core_files": ["app/main.py"], "invariants": ["x"], "test_proof": "y"},
                {"task_id": "T2", "task_name": "schema", "layer_owner": "schema", "core_files": ["app/schemas.py"], "invariants": ["x"], "test_proof": "y"},
            ]
        },
        prd_intake={
            "business_outcome": "Keep hydration trend summary stable",
            "source_of_truth": ["db.items"],
            "source_of_truth_canonical": ["db.items"],
            "layers": ["route", "schema"],
            "path_type": "read",
        },
        review_evidence={"status": "PASS"},
        schema_files=[],
        include_ast_checks=False,
    )

    scope_check = next(item for item in report["checks"] if item["check_name"] == "scope_drift")
    assert scope_check["status"] == "PASS"


def test_build_contract_compliance_report_merges_task_card_scope_with_task_graph(tmp_path: Path) -> None:
    source = tmp_path / "src" / "service.py"
    test_file = tmp_path / "tests" / "test_t002_service.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    test_file.write_text("def test_run():\n    assert True\n", encoding="utf-8")

    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/service.py", "tests/test_t002_service.py"],
        task_card={"files_to_change": ["src/service.py", "tests/test_t002_service.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "service", "core_files": ["src/service.py"], "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Keep service stable", "source_of_truth": [], "path_type": "read"},
        review_evidence={"status": "PASS"},
        schema_files=[],
        include_ast_checks=False,
    )

    scope_check = next(item for item in report["checks"] if item["check_name"] == "scope_drift")
    assert scope_check["status"] == "PASS"


def test_build_contract_compliance_report_surfaces_boundary_debt_as_warn(tmp_path: Path) -> None:
    main_file = tmp_path / "app" / "main.py"
    test_file = tmp_path / "tests" / "test_api.py"
    main_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    main_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    test_file.write_text("def test_api():\n    assert True\n", encoding="utf-8")

    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["app/main.py", "tests/test_api.py"],
        task_graph={
            "boundary_debt": {
                "status": "WARN",
                "details": "Multiple logical layers map to the same physical source file (1 shared file(s)).",
                "items": [
                    {
                        "file": "app/main.py",
                        "layers": ["route", "service"],
                        "tasks": ["T1", "T2"],
                        "severity": "low",
                        "owners": ["route", "service"],
                        "touched_in_feature": True,
                        "recommended_split": [
                            "route: keep request/response binding in the current route file",
                            "service: extract business orchestration to a service module",
                        ],
                    }
                ],
            },
            "tasks": [
                {"task_id": "T1", "task_name": "route", "layer_owner": "route", "core_files": ["app/main.py", "tests/test_api.py"], "invariants": ["x"], "test_proof": "y"},
                {"task_id": "T2", "task_name": "service", "layer_owner": "service", "core_files": ["app/main.py", "tests/test_api.py"], "invariants": ["x"], "test_proof": "y"},
            ],
        },
        prd_intake={
            "business_outcome": "Return hydrated summary",
            "source_of_truth": ["db.water_events"],
            "source_of_truth_canonical": ["db.water_events"],
            "layers": ["route", "service"],
            "path_type": "read",
        },
        review_evidence={"status": "PASS"},
        schema_files=[],
        include_ast_checks=False,
    )

    boundary_check = next(item for item in report["checks"] if item["check_name"] == "layer_boundary_debt")
    assert boundary_check["status"] == "WARN"
    assert boundary_check["blocking_eligible"] is False
    assert boundary_check["evidence"]
    assert "severity=low" in boundary_check["evidence"][0]["hit"]


def test_build_contract_compliance_report_warns_when_review_evidence_is_not_explicit(tmp_path: Path) -> None:
    service = tmp_path / "src" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("def run():\n    return 1\n", encoding="utf-8")
    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/service.py"],
        task_card={"files_to_change": ["src/service.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "noop", "layer_owner": "service", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Keep service stable", "source_of_truth": ["db.items"]},
        review_evidence={"status": "PASS", "source": "summary_fallback", "explicit": False},
        schema_files=[],
    )

    review_check = next(item for item in report["checks"] if item["check_name"] == "review_evidence")
    assert review_check["status"] == "WARN"
    assert "explicit review evidence missing" in review_check["details"]


def test_compliance_fail_without_evidence_is_downgraded(tmp_path: Path) -> None:
    service = tmp_path / "src" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("def run():\n    return 1\n", encoding="utf-8")
    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/service.py"],
        task_card={"files_to_change": ["src/service.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "noop", "layer_owner": "service", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Keep service stable", "source_of_truth": ["db.items"]},
        review_evidence={"status": "FAIL"},
        schema_files=[],
    )
    review_check = next(item for item in report["checks"] if item["check_name"] == "review_evidence")
    assert review_check["status"] == "WARN"
    assert review_check["blocking_eligible"] is False
    assert review_check["evidence_count"] >= 1
    assert report["status"] in {"PASS", "FAIL"}


def test_compliance_fail_with_details_only_still_downgrades(tmp_path: Path) -> None:
    service = tmp_path / "src" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("def run():\n    return 1\n", encoding="utf-8")
    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/service.py"],
        task_card={"files_to_change": ["src/service.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "noop", "layer_owner": "service", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Keep service stable", "source_of_truth": ["db.items"]},
        review_evidence={"status": "FAIL", "details": "hand-wavy failure without concrete evidence"},
        schema_files=[],
    )
    review_check = next(item for item in report["checks"] if item["check_name"] == "review_evidence")
    assert review_check["status"] == "WARN"
    assert review_check["blocking_eligible"] is False


def test_build_contract_compliance_report_includes_duplication_as_warn(tmp_path: Path, monkeypatch) -> None:
    service = tmp_path / "src" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("def run():\n    return 1\n", encoding="utf-8")

    monkeypatch.setattr(
        "kodawari.gate.checker_compliance.run_duplication_checker",
        lambda *_args, **_kwargs: SimpleNamespace(
            to_dict=lambda: {
                "status": "FAIL",
                "duplicate_block_count": 1,
                "details": "Detected 1 duplicate-code block(s).",
                "evidence": [
                    {
                        "file": "src/service.py",
                        "rule": "duplicate_code",
                        "hit": "Similar lines in 2 files",
                        "confidence": 0.95,
                    }
                ],
            }
        ),
    )

    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["src/service.py"],
        task_card={"files_to_change": ["src/service.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "noop", "layer_owner": "service", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Keep service stable", "source_of_truth": ["db.items"]},
        review_evidence={"status": "PASS"},
        schema_files=[],
    )

    duplication_check = next(item for item in report["checks"] if item["check_name"] == "duplication")
    assert duplication_check["status"] == "WARN"
    assert duplication_check["blocking_eligible"] is False
    assert duplication_check["evidence_count"] == 1


def test_build_contract_compliance_report_surfaces_domain_source_of_truth_warn(tmp_path: Path) -> None:
    ownership_path = tmp_path / "module_ownership.yaml"
    ownership_path.write_text(
        json.dumps(
            {
                "modules": {
                    "feed_service": {
                        "owner": "backend",
                        "path": "app/feed_service.py",
                        "public_api": ["build_feed"],
                        "description": "Feed assembly",
                        "forbidden_imports": [],
                        "canonical_for": ["feed assembly logic"],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    helper = tmp_path / "app" / "feed_assembly_helper.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("def build_feed_helper():\n    return []\n", encoding="utf-8")

    report = build_contract_compliance_report(
        project_root=tmp_path,
        changed_files=["app/feed_assembly_helper.py"],
        task_card={"files_to_change": ["app/feed_assembly_helper.py"]},
        task_graph={"tasks": [{"task_id": "T1", "task_name": "noop", "layer_owner": "service", "invariants": ["x"], "test_proof": "y"}]},
        prd_intake={"business_outcome": "Keep service stable", "source_of_truth": ["db.items"]},
        review_evidence={"status": "PASS"},
        schema_files=[],
    )

    domain_check = next(item for item in report["checks"] if item["check_name"] == "domain_source_of_truth")
    assert domain_check["status"] == "WARN"
    assert domain_check["evidence_count"] >= 1


def test_compliance_report_schema_allows_new_and_existing_check_names() -> None:
    payload = {
        "schema_version": "contract_first.compliance_report.v1",
        "status": "PASS",
        "checks": [
            {"check_name": "scope_drift", "status": "PASS"},
            {"check_name": "duplication", "status": "WARN"},
            {"check_name": "import_rules", "status": "PASS"},
            {"check_name": "domain_source_of_truth", "status": "PASS"},
            {"check_name": "review_evidence", "status": "WARN"},
        ],
    }

    assert validate_contract_first_payload("compliance_report", payload) == payload

    with pytest.raises(ContractFirstSchemaValidationError):
        validate_contract_first_payload(
            "compliance_report",
            {
                "schema_version": "contract_first.compliance_report.v1",
                "status": "PASS",
                "checks": [{"check_name": "unknown_check", "status": "PASS"}],
            },
        )

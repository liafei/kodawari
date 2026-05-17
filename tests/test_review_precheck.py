from __future__ import annotations

import json
from pathlib import Path

from kodawari.autopilot.review.review_precheck import compute_deterministic_findings


def test_review_precheck_detects_scope_tests_and_boundary_issues(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "files_to_change": ["app/main.py", "tests/test_main.py"],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "ARCHITECTURE_PLAN.json").write_text(
        json.dumps(
            {
                "module_boundaries": [
                    {"name": "api", "roots": ["app"]},
                    {"name": "shared", "roots": ["app/main.py"]},
                ],
                "verify_recipes": [{"surface": "api", "required": True, "command": "pytest -q"}],
            }
        ),
        encoding="utf-8",
    )

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=["app/main.py", "app/rogue.py"],
        task_card_files=["app/main.py", "tests/test_main.py"],
        invariants=["single source of truth"],
    )

    assert findings["schema_version"] == "review.precheck.v1"
    assert "app/rogue.py" in findings["out_of_scope_files"]
    assert "app/main.py" in findings["missing_test_files"]
    assert findings["cross_boundary_files"][0]["file"] == "app/main.py"
    assert findings["verify_surface_gaps"] == ["api"]


def test_review_precheck_handles_missing_contract_artifacts_best_effort(tmp_path: Path) -> None:
    planning_dir = tmp_path / "repo" / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=["app/main.py", "tests/test_main.py"],
        task_card_files=[],
        invariants=[],
    )

    assert findings["schema_version"] == "review.precheck.v1"
    assert findings["out_of_scope_files"] == []
    assert findings["verify_surface_gaps"] == []


def test_review_precheck_marks_missing_tests_as_scope_conflict_when_tests_are_out_of_scope(tmp_path: Path) -> None:
    planning_dir = tmp_path / "repo" / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=["app/main.py"],
        task_card_files=["app/main.py"],
        invariants=[],
    )

    assert findings["missing_test_files"] == []
    assert findings["test_scope_unavailable_files"] == ["app/main.py"]


def test_review_precheck_accepts_task_numbered_test_changes(tmp_path: Path) -> None:
    planning_dir = tmp_path / "repo" / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=["backend/api/v1/services/translation_core.py", "tests/test_t002_minimal_api_smoke.py"],
        task_card_files=["backend/api/v1/services/translation_core.py", "tests/test_t002_minimal_api_smoke.py"],
        invariants=[],
    )

    assert findings["missing_test_files"] == []
    assert findings["test_scope_unavailable_files"] == []


def test_review_precheck_accepts_source_only_with_verified_scoped_test(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    test_file = project_root / "tests" / "test_main.py"
    (project_root / "app").mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    (project_root / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    test_file.write_text("def test_handler():\n    assert True\n", encoding="utf-8")
    planning_dir.mkdir(parents=True)
    (planning_dir / "ARCHITECTURE_PLAN.json").write_text(
        json.dumps({"verify_recipes": [{"surface": "api", "required": True}]}),
        encoding="utf-8",
    )

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        project_root=project_root,
        changed_files=["app/main.py"],
        task_card_files=["app/main.py", "tests/test_main.py"],
        invariants=[],
        runtime_verify_check={
            "status": "PASS",
            "passed": True,
            "command_executed": True,
            "returncode": 0,
            "verify_cmd_resolved": "python -m pytest tests/test_main.py -q",
            "verify_targets": ["tests/test_main.py"],
        },
    )

    assert findings["changed_test_files"] == []
    assert findings["verified_test_files"] == ["tests/test_main.py"]
    assert findings["test_evidence_files"] == ["tests/test_main.py"]
    assert findings["missing_test_files"] == []
    assert findings["verify_surface_gaps"] == []


def test_review_precheck_rejects_summary_only_test_evidence(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    (project_root / "app").mkdir(parents=True)
    (project_root / "tests").mkdir(parents=True)
    (project_root / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (project_root / "tests" / "test_main.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    planning_dir.mkdir(parents=True)

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        project_root=project_root,
        changed_files=["app/main.py"],
        task_card_files=["app/main.py", "tests/test_main.py"],
        invariants=[],
        runtime_verify_check={
            "status": "PASS",
            "passed": True,
            "command_executed": True,
            "returncode": 0,
            "verify_cmd_resolved": "python -c pass",
            "verify_targets": ["tests/test_main.py"],
            "summary": "tests/test_main.py passed",
        },
    )

    assert findings["verified_test_files"] == []
    assert findings["missing_test_files"] == ["app/main.py"]


def test_review_precheck_rejects_non_executing_or_ignored_pytest_targets(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    (project_root / "app").mkdir(parents=True)
    (project_root / "tests").mkdir(parents=True)
    (project_root / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (project_root / "tests" / "test_main.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (project_root / "tests" / "test_other.py").write_text("def test_y():\n    assert True\n", encoding="utf-8")
    planning_dir.mkdir(parents=True)

    for command in (
        "python -m pytest --collect-only tests/test_main.py",
        "python -m pytest --co tests/test_main.py",
        "python -m pytest --fixtures tests/test_main.py",
        "python -m pytest --funcargs tests/test_main.py",
        "python -m pytest --ignore tests/test_main.py tests/test_other.py",
        "python -m pytest -k handler tests/test_main.py",
        "python -m pytest tests/test_main.py && echo ok",
        "python -m pytest tests",
    ):
        findings = compute_deterministic_findings(
            planning_dir=planning_dir,
            project_root=project_root,
            changed_files=["app/main.py"],
            task_card_files=["app/main.py", "tests/test_main.py", "tests/test_other.py"],
            invariants=[],
            runtime_verify_check={
                "status": "PASS",
                "passed": True,
                "command_executed": True,
                "returncode": 0,
                "verify_cmd_resolved": command,
                "verify_targets": ["tests/test_main.py"],
            },
        )
        assert findings["verified_test_files"] == [], command
        assert findings["missing_test_files"] == ["app/main.py"], command


def test_review_precheck_requires_verified_test_in_task_scope_and_successful_returncode(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    (project_root / "app").mkdir(parents=True)
    (project_root / "tests").mkdir(parents=True)
    (project_root / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (project_root / "tests" / "test_main.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    planning_dir.mkdir(parents=True)

    failed = compute_deterministic_findings(
        planning_dir=planning_dir,
        project_root=project_root,
        changed_files=["app/main.py"],
        task_card_files=["app/main.py", "tests/test_main.py"],
        invariants=[],
        runtime_verify_check={
            "status": "PASS",
            "passed": True,
            "command_executed": True,
            "returncode": 1,
            "verify_cmd_resolved": "python -m pytest tests/test_main.py -q",
        },
    )
    out_of_scope = compute_deterministic_findings(
        planning_dir=planning_dir,
        project_root=project_root,
        changed_files=["app/main.py"],
        task_card_files=["app/main.py"],
        invariants=[],
        runtime_verify_check={
            "status": "PASS",
            "passed": True,
            "command_executed": True,
            "returncode": 0,
            "verify_cmd_resolved": "python -m pytest tests/test_main.py -q",
        },
    )

    assert failed["verified_test_files"] == []
    assert failed["missing_test_files"] == ["app/main.py"]
    assert out_of_scope["verified_test_files"] == []
    assert out_of_scope["test_scope_unavailable_files"] == ["app/main.py"]


def test_verification_only_with_passing_runtime_verify_check_skips_surface_gaps(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({"verification_only_noop": True}), encoding="utf-8"
    )
    (planning_dir / "PLANNING_CONVERSATION.json").write_text(
        json.dumps({
            "verify_recipes": [{"surface": "backend", "required": True, "command": "pytest tests/"}],
            "module_boundaries": [],
        }),
        encoding="utf-8",
    )

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=[],
        task_card_files=[],
        invariants=[],
        runtime_verify_check={"status": "PASS", "passed": True},
    )

    assert findings["verify_surface_gaps"] == []
    assert findings["is_verification_only_task"] is True


def test_verification_only_without_verify_evidence_still_reports_surface_gaps(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({"verification_only_noop": True}), encoding="utf-8"
    )
    (planning_dir / "PLANNING_CONVERSATION.json").write_text(
        json.dumps({
            "verify_recipes": [{"surface": "backend", "required": True, "command": "pytest tests/"}],
            "module_boundaries": [],
        }),
        encoding="utf-8",
    )

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=[],
        task_card_files=[],
        invariants=[],
        runtime_verify_check=None,
    )

    assert findings["verify_surface_gaps"] == ["backend"]
    assert findings["is_verification_only_task"] is True


def test_non_verification_only_task_still_reports_surface_gaps(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir(parents=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({"task_id": "T-REG"}), encoding="utf-8"
    )
    (planning_dir / "PLANNING_CONVERSATION.json").write_text(
        json.dumps({
            "verify_recipes": [{"surface": "backend", "required": True, "command": "pytest tests/"}],
            "module_boundaries": [],
        }),
        encoding="utf-8",
    )

    findings = compute_deterministic_findings(
        planning_dir=planning_dir,
        changed_files=["src/main.py"],
        task_card_files=["src/main.py"],
        invariants=[],
        runtime_verify_check={"status": "PASS", "passed": True},
    )

    assert findings["verify_surface_gaps"] == ["backend"]
    assert findings["is_verification_only_task"] is False


def test_guard_suppresses_verify_surface_gaps_for_verification_only_task() -> None:
    from kodawari.autopilot.review.review_precheck import apply_deterministic_review_guard

    deterministic_findings = {
        "out_of_scope_files": [],
        "missing_test_files": [],
        "test_scope_unavailable_files": [],
        "cross_boundary_files": [],
        "verify_surface_gaps": ["backend"],
        "invariant_conflicts": [],
        "docs_only_changes": False,
        "is_verification_only_task": True,
    }
    review = {"approved": True, "must_fix": [], "blocking_items": [], "should_fix": []}

    guarded = apply_deterministic_review_guard(review, deterministic_findings=deterministic_findings)

    assert guarded["approved"] is True
    injected = " ".join(guarded.get("must_fix") or [])
    assert "backend" not in injected
    assert "verify surface" not in injected

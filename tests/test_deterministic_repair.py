from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.deterministic_repair import (
    apply_deterministic_repairs,
    previous_findings_with_deterministic_refs,
)
from kodawari.autopilot.planning.planning_agent import _validate_plan
from kodawari.autopilot.planning.planning_consistency import validate_plan_revision


def _write(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _task(task_id: str = "T1", **overrides: Any) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_name": f"Task {task_id}",
        "layer_owner": "service",
        "surface": "backend",
        "files_to_change": ["src/service.py"],
        "new_files": [],
        "coverage_hints": [],
        "approach": "do it",
        "invariants": ["no regression"],
        "test_plan": "pytest tests/test_service.py -q",
        "verify_cmd": "pytest tests/test_service.py -q",
        "depends_on": [],
        "forbidden_changes": [],
        "provides": [],
        "requires": [],
        "api_contracts": [],
        **overrides,
    }


def _plan(*tasks: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "summary": "plan",
        "business_outcome": "outcome",
        "out_of_scope": [],
        "source_of_truth": ["src/service.py"],
        "source_of_truth_canonical": ["src/service.py"],
        "path_type": "write",
        "layers": ["service"],
        "coverage_hints": [],
        "module_boundaries": [{"name": "core", "surface": "backend", "roots": ["src"], "layers": ["service"]}],
        "verify_recipes": [{"surface": "backend", "command": "pytest tests/test_service.py -q", "required": True, "roots": ["tests"]}],
        "approval_points": [],
        "execution_constraints": {},
        "confidence": "high",
        "confidence_issues": [],
        "tasks": list(tasks) or [_task()],
        "risks": [],
        "change_log": [],
        **overrides,
    }


def test_truncate_invariants_repairs_structural_error(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "service.py")
    _write(tmp_path / "tests" / "test_service.py")
    plan = _plan(_task(invariants=["a", "b", "c", "d", "e", "f"]))

    assert "tasks[1].invariants exceeds 5 items" in _validate_plan(plan, project_root=tmp_path)

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    assert repaired["tasks"][0]["invariants"] == ["a", "b", "c", "d", "e"]
    assert any(item["rule"] == "truncate_invariants" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_write_anchor_is_demoted_to_read_only_scope(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=["tests/test_service.py"],
        new_files=["tests/test_service.py"],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        read_only_files=["tests/test_service.py"],
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(task)

    repaired, log = apply_deterministic_repairs(plan, previous_plan=plan, project_root=tmp_path)

    repaired_task = repaired["tasks"][0]
    assert repaired_task["files_to_change"] == []
    assert repaired_task["new_files"] == []
    assert "tests/test_service.py" in repaired_task["read_only_files"]
    assert "tests/test_service.py" in repaired_task["related_existing_tests"]
    assert any(item["rule"] == "demote_verification_only_write_anchors" for item in log)
    assert any(item["location"] == "tasks[0].files_to_change" for item in log)
    assert any(
        entry["task_id"] == "T1" and "files_to_change" in entry["fields"]
        for entry in repaired["change_log"]
    )
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_no_edit_contract_does_not_block_on_raw_git_dirtiness(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
            "dirtiness_check": "git status --porcelain && git diff --name-only",
        },
        invariants=["Executor MUST NOT modify any file in the repository"],
        forbidden_changes=["Any modification to any file in the workspace"],
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(task)

    repaired, log = apply_deterministic_repairs(plan, previous_plan=plan, project_root=tmp_path)

    repaired_task = repaired["tasks"][0]
    constraints = repaired_task["execution_constraints"]
    assert "git status" not in constraints["dirtiness_check"]
    assert "repository-tracked product" in constraints["no_edit_boundary"]
    assert repaired_task["invariants"] == [
        "No edits to repository-tracked product source/test/docs/config files. Workflow-managed planning/scratch artifacts, pytest temp DB files, run outputs, and pre-existing workspace dirtiness are not product edits."
    ]
    assert any(item["rule"] == "normalize_verification_only_no_edit_contracts" for item in log)
    assert any(item["location"] == "tasks[0]" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_plan_level_constraints_are_promoted_to_task(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={"explanation": "verify only"},
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(
        task,
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
    )

    repaired, log = apply_deterministic_repairs(plan, previous_plan=plan, project_root=tmp_path)

    constraints = repaired["tasks"][0]["execution_constraints"]
    assert constraints["verification_only_noop"] is True
    assert constraints["executor_must_not_edit"] is True
    assert constraints["explanation"] == "verify only"
    assert any(item["rule"] == "ensure_verification_only_task_constraints" for item in log)
    assert any(item["location"] == "tasks[0].execution_constraints" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_workspace_cd_verify_commands_are_normalized_to_project_root(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    chained = f"cd {tmp_path} && python -m pytest tests/test_service.py -q"
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd=chained,
        test_plan=chained,
    )
    plan = _plan(task, verify_recipes=[{"surface": "backend", "command": chained, "required": True, "roots": ["tests"]}])

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    expected = "python -m pytest tests/test_service.py -q"
    assert repaired["tasks"][0]["verify_cmd"] == expected
    assert repaired["tasks"][0]["test_plan"] == expected
    assert repaired["verify_recipes"][0]["command"] == expected
    assert any(item["rule"] == "normalize_workspace_relative_verify_commands" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_work_all_gate_is_added_when_requested(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(task, approval_points=[])

    repaired, log = apply_deterministic_repairs(
        plan,
        project_root=tmp_path,
        task_direction="验收标准：workflow work-all PASS，pytest 通过",
    )

    assert any("work-all" in item["name"] for item in repaired["approval_points"])
    assert "workflow work-all PASS is required for closure" in repaired["tasks"][0]["invariants"]
    assert any(item["rule"] == "ensure_verification_only_work_all_approval" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_unrequested_smoke_gates_are_removed(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    _write(tmp_path / "tests" / "test_t001_workspace_smoke.py", "def test_smoke():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        invariants=["All requested tests pass", "Workspace smoke tests (t001 + t002) must pass"],
        coverage_hints=["Run workspace smoke tests"],
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan=(
            "Run python -m pytest tests/test_service.py -q. "
            "Then run python -m pytest tests/test_t001_workspace_smoke.py -q. All must pass."
        ),
    )
    plan = _plan(
        task,
        verify_recipes=[
            {"surface": "backend", "command": "python -m pytest tests/test_service.py -q", "required": True, "roots": ["tests"]},
            {"surface": "smoke", "command": "python -m pytest tests/test_t001_workspace_smoke.py -q", "required": True, "roots": ["tests"]},
        ],
        approval_points=[
            {"name": "requested_tests_pass", "required": True, "reason": "requested verify command passes"},
            {"name": "workspace_smoke_pass", "required": True, "reason": "t001 workspace smoke passes"},
        ],
    )

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    repaired_task = repaired["tasks"][0]
    assert repaired["approval_points"] == [
        {"name": "requested_tests_pass", "required": True, "reason": "requested verify command passes"}
    ]
    assert repaired["verify_recipes"] == [
        {"surface": "backend", "command": "python -m pytest tests/test_service.py -q", "required": True, "roots": ["tests"]}
    ]
    assert all("smoke" not in item.lower() for item in repaired_task["invariants"])
    assert repaired_task["coverage_hints"] == []
    assert "test_t001" not in repaired_task["test_plan"]
    assert any(item["rule"] == "remove_verification_only_unrequested_smoke_gates" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_page_scope_adds_frontend_file_as_read_only(tmp_path: Path) -> None:
    _write(tmp_path / "mobile" / "www" / "index.html", "<main>read later</main>\n")
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(task, source_of_truth_canonical=["tests/test_service.py"])

    repaired, log = apply_deterministic_repairs(
        plan,
        project_root=tmp_path,
        task_direction="验证稍后阅读页面和浏览历史页面",
    )

    repaired_task = repaired["tasks"][0]
    assert "mobile/www/index.html" not in repaired["source_of_truth"]
    assert "mobile/www/index.html" not in repaired["source_of_truth_canonical"]
    assert "mobile/www/index.html" in repaired_task["read_only_files"]
    assert "mobile/www/index.html" in repaired_task["do_not_change"]
    assert any(item["rule"] == "add_verification_only_frontend_read_only_scope" for item in log)
    assert any(item["location"] == "tasks[0].read_only_files" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_truth_docs_are_read_only_scope(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "任务计划_v1.1.md", "# plan\n")
    _write(tmp_path / "docs" / "启动交付与运行手册.md", "# runbook\n")
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(
        task,
        source_of_truth=["tests/test_service.py"],
        source_of_truth_canonical=["tests/test_service.py"],
        module_boundaries=[
            {
                "name": "read_later",
                "surface": "Read Later service + API routes",
                "roots": ["backend/api/v1/services/read_later_service.py", "backend/api/v1/routes/read_later_routes.py"],
                "layers": ["service", "route"],
            }
        ],
    )

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    repaired_task = repaired["tasks"][0]
    for path in ("docs/任务计划_v1.1.md", "docs/启动交付与运行手册.md"):
        assert path in repaired["source_of_truth"]
        assert path in repaired["source_of_truth_canonical"]
        assert path in repaired_task["read_only_files"]
        assert path in repaired_task["do_not_change"]
    assert any(item["rule"] == "add_verification_only_truth_docs_read_only_scope" for item in log)
    assert any(item["location"] == "tasks[0].read_only_files" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_read_later_persistence_scope_is_added(tmp_path: Path) -> None:
    _write(tmp_path / "backend" / "api" / "v1" / "services" / "edition_assembly.py", "read_later = 1\n")
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(
        task,
        source_of_truth=["tests/test_service.py"],
        source_of_truth_canonical=["tests/test_service.py"],
        module_boundaries=[
            {
                "name": "read_later",
                "surface": "Read Later service + API routes",
                "roots": ["backend/api/v1/services/read_later_service.py", "backend/api/v1/routes/read_later_routes.py"],
                "layers": ["service", "route"],
            }
        ],
    )

    repaired, log = apply_deterministic_repairs(
        plan,
        project_root=tmp_path,
        task_direction="验证 read_later / 稍后阅读 持久化证据",
    )

    path = "backend/api/v1/services/edition_assembly.py"
    repaired_task = repaired["tasks"][0]
    assert path not in repaired["source_of_truth"]
    assert path not in repaired["source_of_truth_canonical"]
    assert path in repaired_task["read_only_files"]
    assert path in repaired_task["do_not_change"]
    assert path in repaired["module_boundaries"][0]["roots"]
    assert any(item["rule"] == "add_verification_only_read_later_persistence_scope" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_implementation_paths_are_not_canonical_truth(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "spec.md", "# spec\n")
    _write(tmp_path / "backend" / "api" / "v1" / "services" / "edition_assembly.py", "read_later = 1\n")
    _write(tmp_path / "mobile" / "www" / "index.html", "<main></main>\n")
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(
        task,
        source_of_truth_canonical=[
            "docs/spec.md",
            "backend/api/v1/services/edition_assembly.py",
            "mobile/www/index.html",
        ],
    )

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    assert repaired["source_of_truth_canonical"] == ["docs/spec.md", "tests/test_service.py"]
    assert "backend/api/v1/services/edition_assembly.py" not in repaired["source_of_truth"]
    assert "mobile/www/index.html" not in repaired["source_of_truth"]
    assert "tests/test_service.py" in repaired["source_of_truth"]
    assert "tests/test_service.py" in repaired["tasks"][0]["read_only_files"]
    assert any(item["rule"] == "demote_verification_only_implementation_canonical_truth" for item in log)
    assert any(item["rule"] == "promote_verification_only_tests_to_truth" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_source_docs_are_synced_to_read_only_scope(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "custom_coverage.md", "# coverage\n")
    _write(tmp_path / "docs" / "custom_status.md", "# status\n")
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(
        task,
        source_of_truth=["docs/custom_coverage.md"],
        source_of_truth_canonical=["docs/custom_status.md"],
    )

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    repaired_task = repaired["tasks"][0]
    for path in ("docs/custom_coverage.md", "docs/custom_status.md"):
        assert path in repaired_task["read_only_files"]
        assert path in repaired_task["do_not_change"]
    assert any(item["rule"] == "sync_verification_only_source_docs_read_only_scope" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_verification_only_report_contract_gets_approval_point(tmp_path: Path) -> None:
    _write(tmp_path / "tests" / "test_service.py", "def test_ok():\n    assert True\n")
    task = _task(
        files_to_change=[],
        new_files=[],
        execution_constraints={
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        verify_cmd="python -m pytest tests/test_service.py -q",
        test_plan="python -m pytest tests/test_service.py -q",
    )
    plan = _plan(task, approval_points=[])

    repaired, log = apply_deterministic_repairs(
        plan,
        project_root=tmp_path,
        task_direction="最终报告必须包含真实命令、退出码、pytest 输出摘要",
    )

    assert any("exit code" in item["name"] and "pytest" in item["name"] for item in repaired["approval_points"])
    assert any(item["rule"] == "ensure_verification_only_report_approval" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_change_log_known_task_ref_adds_synthetic_finding_for_revision_gate() -> None:
    previous = _plan(_task("T1"), _task("T2", approach="old"))
    current = _plan(
        _task("T1"),
        _task("T2", approach="new"),
        change_log=[{"task_id": "T2", "fields": ["approach"], "reason": "Fix targeted task."}],
    )
    findings = [{"severity": "blocking", "description": "T1 needs a narrower plan."}]

    before = validate_plan_revision(previous_plan=previous, current_plan=current, previous_findings=findings)
    assert any("targets T2" in item for item in before)

    repaired, log = apply_deterministic_repairs(
        current,
        previous_plan=previous,
        previous_findings=findings,
    )
    effective_findings = previous_findings_with_deterministic_refs(findings, log)

    assert repaired["change_log"][0]["task_id"] == "T2"
    assert any(item["rule"] == "change_log_known_task_ref" for item in log)
    assert validate_plan_revision(
        previous_plan=previous,
        current_plan=repaired,
        previous_findings=effective_findings,
    ) == []


def test_missing_added_removed_task_change_log_entries_are_repaired() -> None:
    previous = _plan(_task("T099_admin_audit_log_api"))
    current = _plan(_task("T100_admin_source_crud"))

    before = validate_plan_revision(previous_plan=previous, current_plan=current, previous_findings=[])
    assert "change_log missing removed task T099_admin_audit_log_api" in before
    assert "change_log missing added task T100_admin_source_crud" in before

    repaired, log = apply_deterministic_repairs(
        current,
        previous_plan=previous,
        previous_findings=[],
    )
    effective_findings = previous_findings_with_deterministic_refs([], log)

    targets = [entry["task_id"] for entry in repaired["change_log"]]
    assert targets == ["T099_admin_audit_log_api", "T100_admin_source_crud"]
    assert [item["rule"] for item in log[:2]] == [
        "add_missing_task_change_log_entry",
        "add_missing_task_change_log_entry",
    ]
    assert validate_plan_revision(
        previous_plan=previous,
        current_plan=repaired,
        previous_findings=effective_findings,
    ) == []


def test_verify_recipes_are_deduped_and_missing_roots_are_filtered(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "service.py")
    _write(tmp_path / "tests" / "test_service.py")
    recipe = {
        "surface": "backend",
        "command": "pytest tests/test_service.py -q",
        "required": True,
        "roots": ["tests", "missing"],
    }
    plan = _plan(
        _task(),
        verify_recipes=[
            recipe,
            {
                "surface": "backend",
                "command": "pytest tests/test_service.py -q",
                "required": True,
                "roots": ["tests"],
            },
        ],
    )

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    assert repaired["verify_recipes"] == [
        {
            "surface": "backend",
            "command": "pytest tests/test_service.py -q",
            "required": True,
            "roots": ["tests"],
        }
    ]
    assert [item["rule"] for item in log] == [
        "filter_missing_verify_recipe_roots",
        "dedupe_verify_recipes",
    ]


def test_owned_files_are_removed_from_read_only_scope(tmp_path: Path) -> None:
    _write(tmp_path / "backend" / "api" / "v1" / "router.py")
    _write(tmp_path / "tests" / "test_service.py")
    plan = _plan(
        _task(
            files_to_change=["backend/api/v1/router.py"],
            read_only_files=["backend/api/v1/router.py", "backend/api/responses.py"],
            do_not_change=["backend/api/v1/router.py"],
        )
    )

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)
    task = repaired["tasks"][0]

    assert task["read_only_files"] == ["backend/api/responses.py"]
    assert task["do_not_change"] == []
    assert [item["rule"] for item in log] == [
        "remove_owned_files_from_read_only",
        "remove_owned_files_from_read_only",
    ]


def test_reviewer_requested_read_only_scope_conflict_is_promoted_safely(tmp_path: Path) -> None:
    for rel in (
        "backend/api/v1/routes/admin_sources.py",
        "backend/api/v1/router.py",
        "tests/test_admin_sources.py",
    ):
        _write(tmp_path / rel)
    previous = _plan(_task("T099_admin_audit_log_api", files_to_change=["backend/api/v1/routes/admin_audit.py"]))
    current = _plan(
        _task(
            "T100_admin_source_crud",
            files_to_change=[
                "backend/api/v1/routes/admin_sources.py",
                "tests/test_admin_sources.py",
            ],
            read_only_files=["backend/api/v1/router.py"],
        )
    )
    findings = [
        {
            "severity": "blocking",
            "category": "consistency with project conventions",
            "description": (
                "backend/api/v1/router.py is required for route registration but is absent from "
                "files_to_change and currently listed under read_only_files."
            ),
            "recommendation": "加入 backend/api/v1/router.py 到 files_to_change，并从 read_only_files 移除。",
        }
    ]

    repaired, log = apply_deterministic_repairs(
        current,
        previous_plan=previous,
        previous_findings=findings,
        project_root=tmp_path,
    )
    task = repaired["tasks"][0]
    effective_findings = previous_findings_with_deterministic_refs(findings, log)

    assert task["files_to_change"] == [
        "backend/api/v1/routes/admin_sources.py",
        "tests/test_admin_sources.py",
        "backend/api/v1/router.py",
    ]
    assert task["read_only_files"] == []
    assert any(item["rule"] == "promote_review_requested_write_path" for item in log)
    assert validate_plan_revision(
        previous_plan=previous,
        current_plan=repaired,
        previous_findings=effective_findings,
    ) == []


def test_scope_conflict_promote_refuses_do_not_change_and_full_scope(tmp_path: Path) -> None:
    for rel in ("src/a.py", "src/b.py", "src/c.py", "src/router.py"):
        _write(tmp_path / rel)
    findings = [
        {
            "severity": "blocking",
            "category": "scope correctness",
            "description": "src/router.py must be added to files_to_change and removed from read_only_files.",
            "recommendation": "add src/router.py to files_to_change",
        }
    ]
    do_not_change_plan = _plan(
        _task(
            files_to_change=["src/a.py"],
            read_only_files=["src/router.py"],
            do_not_change=["src/router.py"],
        )
    )
    full_scope_plan = _plan(
        _task(
            files_to_change=["src/a.py", "src/b.py", "src/c.py"],
            read_only_files=["src/router.py"],
        )
    )
    forbidden_plan = _plan(
        _task(
            files_to_change=["src/a.py"],
            read_only_files=["src/router.py"],
            forbidden_changes=[{"paths": ["src/router.py"]}],
        )
    )

    blocked, blocked_log = apply_deterministic_repairs(
        do_not_change_plan,
        previous_findings=findings,
        project_root=tmp_path,
    )
    full, full_log = apply_deterministic_repairs(
        full_scope_plan,
        previous_findings=findings,
        project_root=tmp_path,
    )
    forbidden, forbidden_log = apply_deterministic_repairs(
        forbidden_plan,
        previous_findings=findings,
        project_root=tmp_path,
    )

    assert blocked["tasks"][0]["files_to_change"] == ["src/a.py"]
    assert full["tasks"][0]["files_to_change"] == ["src/a.py", "src/b.py", "src/c.py"]
    assert forbidden["tasks"][0]["files_to_change"] == ["src/a.py"]
    assert not any(item["rule"] == "promote_review_requested_write_path" for item in blocked_log)
    assert not any(item["rule"] == "promote_review_requested_write_path" for item in full_log)
    assert not any(item["rule"] == "promote_review_requested_write_path" for item in forbidden_log)


def test_scope_conflict_promote_refuses_ambiguous_task_match(tmp_path: Path) -> None:
    _write(tmp_path / "src/router.py")
    _write(tmp_path / "src/a.py")
    _write(tmp_path / "src/b.py")
    plan = _plan(
        _task("T1", files_to_change=["src/a.py"], read_only_files=["src/router.py"]),
        _task("T2", files_to_change=["src/b.py"], read_only_files=["src/router.py"]),
    )
    findings = [
        {
            "severity": "blocking",
            "category": "scope correctness",
            "description": "src/router.py is in read_only_files but must be included in files_to_change.",
            "recommendation": "add src/router.py to files_to_change",
        }
    ]

    repaired, log = apply_deterministic_repairs(plan, previous_findings=findings, project_root=tmp_path)

    assert repaired["tasks"][0]["files_to_change"] == ["src/a.py"]
    assert repaired["tasks"][1]["files_to_change"] == ["src/b.py"]
    assert not any(item["rule"] == "promote_review_requested_write_path" for item in log)


def test_keep_passing_tests_are_demoted_to_verify_only_scope(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "service.py")
    _write(tmp_path / "tests" / "test_primary.py")
    _write(tmp_path / "tests" / "test_t096_social_aggregation_api.py")
    plan = _plan(
        _task(
            files_to_change=[
                "src/service.py",
                "tests/test_primary.py",
                "tests/test_t096_social_aggregation_api.py",
            ],
            related_existing_tests=[],
            read_only_files=[],
            test_plan=(
                "Add new assertions to tests/test_primary.py. "
                "Add helper tests to test_t096. "
                "All existing regression tests must pass."
            ),
        )
    )

    repaired, log = apply_deterministic_repairs(
        plan,
        project_root=tmp_path,
        task_direction=(
            "Update tests/test_primary.py for the new behavior and keep "
            "tests/test_t096_social_aggregation_api.py passing."
        ),
    )
    task = repaired["tasks"][0]

    assert task["files_to_change"] == ["src/service.py", "tests/test_primary.py"]
    assert task["related_existing_tests"] == ["tests/test_t096_social_aggregation_api.py"]
    assert task["read_only_files"] == ["tests/test_t096_social_aggregation_api.py"]
    assert "test_t096" not in task["test_plan"]
    assert "tests/test_primary.py" in task["test_plan"]
    assert any(item["rule"] == "protect_verify_only_task_direction_paths" for item in log)


def test_keep_passing_tests_stay_writable_when_plan_bundles_test_edits(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "service.py")
    _write(tmp_path / "tests" / "test_t096_social_aggregation_api.py")
    plan = _plan(
        _task(
            task_name="Implement service behavior and add sorting-edge-case tests",
            files_to_change=["src/service.py", "tests/test_t096_social_aggregation_api.py"],
            related_existing_tests=[],
            read_only_files=[],
            test_plan="Add unit tests for blank published_at ordering and thread_id tiebreaker.",
            coverage_hints=["Add unit tests for sorting edge cases"],
        ),
        execution_constraints={"bundle_implementation_and_tests": True, "require_test_in_task": True},
    )

    repaired, log = apply_deterministic_repairs(
        plan,
        project_root=tmp_path,
        task_direction=(
            "Complete the live endpoint ranking and keep "
            "tests/test_t096_social_aggregation_api.py passing."
        ),
    )
    task = repaired["tasks"][0]

    assert task["files_to_change"] == ["src/service.py", "tests/test_t096_social_aggregation_api.py"]
    assert task["read_only_files"] == []
    assert not any(item["rule"] == "protect_verify_only_task_direction_paths" for item in log)


def test_parallel_file_conflicts_are_serialized_before_validation(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "db_schema_ops.py")
    _write(tmp_path / "tests" / "test_service.py")
    plan = _plan(
        _task("T2", files_to_change=["src/db_schema_ops.py"]),
        _task("T6", files_to_change=["src/db_schema_ops.py"]),
    )

    assert any("both claim files_to_change" in item for item in _validate_plan(plan, project_root=tmp_path))

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    assert repaired["tasks"][1]["depends_on"] == ["T2"]
    assert repaired["taskgraph_resolution_log"][0]["depends_on_added"] == "T2"
    assert any(item["rule"] == "serialize_parallel_file_conflicts" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_missing_layer_owner_is_inferred_before_validation(tmp_path: Path) -> None:
    _write(tmp_path / "backend" / "api" / "v1" / "routes" / "review_routes.py")
    _write(tmp_path / "tests" / "test_audio.py")
    plan = _plan(
        _task(
            "T2",
            layer_owner="",
            surface="POST /api/v1/audio/summary",
            files_to_change=["backend/api/v1/routes/review_routes.py", "tests/test_audio.py"],
        )
    )

    assert "tasks[1].layer_owner is required" in _validate_plan(plan, project_root=tmp_path)

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)

    assert repaired["tasks"][0]["layer_owner"] == "route"
    assert any(item["rule"] == "infer_missing_layer_owner" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []


def test_conflicting_api_contract_variants_are_collapsed(tmp_path: Path) -> None:
    _write(tmp_path / "backend" / "api" / "v1" / "routes" / "review_routes.py")
    _write(tmp_path / "tests" / "test_audio.py")
    plan = _plan(
        _task(
            "T2",
            layer_owner="route",
            surface="POST /api/v1/audio/summary",
            files_to_change=["backend/api/v1/routes/review_routes.py", "tests/test_audio.py"],
            api_contracts=[
                {
                    "method": "POST",
                    "endpoint": "/api/v1/audio/summary",
                    "response_shape": {"status": "OK", "data": {"audio_id": "string"}},
                },
                {
                    "method": "POST",
                    "endpoint": "/api/v1/audio/summary",
                    "response_shape": {"status": "OK", "data": {"degraded": True}},
                },
            ],
            provides=[
                {
                    "kind": "api_response",
                    "method": "POST",
                    "endpoint": "/api/v1/audio/summary",
                    "response_shape": {"status": "OK", "data": {"audio_available": False}},
                }
            ],
        )
    )

    assert any("api_contracts conflict for POST /api/v1/audio/summary" in item for item in _validate_plan(plan, project_root=tmp_path))

    repaired, log = apply_deterministic_repairs(plan, project_root=tmp_path)
    task = repaired["tasks"][0]

    assert len(task["api_contracts"]) == 1
    assert "variants" in task["api_contracts"][0]["response_shape"]
    assert task["provides"][0]["response_shape"] == task["api_contracts"][0]["response_shape"]
    assert any(item["rule"] == "normalize_api_contract_response_shape" for item in log)
    assert _validate_plan(repaired, project_root=tmp_path) == []

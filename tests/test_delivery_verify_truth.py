import json
from pathlib import Path

from kodawari.cli.delivery_verify import _surface_planning_available, build_verify_report


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_build_verify_report_prefers_authoritative_execution_changed_files_over_stale_task_run(
    tmp_path: Path,
) -> None:
    feature = "verify-execution-truth"
    planning_dir = tmp_path / "planning" / feature
    current_file = tmp_path / "app" / "main.py"
    stale_file = tmp_path / "app" / "stale.py"
    current_file.parent.mkdir(parents=True, exist_ok=True)
    current_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    stale_file.write_text("def stale():\n    return 0\n", encoding="utf-8")

    _write_json(
        planning_dir / ".execution_result.json",
        {
            "backend": "claude_code",
            "status": "PASS",
            "changed_files": ["app/main.py"],
        },
    )
    _write_json(
        planning_dir / ".task_run_result.json",
        {
            "task_delta_changed_files": ["app/stale.py"],
            "changed_files": ["app/stale.py"],
        },
    )

    payload = build_verify_report(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature=feature,
        verify_command='python -c "print(\'verify ok\')"',
    )

    assert payload["changed_files"]["source"] == ".execution_result.json.changed_files"
    assert payload["changed_files"]["items"] == ["app/main.py"]
    assert payload["input_confidence"] == "curated"


def test_build_verify_report_reuses_passed_execution_verify_summary_before_surface_planning(
    tmp_path: Path,
) -> None:
    feature = "verify-runtime-summary"
    planning_dir = tmp_path / "planning" / feature
    changed_files = [
        "docs/plan.md",
        "mobile/www/index.html",
        "tests/test_t077_external_trends_frontend_contract.py",
    ]
    for relpath in changed_files:
        path = tmp_path / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
    _write_json(
        planning_dir / "REPO_INVENTORY.json",
        {
            "schema_version": "contract_first.repo_inventory.v1",
            "generated_at": "2026-05-08T00:00:00+00:00",
            "project_root": str(tmp_path),
            "mode": "existing",
            "archetype": "newsapp",
            "capabilities": ["docs", "frontend"],
            "project_layout": {"code_roots": ["mobile/www"], "test_roots": ["tests"]},
            "surfaces": [
                {"name": "docs", "roots": ["docs"], "verify_command": ""},
                {"name": "frontend", "roots": ["mobile/www"], "verify_command": "npm test"},
            ],
            "verify_surfaces": [
                {"name": "docs", "verify_command": ""},
                {"name": "frontend", "verify_command": "npm test"},
            ],
        },
    )
    scoped_command = "python -m pytest tests/test_t077_external_trends_frontend_contract.py -q"
    _write_json(
        planning_dir / ".execution_result.json",
        {
            "schema_version": "execution.result.v1",
            "feature": feature,
            "task": "T077: External Trends Hot Page Entry",
            "backend": "openai_tool_use",
            "status": "PASS",
            "changed_files": changed_files,
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "returncode": 0,
            "artifacts": changed_files,
            "error_code": "",
            "blocking_reason": "",
            "summary": "executor completed",
            "verify_summary": {
                "status": "PASS",
                "passed": True,
                "mode": "command",
                "source": "verify_command",
                "verify_cmd": scoped_command,
                "verify_cmd_resolved": scoped_command,
                "verify_target_source": "explicit_command",
                "verify_targets": ["tests/test_t077_external_trends_frontend_contract.py"],
                "summary": "12 passed",
                "blocking_reason": "",
                "command_executed": True,
                "returncode": 0,
                "stdout_excerpt": "12 passed",
                "stderr_excerpt": "",
                "artifacts": changed_files,
            },
        },
    )
    _write_json(
        planning_dir / ".verify_report.json",
        {
            "schema_version": "verify.report.v1",
            "generated_at": "2026-05-08T00:00:00+00:00",
            "feature": feature,
            "planning_dir": str(planning_dir.resolve()),
            "entrypoint": "kodawari verify",
            "requested_command": "pytest -q",
            "requested_command_kind": "default",
            "changed_files": {"source": ".execution_result.json.changed_files", "items": changed_files, "count": 3},
            "input_confidence": "curated",
            "status": "BLOCKED",
            "verify_scope_mode": "surface_plan",
            "verify_check": {
                "status": "BLOCKED",
                "passed": False,
                "mode": "planning_guard",
                "source": "verify_recipe_missing",
                "verify_cmd": "pytest -q",
                "verify_cmd_resolved": "pytest -q",
                "verify_target_source": "verify_recipe_missing",
                "verify_targets": [],
                "summary": "surface 'docs' has no deterministic verify recipe.",
                "blocking_reason": "surface 'docs' has no deterministic verify recipe.",
                "command_executed": False,
            },
        },
    )

    payload = build_verify_report(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature=feature,
    )

    assert payload["status"] == "PASS"
    assert payload["requested_command"] == scoped_command
    assert payload["requested_command_kind"] == "inline"
    assert payload["verify_scope_mode"] == "custom"
    assert payload["verify_check"]["source"] == ".execution_result.json.verify_summary"
    artifact = json.loads((planning_dir / ".verify_report.json").read_text(encoding="utf-8"))
    assert artifact["status"] == "PASS"
    assert artifact["verify_check"]["stdout_excerpt"] == "12 passed"


def test_build_verify_report_marks_verified_noop_execution_summary_curated(
    tmp_path: Path,
) -> None:
    feature = "verify-noop-runtime-summary"
    planning_dir = tmp_path / "planning" / feature
    scoped_command = "python -m pytest tests/test_read_later.py tests/test_history.py -v"
    _write_json(
        planning_dir / ".execution_result.json",
        {
            "schema_version": "execution.result.v1",
            "feature": feature,
            "task": "V001: verification-only closure",
            "backend": "openai_tool_use",
            "status": "PASS",
            "changed_files": [],
            "verification_only_noop": True,
            "verify_summary": {
                "status": "PASS",
                "passed": True,
                "mode": "command",
                "source": "verify_command",
                "verify_cmd": scoped_command,
                "verify_cmd_resolved": scoped_command,
                "verify_target_source": "explicit_command",
                "verify_targets": [],
                "summary": "48 passed",
                "blocking_reason": "",
                "command_executed": True,
                "returncode": 0,
                "stdout_excerpt": "48 passed",
                "stderr_excerpt": "",
            },
        },
    )

    payload = build_verify_report(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature=feature,
    )

    assert payload["status"] == "PASS"
    assert payload["changed_files"]["source"] == "none"
    assert payload["input_confidence"] == "curated"
    assert payload["verify_check"]["source"] == ".execution_result.json.verify_summary"
    artifact = json.loads((planning_dir / ".verify_report.json").read_text(encoding="utf-8"))
    assert artifact["input_confidence"] == "curated"


def test_build_verify_report_ignores_broad_execution_verify_summary(
    tmp_path: Path,
) -> None:
    feature = "verify-runtime-summary-broad"
    planning_dir = tmp_path / "planning" / feature
    target = tmp_path / "tests" / "test_runtime.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def test_runtime():\n    assert True\n", encoding="utf-8")
    _write_json(
        planning_dir / ".execution_result.json",
        {
            "backend": "openai_tool_use",
            "status": "PASS",
            "changed_files": ["tests/test_runtime.py"],
            "verify_summary": {
                "status": "PASS",
                "passed": True,
                "verify_cmd": "pytest -q",
                "command_executed": True,
                "returncode": 0,
            },
        },
    )

    payload = build_verify_report(
        project_root=tmp_path,
        planning_dir=planning_dir,
        feature=feature,
    )

    assert payload["status"] == "PASS"
    assert payload["requested_command"] == "pytest -q"
    assert payload["verify_check"]["source"] == "verify_command"
    assert payload["verify_check"]["verify_target_source"] == "changed_test_files"


def test_surface_planning_available_when_planning_conversation_exists(tmp_path: Path) -> None:
    feature = "verify-surface-conversation"
    planning_dir = tmp_path / "planning" / feature
    _write_json(
        planning_dir / "PLANNING_CONVERSATION.json",
        {
            "schema_version": "planning.conversation.v1",
            "task_direction": "demo",
            "status": "approved",
        },
    )
    assert _surface_planning_available(planning_dir) is True

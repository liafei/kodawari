from pathlib import Path

import pytest

from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine
from kodawari.autopilot import execution_artifacts


def _context(project_root: Path, planning_dir: Path, *, task_id: str = "T1") -> dict[str, object]:
    return {
        "project_root": str(project_root),
        "planning_dir": str(planning_dir),
        "task_id": task_id,
        "requested_action": "codex_implement",
        "task_invariants": ["single source of truth"],
    }


def test_run_execution_backend_reuses_existing_pass_result_for_changed_files_missing_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")

    previous = execution_artifacts.build_execution_result(
        feature="demo",
        task="T1: implement",
        backend="claude_code",
        status="PASS",
        changed_files=["app/main.py"],
        artifacts=["app/main.py"],
        summary="prior successful execution",
    )
    execution_artifacts.write_execution_result(planning_dir / ".execution_result.json", previous)

    blocked = execution_artifacts.build_execution_result(
        feature="demo",
        task="T1: implement",
        backend="claude_code",
        status="BLOCKED",
        changed_files=[],
        returncode=0,
        error_code="CLAUDE_CODE_CHANGED_FILES_MISSING",
        blocking_reason="claude_code execution completed without deterministic changed files",
        summary="claude_code did not produce deterministic changed files",
    )

    monkeypatch.setattr(
        execution_artifacts,
        "run_registered_execution_backend",
        lambda backend, invocation: dict(blocked),
    )

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="claude_code",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context=_context(project_root, planning_dir),
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["status"] == "PASS"
    assert result["execution_result"]["changed_files"] == ["app/main.py"]
    assert "rerun reused existing deterministic task changes" in result["execution_result"]["summary"]


def test_run_execution_backend_does_not_reuse_out_of_scope_previous_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    main_file = project_root / "app" / "main.py"
    other_file = project_root / "app" / "other.py"
    main_file.parent.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)
    main_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    other_file.write_text("def other():\n    return 1\n", encoding="utf-8")

    previous = execution_artifacts.build_execution_result(
        feature="demo",
        task="T1: implement",
        backend="claude_code",
        status="PASS",
        changed_files=["app/other.py"],
        artifacts=["app/other.py"],
        summary="prior successful execution",
    )
    execution_artifacts.write_execution_result(planning_dir / ".execution_result.json", previous)

    blocked = execution_artifacts.build_execution_result(
        feature="demo",
        task="T1: implement",
        backend="claude_code",
        status="BLOCKED",
        changed_files=[],
        returncode=0,
        error_code="CLAUDE_CODE_CHANGED_FILES_MISSING",
        blocking_reason="claude_code execution completed without deterministic changed files",
        summary="claude_code did not produce deterministic changed files",
    )

    monkeypatch.setattr(
        execution_artifacts,
        "run_registered_execution_backend",
        lambda backend, invocation: dict(blocked),
    )

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="claude_code",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context=_context(project_root, planning_dir),
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "blocked"
    assert result["execution_result"]["error_code"] == "CLAUDE_CODE_CHANGED_FILES_MISSING"


def test_run_execution_backend_accepts_explicit_idempotent_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    monkeypatch.setenv("WORKFLOW_ACCEPT_IDEMPOTENT_NOOP", "1")

    blocked = execution_artifacts.build_execution_result(
        feature="demo",
        task="T1: implement",
        backend="claude_code",
        status="BLOCKED",
        changed_files=[],
        returncode=0,
        error_code="CLAUDE_CODE_CHANGED_FILES_MISSING",
        blocking_reason="claude_code execution completed without deterministic changed files",
        summary="claude_code did not produce deterministic changed files",
    )

    monkeypatch.setattr(
        execution_artifacts,
        "run_registered_execution_backend",
        lambda backend, invocation: dict(blocked),
    )

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="claude_code",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context=_context(project_root, planning_dir),
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["status"] == "PASS"
    assert result["execution_result"]["changed_files"] == ["app/main.py"]
    assert result["execution_result"]["idempotent_noop_accepted"] is True


def test_run_execution_backend_reuses_state_changed_files_when_previous_result_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    (planning_dir / ".autopilot_state.json").write_text(
        '{"feature":"demo","changed_files":["app/main.py"]}',
        encoding="utf-8",
    )

    blocked = execution_artifacts.build_execution_result(
        feature="demo",
        task="T1: implement",
        backend="claude_code",
        status="BLOCKED",
        changed_files=[],
        returncode=0,
        error_code="CLAUDE_CODE_CHANGED_FILES_MISSING",
        blocking_reason="claude_code execution completed without deterministic changed files",
        summary="claude_code did not produce deterministic changed files",
    )

    monkeypatch.setattr(
        execution_artifacts,
        "run_registered_execution_backend",
        lambda backend, invocation: dict(blocked),
    )

    result = execution_artifacts.run_execution_backend(
        config=execution_artifacts.ExecutionBackendConfig(
            backend="claude_code",
            command="",
            project_root=project_root,
            planning_dir=planning_dir,
            feature="demo",
        ),
        task="T1: implement",
        context=_context(project_root, planning_dir),
        allowed_files=["app/main.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["changed_files"] == ["app/main.py"]


class _BlockedExecutionAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "blocked",
            "reason": "CLAUDE_CODE_CHANGED_FILES_MISSING",
            "blocking_reason": "claude_code execution completed without deterministic changed files",
            "execution_result": {
                "schema_version": "execution.result.v1",
                "feature": "demo",
                "task": "T1: implement",
                "backend": "claude_code",
                "status": "BLOCKED",
                "changed_files": [],
                "error_code": "CLAUDE_CODE_CHANGED_FILES_MISSING",
                "blocking_reason": "claude_code execution completed without deterministic changed files",
                "summary": "claude_code did not produce deterministic changed files",
            },
            "execution_artifacts": {
                ".execution_result.json": "E:/demo/.execution_result.json",
            },
        }


def test_engine_surfaces_blocked_execution_artifacts_in_loop_result(tmp_path: Path) -> None:
    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="demo", max_cycles=1),
        adapter=_BlockedExecutionAdapter(),
    )

    result = engine.run_collaboration_loop(
        task_label="T1: implement",
        task_scope="scope",
    )

    assert result["reason"] == "CLAUDE_CODE_CHANGED_FILES_MISSING"
    assert result["execution_result"]["error_code"] == "CLAUDE_CODE_CHANGED_FILES_MISSING"
    assert result["execution_artifacts"][".execution_result.json"].endswith(".execution_result.json")

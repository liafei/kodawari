from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from kodawari.autopilot import execution_artifacts, execution_backend, execution_claude_code


def _backend_context(project_root: Path, planning_dir: Path) -> dict[str, Any]:
    return {
        "project_root": str(project_root),
        "planning_dir": str(planning_dir),
        "task_id": "T1",
        "requested_action": "implement",
    }


def test_claude_code_workspace_contains_project_runtime_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "watercare-app"
    planning_dir = project_root / "planning" / "demo"
    schemas_file = project_root / "app" / "schemas.py"
    main_file = project_root / "app" / "main.py"
    db_file = project_root / "app" / "database.py"
    test_file = project_root / "tests" / "test_api.py"
    planning_dir.mkdir(parents=True, exist_ok=True)
    schemas_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    schemas_file.write_text("VALUE = 'before'\n", encoding="utf-8")
    main_file.write_text("from app.schemas import VALUE\n", encoding="utf-8")
    db_file.write_text("DB_URL = 'sqlite:///test.db'\n", encoding="utf-8")
    test_file.write_text(
        "from app.database import DB_URL\nfrom app.main import VALUE\n",
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    def _fake_run(command: Any, **kwargs: Any) -> Any:
        del command
        execution_root = Path(str(kwargs.get("cwd") or "")).resolve()
        captured["cwd"] = execution_root
        assert execution_root != project_root.resolve()
        assert str(execution_root).startswith(str((planning_dir / ".parallel_workers").resolve()))
        assert (execution_root / "app" / "schemas.py").exists()
        assert (execution_root / "app" / "main.py").exists()
        assert (execution_root / "app" / "database.py").exists()
        assert (execution_root / "tests" / "test_api.py").exists()
        assert not (execution_root / "planning" / "demo").exists()
        (execution_root / "app" / "schemas.py").write_text("VALUE = 'after'\n", encoding="utf-8")
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
        context=_backend_context(project_root, planning_dir),
        allowed_files=["app/schemas.py", "tests/test_api.py"],
    )

    assert result["status"] == "done"
    assert result["execution_result"]["status"] == "PASS"
    assert result["execution_result"]["changed_files"] == ["app/schemas.py"]
    assert schemas_file.read_text(encoding="utf-8") == "VALUE = 'after'\n"
    assert main_file.read_text(encoding="utf-8") == "from app.schemas import VALUE\n"
    assert db_file.read_text(encoding="utf-8") == "DB_URL = 'sqlite:///test.db'\n"
    assert test_file.read_text(encoding="utf-8") == "from app.database import DB_URL\nfrom app.main import VALUE\n"

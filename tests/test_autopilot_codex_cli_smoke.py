from __future__ import annotations

import json
from pathlib import Path
import sys

from tests.generic_canary_support import commit_all, create_test_shims, ensure_git_repo, run_cli, workflow_env


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _prepare_repo(project_root: Path) -> Path:
    _write_text(project_root / ".gitignore", "planning/\n.claude/\n__pycache__/\n.pytest_cache/\n")
    _write_text(project_root / "app" / "main.py", "def handler() -> str:\n    return 'ok'\n")
    _write_text(project_root / "tests" / "test_api.py", "def test_api() -> None:\n    assert True\n")
    verify_script = project_root / "scripts" / "verify_autopilot.py"
    _write_text(verify_script, "print('autopilot verify ok')\n")
    requirements = project_root / "requirements.txt"
    _write_text(requirements, "Update app/main.py and tests/test_api.py for autopilot codex smoke.\n")
    return verify_script


def test_autopilot_codex_cli_smoke(tmp_path: Path) -> None:
    project_root = tmp_path / "autopilot-smoke"
    project_root.mkdir(parents=True, exist_ok=True)
    verify_script = _prepare_repo(project_root)
    ensure_git_repo(project_root)
    commit_all(project_root, message="baseline")
    env = workflow_env(
        shim_dir=create_test_shims(tmp_path),
        extra_env={"WORKFLOW_SELF_REVIEW_BACKEND": "noop_test_only"},
    )
    verify_cmd = f'"{Path(sys.executable).resolve()}" "{verify_script}"'
    rc, payload, _ = run_cli(
        "autopilot",
        "--project-root",
        str(project_root),
        "--feature",
        "autopilot-codex-smoke",
        "--requirements-file",
        str(project_root / "requirements.txt"),
        "--executor-backend",
        "codex_cli",
        "--verify-cmd",
        verify_cmd,
        env=env,
    )

    assert rc in {0, 1}
    planning_dir = Path(str(payload["planning_dir"]))
    assert payload["status"] in {"ok", "blocked"}
    assert payload["execution_backend"] == "codex_cli"
    assert payload["execution_backend_capabilities"]["backend"] == "codex_cli"
    assert payload["execution_backend_capabilities"]["implemented"] is True
    assert payload["execution_result"]["backend"] == "codex_cli"
    assert (planning_dir / ".execution_result.json").exists()
    status_rc, status_payload, _ = run_cli(
        "status",
        "--project-root",
        str(project_root),
        "--feature",
        "autopilot-codex-smoke",
        env=env,
    )
    assert status_rc == 0
    assert status_payload["execution_backend"] == "codex_cli"
    assert status_payload["execution_backend_capabilities"]["backend"] == "codex_cli"
    assert status_payload["execution_backend_capabilities"]["implemented"] is True
    assert status_payload["review_mode"] in {"", "simulated"}
    assert status_payload["tokens_used"] >= 0
    assert "token_budget" in status_payload

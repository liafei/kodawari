from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


@dataclass(frozen=True)
class CanaryCase:
    name: str
    feature: str
    archetype: str
    mode: str
    prd_text: str
    preferred_surface: str
    expected_verify_surfaces: tuple[str, ...]
    capabilities: tuple[str, ...] = ()
    layout_kind: str = "existing"


def workflow_env(*, shim_dir: Path, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    existing = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = str(SRC_ROOT) if not existing else f"{SRC_ROOT}{os.pathsep}{existing}"
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    env["WORKFLOW_CODEX_EXECUTABLE"] = str((shim_dir / "fake_codex.py").resolve())
    env["WORKFLOW_REVIEW_ENABLED"] = "0"
    env["WORKFLOW_FAKE_CODEX_MODE"] = env.get("WORKFLOW_FAKE_CODEX_MODE", "pass")
    env["WORKFLOW_SDK_TEST_MODE"] = "1"
    env.pop("PYTEST_CURRENT_TEST", None)
    if extra_env:
        env.update({key: str(value) for key, value in extra_env.items()})
    return env


def run_cli(
    *args: str,
    env: dict[str, str],
    cwd: Path = REPO_ROOT,
) -> tuple[int, dict[str, Any], subprocess.CompletedProcess[str]]:
    run = subprocess.run(
        [sys.executable, "-m", "kodawari.cli.main", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    payload = json.loads(run.stdout) if str(run.stdout or "").strip() else {}
    return run.returncode, payload, run


def create_test_shims(root: Path) -> Path:
    shim_dir = (root / "shims").resolve()
    shim_dir.mkdir(parents=True, exist_ok=True)
    _write_text(shim_dir / "fake_codex.py", _fake_codex_script())
    _write_text(shim_dir / "codex.cmd", _shim_wrapper("fake_codex.py"))
    _write_text(shim_dir / "fake_npm.py", _fake_npm_script())
    _write_text(shim_dir / "npm.cmd", _shim_wrapper("fake_npm.py"))
    return shim_dir


def ensure_git_repo(project_root: Path) -> None:
    if shutil.which("git") is None:
        raise RuntimeError("git is required for generic canary proof tests")
    _git(project_root, "init")
    _git(project_root, "config", "user.email", "workflow@example.com")
    _git(project_root, "config", "user.name", "kodawari")


def commit_all(project_root: Path, *, message: str) -> None:
    _git(project_root, "add", ".")
    _git(project_root, "commit", "-m", message)


def prepare_existing_repo(project_root: Path, case: CanaryCase) -> None:
    _write_text(project_root / ".gitignore", "planning/\n.claude/\n__pycache__/\n.pytest_cache/\nnode_modules/\n")
    if case.name == "fastapi_api":
        _prepare_fastapi_repo(project_root)
    elif case.name == "flask_api":
        _prepare_flask_repo(project_root)
    elif case.name == "django_web":
        _prepare_django_repo(project_root)
    elif case.name == "node_api":
        _prepare_node_repo(project_root)
    elif case.name == "monorepo_workspace":
        _prepare_monorepo_repo(project_root)
    else:
        raise ValueError(f"unsupported existing canary case: {case.name}")


def write_prd(project_root: Path, *, case: CanaryCase) -> Path:
    prd_path = project_root / "PRD.md"
    _write_text(prd_path, case.prd_text + "\n")
    return prd_path


def run_happy_path_chain(
    *,
    project_root: Path,
    case: CanaryCase,
    env: dict[str, str],
) -> dict[str, Any]:
    prd_path = write_prd(project_root, case=case)
    intake_rc, intake_payload, _ = run_cli(
        "prd-intake",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--prd",
        str(prd_path),
        env=env,
    )
    assert intake_rc == 0, intake_payload
    architecture_path = ""
    if case.mode == "greenfield" or len(case.expected_verify_surfaces) > 1 or case.capabilities:
        arch_args = [
            "architecture-plan",
            "--project-root",
            str(project_root),
            "--feature",
            case.feature,
            "--intake",
            intake_payload["artifacts"]["PRD_INTAKE.json"],
            "--mode",
            case.mode,
            "--archetype",
            case.archetype,
        ]
        for capability in case.capabilities:
            arch_args.extend(["--capability", capability])
        arch_rc, arch_payload, _ = run_cli(*arch_args, env=env)
        assert arch_rc == 0, arch_payload
        architecture_path = str(arch_payload["artifacts"]["ARCHITECTURE_PLAN.json"])
    if case.mode == "greenfield":
        init_args = ["init", "--project-root", str(project_root), "--architecture-plan", architecture_path]
        init_rc, init_payload, _ = run_cli(*init_args, env=env)
        assert init_rc == 0, init_payload
        if not (project_root / ".git").exists():
            ensure_git_repo(project_root)
        commit_all(project_root, message="scaffold baseline")
    graph_args = [
        "task-plan",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--intake",
        intake_payload["artifacts"]["PRD_INTAKE.json"],
        "--mode",
        case.mode,
        "--archetype",
        case.archetype,
    ]
    if architecture_path:
        graph_args.extend(["--architecture-plan", architecture_path])
    for capability in case.capabilities:
        graph_args.extend(["--capability", capability])
    graph_rc, graph_payload, _ = run_cli(*graph_args, env=env)
    assert graph_rc == 0, graph_payload
    graph_path = Path(graph_payload["artifacts"]["TASK_GRAPH.json"])
    task_id = _preferred_task_id(graph_path, preferred_surface=case.preferred_surface)
    card_rc, card_payload, _ = run_cli(
        "task-prepare",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--graph",
        str(graph_path),
        "--task",
        task_id,
        env=env,
    )
    assert card_rc == 0, card_payload
    verify_cmd = _write_task_run_verify(project_root)
    task_run_rc, task_run_payload, _ = run_cli(
        "task-run",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--card",
        card_payload["artifacts"]["TASK_CARD.json"],
        "--strict-scope",
        "--contract-mode",
        "strict",
        "--executor-backend",
        "codex_cli",
        "--self-review-backend",
        "noop_test_only",
        "--verify-cmd",
        verify_cmd,
        env=env,
    )
    assert task_run_rc == 0, task_run_payload
    review_rc, review_payload, _ = run_cli(
        "review",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--fail-on-block",
        env=env,
    )
    assert review_rc == 0, review_payload
    verify_rc, verify_payload, _ = run_cli(
        "verify",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--fail-on-block",
        env=env,
    )
    assert verify_rc == 0, verify_payload
    qa_rc, qa_payload, _ = run_cli(
        "qa",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--fail-on-block",
        env=env,
    )
    assert qa_rc == 0, qa_payload
    _write_text(project_root / "AUTOMATION_EVAL_REPORT.json", json.dumps({"status": "PASS"}))
    ship_rc, ship_payload, _ = run_cli(
        "ship-readiness",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--fail-on-block",
        env=env,
    )
    assert ship_rc == 0, ship_payload
    status_rc, status_payload, _ = run_cli(
        "status",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        env=env,
    )
    assert status_rc == 0, status_payload
    return {
        "planning_dir": Path(task_run_payload["planning_dir"]),
        "task_run": task_run_payload,
        "review": review_payload,
        "verify": verify_payload,
        "qa": qa_payload,
        "ship": ship_payload,
        "status": status_payload,
    }


def assert_happy_path_truth(
    result: dict[str, Any],
    *,
    case: CanaryCase,
    expected_review_mode: str,
) -> None:
    planning_dir = Path(result["planning_dir"])
    for artifact in (
        ".execution_result.json",
        ".review_evidence.json",
        ".verify_report.json",
        ".qa_report.json",
        ".ship_readiness.json",
    ):
        assert (planning_dir / artifact).exists(), artifact
    execution_payload = json.loads((planning_dir / ".execution_result.json").read_text(encoding="utf-8"))
    verify_payload = json.loads((planning_dir / ".verify_report.json").read_text(encoding="utf-8"))
    review_payload = json.loads((planning_dir / ".review_result.json").read_text(encoding="utf-8"))
    status_payload = dict(result["status"])
    assert execution_payload["backend"] == "codex_cli"
    assert review_payload["review_mode"] == expected_review_mode
    assert status_payload["execution_backend"] == "codex_cli"
    assert status_payload["review_mode"] == expected_review_mode
    assert status_payload["verify_scope_mode"] == verify_payload["verify_scope_mode"]
    assert tuple(status_payload["verify_surfaces"]) == case.expected_verify_surfaces
    assert status_payload["execution_truth_source"] == ".execution_result.json"
    assert status_payload["review_truth_source"] == ".review_evidence.json"
    assert status_payload["verify_truth_source"] == ".verify_report.json"


def _preferred_task_id(graph_path: Path, *, preferred_surface: str) -> str:
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    for item in list(payload.get("tasks") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("surface") or "").strip() == preferred_surface:
            return str(item.get("task_id") or "T1")
    return str(dict((payload.get("tasks") or [{}])[0]).get("task_id") or "T1")


def _write_task_run_verify(project_root: Path) -> str:
    verify_script = project_root / "scripts" / "task_run_verify.py"
    _write_text(
        verify_script,
        "print('task-run verify ok')\n",
    )
    return f'"{sys.executable}" "{verify_script}"'


def _prepare_fastapi_repo(project_root: Path) -> None:
    _write_text(project_root / "app" / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write_text(project_root / "app" / "schemas.py", "class HydrationSchema:\n    name = 'hydration'\n")
    _write_text(project_root / "app" / "repository.py", "def load_snapshot() -> dict[str, str]:\n    return {'status': 'ok'}\n")
    _write_text(project_root / "app" / "service.py", "def summarize() -> str:\n    return 'ok'\n")
    _write_text(project_root / "app" / "services.py", "def summarize_service() -> str:\n    return 'ok'\n")
    _write_text(project_root / "tests" / "test_api.py", "def test_api() -> None:\n    assert True\n")
    _write_text(project_root / "requirements.txt", "pytest>=8\n")


def _prepare_flask_repo(project_root: Path) -> None:
    _write_text(project_root / "app" / "main.py", "from flask import Flask\napp = Flask(__name__)\n")
    _write_text(project_root / "app" / "repository.py", "def load_snapshot() -> dict[str, str]:\n    return {'status': 'ok'}\n")
    _write_text(project_root / "app" / "service.py", "def summarize() -> str:\n    return 'ok'\n")
    _write_text(project_root / "app" / "routes.py", "def healthcheck() -> dict[str, str]:\n    return {'status': 'ok'}\n")
    _write_text(project_root / "tests" / "test_api.py", "def test_api() -> None:\n    assert True\n")
    _write_text(project_root / "requirements.txt", "pytest>=8\n")


def _prepare_django_repo(project_root: Path) -> None:
    _write_text(project_root / "manage.py", "def main() -> int:\n    return 0\n")
    _write_text(project_root / "app" / "models.py", "class HydrationModel:\n    status = 'ok'\n")
    _write_text(project_root / "app" / "repository.py", "def load_snapshot() -> dict[str, str]:\n    return {'status': 'ok'}\n")
    _write_text(project_root / "app" / "services.py", "def summarize_service() -> str:\n    return 'ok'\n")
    _write_text(project_root / "app" / "views.py", "def health_view() -> dict[str, str]:\n    return {'status': 'ok'}\n")
    _write_text(project_root / "app" / "urls.py", "urlpatterns = ['health_view']\n")
    _write_text(project_root / "tests" / "test_models.py", "def test_models() -> None:\n    assert True\n")
    _write_text(project_root / "tests" / "test_views.py", "def test_views() -> None:\n    assert True\n")
    _write_text(project_root / "requirements.txt", "pytest>=8\n")


def _prepare_node_repo(project_root: Path) -> None:
    _write_text(project_root / "src" / "schema.ts", "export const hydrationSchema = { name: 'hydration' };\n")
    _write_text(project_root / "src" / "repository.ts", "export const loadSnapshot = () => ({ status: 'ok' });\n")
    _write_text(project_root / "src" / "services.ts", "export const summarizeService = () => 'ok';\n")
    _write_text(project_root / "src" / "server.js", "module.exports = { healthcheck: () => ({ status: 'ok' }) };\n")
    _write_text(project_root / "tests" / "api.test.js", "console.log('node api ok');\n")
    _write_text(
        project_root / "package.json",
        json.dumps({"name": "canary-node", "private": True, "scripts": {"test": "node tests/api.test.js"}}, indent=2) + "\n",
    )


def _prepare_monorepo_repo(project_root: Path) -> None:
    _prepare_fastapi_repo(project_root)
    _prepare_fastapi_repo(project_root / "packages" / "api")
    _write_text(project_root / "pnpm-workspace.yaml", "packages:\n  - packages/*\n")
    _write_text(project_root / "packages" / "README.md", "# Workspace\n")
    _write_text(
        project_root / "scripts" / "verify_workspace.py",
        "from pathlib import Path\nassert Path('packages/README.md').exists()\nprint('workspace verify ok')\n",
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(project_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(project_root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _shim_wrapper(script_name: str) -> str:
    python_path = str(Path(sys.executable).resolve())
    return f'@echo off\r\n"{python_path}" "%~dp0{script_name}" %*\r\n'


def _fake_codex_script() -> str:
    return textwrap.dedent(
        """
        import json
        import os
        import re
        import sys
        from pathlib import Path

        def _request_path(prompt: str) -> Path:
            match = re.search(r"^Request path: (.+)$", prompt, re.MULTILINE)
            if not match:
                raise SystemExit("missing request path in codex prompt")
            return Path(match.group(1).strip())

        def _touch(path: Path) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            suffix = "\\n# codex touched\\n" if path.suffix == ".py" else "\\ncodex touched\\n"
            path.write_text(existing + suffix, encoding="utf-8")

        prompt = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        if not prompt:
            prompt = sys.argv[-1] if len(sys.argv) > 1 else ""
        mode = os.environ.get("WORKFLOW_FAKE_CODEX_MODE", "pass")
        request_path = _request_path(prompt)
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        project_root = Path(request_payload["project_root"]).resolve()
        allowed = [Path(project_root / item).resolve() for item in request_payload.get("files_to_change", [])]
        log_path = os.environ.get("WORKFLOW_FAKE_CODEX_LOG", "")
        if log_path:
            Path(log_path).write_text(json.dumps({"prompt": prompt, "request": request_payload}, ensure_ascii=False), encoding="utf-8")
        if mode == "fail":
            print("fake codex failed", file=sys.stderr)
            raise SystemExit(7)
        if mode == "nochange":
            print("fake codex no-op")
            raise SystemExit(0)
        for path in allowed:
            _touch(path)
        if mode == "out_of_scope":
            _touch(project_root / "rogue.py")
        print("fake codex ok")
        raise SystemExit(0)
        """
    ).strip() + "\n"


def _fake_npm_script() -> str:
    return textwrap.dedent(
        """
        import json
        import os
        import sys
        from pathlib import Path

        log_path = os.environ.get("WORKFLOW_FAKE_NPM_LOG", "")
        if log_path:
            Path(log_path).write_text(json.dumps({"argv": sys.argv[1:]}, ensure_ascii=False), encoding="utf-8")
        if os.environ.get("WORKFLOW_FAKE_NPM_MODE", "pass") == "fail":
            print("fake npm failed", file=sys.stderr)
            raise SystemExit(9)
        print("fake npm ok")
        raise SystemExit(0)
        """
    ).strip() + "\n"

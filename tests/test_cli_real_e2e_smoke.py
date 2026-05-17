import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"


def _workflow_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = str(SRC_ROOT) if not existing else f"{SRC_ROOT}{os.pathsep}{existing}"
    env["WORKFLOW_REVIEW_ENABLED"] = "0"
    env["WORKFLOW_REVIEW_REQUIRED"] = "0"
    env.pop("PYTEST_CURRENT_TEST", None)
    env.pop("WORKFLOW_SDK_TEST_MODE", None)
    return env


def _run_cli(*args: str, env: dict[str, str] | None = None) -> tuple[int, dict[str, Any], subprocess.CompletedProcess[str]]:
    run = subprocess.run(
        [sys.executable, "-m", "kodawari.cli.main", *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env or _workflow_env(),
    )
    payload = json.loads(run.stdout) if str(run.stdout or "").strip() else {}
    return run.returncode, payload, run


def _prepare_project(tmp_path: Path, *, feature: str) -> tuple[Path, Path]:
    project_root = tmp_path / "sample-app"
    (project_root / "app").mkdir(parents=True, exist_ok=True)
    (project_root / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n",
        encoding="utf-8",
    )
    (project_root / "app" / "schemas.py").write_text("class Payload: ...\n", encoding="utf-8")
    (project_root / "tests").mkdir(parents=True, exist_ok=True)
    (project_root / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
    prd_path = project_root / "PRD.md"
    prd_path.write_text(
        "\n".join(
            [
                "1. business outcome",
                "- Return hydration goal details in one API response.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "- db.goal_change_events",
                "",
                "3. flow type",
                "- This is a read path for the current snapshot.",
                "",
                "4. layer ownership",
                "- route",
                "- service",
                "- repository",
                "",
                "7. non-goals",
                "- Do not change reminder generation",
            ]
        ),
        encoding="utf-8",
    )
    return project_root, prd_path


def _prepare_executor_script(tmp_path: Path) -> Path:
    script_path = tmp_path / "test_executor.py"
    script_path.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "from pathlib import Path",
                "",
                "stage = os.environ.get('WORKFLOW_AUTOMATION_STAGE', '')",
                "if stage == 'self_review':",
                "    print(json.dumps({",
                "        'status': 'PASS',",
                "        'approved': True,",
                "        'summary': 'external self review approved',",
                "        'reviewer': 'codex',",
                "        'source': 'test.external_self_review'",
                "    }))",
                "    raise SystemExit(0)",
                "",
                "request_path = Path(os.environ['WORKFLOW_EXECUTION_REQUEST_PATH'])",
                "result_path = Path(os.environ['WORKFLOW_EXECUTION_RESULT_PATH'])",
                "request_payload = json.loads(request_path.read_text(encoding='utf-8'))",
                "project_root = Path(request_payload['project_root'])",
                "changed_files = list(request_payload.get('files_to_change') or [])",
                "for rel in changed_files:",
                "    path = project_root / rel",
                "    path.parent.mkdir(parents=True, exist_ok=True)",
                "    text = path.read_text(encoding='utf-8') if path.exists() else ''",
                "    if '# executor touched' not in text:",
                "        suffix = '\\n# executor touched\\n' if path.suffix == '.py' else '\\nexecutor touched\\n'",
                "        path.write_text(text + suffix, encoding='utf-8')",
                "result_payload = {",
                "    'schema_version': 'execution.result.v1',",
                "    'feature': request_payload.get('feature', ''),",
                "    'task': request_payload.get('task', ''),",
                "    'backend': 'external_cli',",
                "    'status': 'PASS',",
                "    'changed_files': changed_files,",
                "    'stdout_excerpt': 'executor ok',",
                "    'stderr_excerpt': '',",
                "    'returncode': 0,",
                "    'artifacts': changed_files,",
                "    'error_code': '',",
                "    'blocking_reason': '',",
                "    'summary': 'external executor completed'",
                "}",
                "result_path.write_text(json.dumps(result_payload), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    return script_path


def test_real_cli_contract_first_chain_with_external_executor(tmp_path: Path) -> None:
    feature = "real-e2e"
    project_root, prd_path = _prepare_project(tmp_path, feature=feature)
    executor_script = _prepare_executor_script(tmp_path)
    executor_command = f'"{sys.executable}" "{executor_script}"'
    verify_command = f'"{sys.executable}" -c "print(\'verify ok\')"'

    intake_rc, intake_payload, _ = _run_cli(
        "prd-intake",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--prd",
        str(prd_path),
    )
    assert intake_rc == 0

    graph_rc, graph_payload, _ = _run_cli(
        "task-plan",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--intake",
        str(Path(intake_payload["artifacts"]["PRD_INTAKE.json"])),
        "--project-profile",
        "fastapi",
    )
    assert graph_rc == 0

    card_rc, card_payload, _ = _run_cli(
        "task-prepare",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--graph",
        str(Path(graph_payload["artifacts"]["TASK_GRAPH.json"])),
        "--task",
        "T1",
    )
    assert card_rc == 0

    task_run_rc, task_run_payload, _ = _run_cli(
        "task-run",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--card",
        str(Path(card_payload["artifacts"]["TASK_CARD.json"])),
        "--strict-scope",
        "--contract-mode",
        "strict",
        "--executor-backend",
        "external_cli",
        "--executor-command",
        executor_command,
        "--self-review-backend",
        "external_cli",
        "--self-review-command",
        executor_command,
        "--verify-cmd",
        verify_command,
    )
    assert task_run_rc == 0
    assert task_run_payload["status"] == "PASS"
    planning_dir = Path(task_run_payload["planning_dir"])
    assert (planning_dir / ".execution_request.json").exists()
    assert (planning_dir / ".execution_result.json").exists()
    assert (planning_dir / ".review_evidence.json").exists()
    assert (planning_dir / ".verify_report.json").exists()

    review_rc, review_payload, _ = _run_cli(
        "review",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--fail-on-block",
    )
    assert review_rc == 0
    assert review_payload["status"] == "PASS"
    assert review_payload["execution_source"] == ".execution_result.json"
    assert review_payload["review_evidence_status"] == "PASS"

    verify_rc, verify_payload, _ = _run_cli(
        "verify",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--fail-on-block",
    )
    assert verify_rc == 0
    assert verify_payload["status"] == "PASS"

    qa_rc, qa_payload, _ = _run_cli(
        "qa",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--fail-on-block",
    )
    assert qa_rc == 0
    assert qa_payload["status"] == "PASS"
    assert qa_payload["execution_source"] == ".execution_result.json"

    (project_root / "AUTOMATION_EVAL_REPORT.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    ship_rc, ship_payload, _ = _run_cli(
        "ship-readiness",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--fail-on-block",
    )
    assert ship_rc == 0
    assert ship_payload["status"] == "PASS"
    assert ship_payload["execution_source"] == ".execution_result.json"


def test_real_cli_task_run_blocks_without_executor_backend(tmp_path: Path) -> None:
    feature = "real-e2e-no-executor"
    project_root, prd_path = _prepare_project(tmp_path, feature=feature)

    intake_rc, intake_payload, _ = _run_cli(
        "prd-intake",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--prd",
        str(prd_path),
    )
    assert intake_rc == 0

    graph_rc, graph_payload, _ = _run_cli(
        "task-plan",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--intake",
        str(Path(intake_payload["artifacts"]["PRD_INTAKE.json"])),
        "--project-profile",
        "fastapi",
    )
    assert graph_rc == 0

    card_rc, card_payload, _ = _run_cli(
        "task-prepare",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--graph",
        str(Path(graph_payload["artifacts"]["TASK_GRAPH.json"])),
        "--task",
        "T1",
    )
    assert card_rc == 0

    verify_command = f'"{sys.executable}" -c "print(\'verify ok\')"'
    task_run_rc, task_run_payload, _ = _run_cli(
        "task-run",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--card",
        str(Path(card_payload["artifacts"]["TASK_CARD.json"])),
        "--verify-cmd",
        verify_command,
    )

    assert task_run_rc == 2
    assert task_run_payload["status"] == "FAIL"
    assert task_run_payload["reason"] in {"EXECUTION_BACKEND_BLOCKED", "EXECUTOR_BACKEND_MISSING"}
    planning_dir = Path(task_run_payload["planning_dir"])
    assert (planning_dir / ".execution_request.json").exists()

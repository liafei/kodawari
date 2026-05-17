from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.generic_canary_support import create_test_shims, run_cli, workflow_env
from kodawari.cli.autopilot_decision_runtime import (
    build_decision_response,
    load_decision_request,
    write_decision_response,
)
from kodawari.cli.review_evidence_artifact import (
    build_review_evidence_artifact,
    write_review_evidence_artifact,
)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_newsapp_benchmark_fixture(project_root: Path) -> None:
    _write_text(project_root / "backend" / "app" / "main.py", "from fastapi import FastAPI\napp = FastAPI()\n")
    _write_text(project_root / "backend" / "tests" / "test_api.py", "def test_api() -> None:\n    assert True\n")
    _write_text(
        project_root / "web" / "package.json",
        json.dumps(
            {
                "name": "newsapp-benchmark-web",
                "private": True,
                "dependencies": {"react": "^18.0.0"},
                "scripts": {"test": "node src/App.test.js"},
            },
            indent=2,
        )
        + "\n",
    )
    _write_text(project_root / "web" / "src" / "App.js", "module.exports = { renderApp: () => 'newsapp' };\n")
    _write_text(project_root / "web" / "src" / "App.test.js", "console.log('newsapp web ok');\n")
    _write_text(project_root / "docs" / "RUNBOOK.md", "# Runbook\n\n- benchmark fixture\n")
    _write_text(project_root / "mobile" / "README.md", "# Mobile Wrapper\n")
    _write_text(project_root / "pnpm-workspace.yaml", "packages:\n  - packages/*\n")
    _write_text(project_root / "packages" / "README.md", "# Workspace\n")
    _write_text(project_root / "Dockerfile.backend", "FROM python:3.11-slim\n")


def _write_prd(prd_path: Path) -> None:
    prd_path.write_text(
        "\n".join(
            [
                "1. business outcome",
                "- ship a newsapp-style ranking surface across backend, web, mobile wrapper, and ops rails.",
                "",
                "2. source of truth",
                "- db.rankings",
                "- api.feed",
                "",
                "3. flow type",
                "- read",
                "",
                "4. layer ownership",
                "- route",
                "- service",
                "- frontend",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _write_verify_script(project_root: Path) -> str:
    verify_script = project_root / "scripts" / "newsapp_verify.py"
    _write_text(verify_script, "print('newsapp verify ok')\n")
    return f'"{Path(sys.executable).resolve()}" "{verify_script}"'


def _write_self_review_script(project_root: Path) -> str:
    review_script = project_root / "scripts" / "self_review.py"
    _write_text(
        review_script,
        "\n".join(
            [
                "import json",
                "print(json.dumps({",
                "  'status': 'PASS',",
                "  'approved': True,",
                "  'summary': 'external self review approved',",
                "  'reviewer': 'codex',",
                "  'source': 'test.newsapp_benchmark.self_review'",
                "}))",
            ]
        )
        + "\n",
    )
    return f'"{Path(sys.executable).resolve()}" "{review_script}"'


def _run_autopilot(
    project_root: Path,
    *,
    feature: str,
    prd_path: Path,
    env: dict[str, str],
    verify_cmd: str,
    self_review_cmd: str,
) -> tuple[int, dict[str, object]]:
    rc, payload, _ = run_cli(
        "autopilot",
        "--project-root",
        str(project_root),
        "--feature",
        feature,
        "--tier",
        "heavy",
        "--prd",
        str(prd_path),
        "--executor-backend",
        "codex_cli",
        "--self-review-backend",
        "external_cli",
        "--self-review-command",
        self_review_cmd,
        "--verify-cmd",
        verify_cmd,
        env=env,
    )
    return rc, payload


def _respond_decision(planning_dir: Path, *, selected_option: str, rationale: str) -> None:
    request_payload = load_decision_request(planning_dir) or {}
    decision_id = str(request_payload.get("decision_id") or "")
    assert decision_id
    write_decision_response(
        planning_dir,
        build_decision_response(
            decision_id=decision_id,
            selected_option=selected_option,
            rationale=rationale,
        ),
    )


def _reject_option(decision_kind: str) -> str:
    if decision_kind == "release_approval":
        return "hold"
    return "revise"


def _write_passing_review_evidence(*, planning_dir: Path, feature: str) -> None:
    payload = build_review_evidence_artifact(
        feature=feature,
        planning_dir=planning_dir,
        entrypoint="kodawari autopilot",
        review_evidence={
            "status": "PASS",
            "blocking_reason": "",
            "checks": {
                "self_review_count": 1,
                "peer_review_count": 1,
                "must_fix_remaining": 0,
            },
            "issues": [],
            "evidence": [
                {
                    "file": "backend/tests/test_api.py",
                    "rule": "review_evidence.synthetic",
                    "hit": "synthetic self-review evidence for benchmark proof",
                    "confidence": 1.0,
                }
            ],
        },
    )
    write_review_evidence_artifact(planning_dir / ".review_evidence.json", payload)


def test_newsapp_benchmark_autopilot_happy_path_with_release_approval(tmp_path: Path) -> None:
    feature = "newsapp-benchmark-happy"
    _write_newsapp_benchmark_fixture(tmp_path)
    prd_path = tmp_path / "PRD.md"
    _write_prd(prd_path)
    env = workflow_env(shim_dir=create_test_shims(tmp_path))
    verify_cmd = _write_verify_script(tmp_path)
    self_review_cmd = _write_self_review_script(tmp_path)

    run_rc, run_payload = _run_autopilot(
        tmp_path,
        feature=feature,
        prd_path=prd_path,
        env=env,
        verify_cmd=verify_cmd,
        self_review_cmd=self_review_cmd,
    )
    assert run_rc == 0
    assert run_payload["status"] == "awaiting_decision"
    planning_dir = Path(str(run_payload["planning_dir"]))
    for _ in range(3):
        decision_kind = str(run_payload.get("decision_kind") or "")
        if decision_kind == "release_approval":
            break
        _respond_decision(planning_dir, selected_option="approve", rationale="继续执行")
        run_rc, run_payload = _run_autopilot(
            tmp_path,
            feature=feature,
            prd_path=prd_path,
            env=env,
            verify_cmd=verify_cmd,
            self_review_cmd=self_review_cmd,
        )
        assert run_rc == 0
        assert run_payload["status"] == "awaiting_decision"
    assert run_payload["decision_kind"] == "release_approval"

    _write_text(tmp_path / "AUTOMATION_EVAL_REPORT.json", json.dumps({"status": "PASS"}))
    _write_passing_review_evidence(planning_dir=planning_dir, feature=feature)
    _respond_decision(planning_dir, selected_option="ship", rationale="批准发布")
    final_rc, final_payload = _run_autopilot(
        tmp_path,
        feature=feature,
        prd_path=prd_path,
        env=env,
        verify_cmd=verify_cmd,
        self_review_cmd=self_review_cmd,
    )
    assert final_rc == 0
    assert final_payload["status"] == "ok"
    assert dict(final_payload.get("release_tail") or {}).get("status") == "PASS"
    assert final_payload["interaction_state"] == "PASS"
    assert final_payload["next_action_type"] == "completed"

    for artifact in (
        ".execution_result.json",
        ".review_result.json",
        ".verify_report.json",
        ".qa_report.json",
        ".ship_readiness.json",
    ):
        assert (planning_dir / artifact).exists(), artifact

    status_rc, status_payload, _ = run_cli(
        "status",
        "--project-root",
        str(tmp_path),
        "--feature",
        feature,
        env=env,
    )
    assert status_rc == 0
    assert status_payload["release_complete"] is True
    assert status_payload["interaction_state"] in {"PASS", "RUNNING"}


def test_newsapp_benchmark_blocks_when_release_decision_is_rejected(tmp_path: Path) -> None:
    feature = "newsapp-benchmark-blocked"
    _write_newsapp_benchmark_fixture(tmp_path)
    prd_path = tmp_path / "PRD.md"
    _write_prd(prd_path)
    env = workflow_env(shim_dir=create_test_shims(tmp_path))
    verify_cmd = _write_verify_script(tmp_path)
    self_review_cmd = _write_self_review_script(tmp_path)

    first_rc, first_payload = _run_autopilot(
        tmp_path,
        feature=feature,
        prd_path=prd_path,
        env=env,
        verify_cmd=verify_cmd,
        self_review_cmd=self_review_cmd,
    )
    assert first_rc == 0
    assert first_payload["status"] == "awaiting_decision"

    planning_dir = Path(str(first_payload["planning_dir"]))
    decision_kind = str(first_payload.get("decision_kind") or "")
    _respond_decision(
        planning_dir,
        selected_option=_reject_option(decision_kind),
        rationale="拒绝继续",
    )

    second_rc, second_payload = _run_autopilot(
        tmp_path,
        feature=feature,
        prd_path=prd_path,
        env=env,
        verify_cmd=verify_cmd,
        self_review_cmd=self_review_cmd,
    )
    assert second_rc == 1
    assert second_payload["status"] == "blocked"
    assert second_payload["interaction_state"] == "BLOCKED"
    assert second_payload["decision_kind"] == decision_kind
    assert "human decision did not approve continuation" in str(second_payload.get("blocking_reason") or "")

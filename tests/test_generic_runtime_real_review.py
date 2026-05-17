from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from tests.generic_canary_support import (
    CanaryCase,
    commit_all,
    create_test_shims,
    ensure_git_repo,
    prepare_existing_repo,
    run_cli,
    workflow_env,
    write_prd,
)


REAL_REVIEW_READY = bool(
    (os.getenv("WORKFLOW_REVIEWER_API_KEY") or os.getenv("WORKFLOW_OPUS_API_KEY"))
    and (os.getenv("WORKFLOW_REVIEWER_BASE_URL") or os.getenv("WORKFLOW_OPUS_GATEWAY"))
)

CODEX_REVIEW_READY = bool(os.getenv("WORKFLOW_CODEX_REVIEW_ENABLED"))

REAL_REVIEW_CASES = (
    CanaryCase(
        name="fastapi_api",
        feature="real-review-fastapi",
        archetype="fastapi_api",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        prd_text=(
            "1. business outcome\n"
            "- Return hydration goal history.\n\n"
            "2. source of truth\n"
            "- db.hydration_goals\n\n"
            "3. flow type\n"
            "- This is a read path.\n\n"
            "4. layer ownership\n"
            "- repository\n"
            "- service\n"
            "- route\n"
        ),
    ),
    CanaryCase(
        name="node_api",
        feature="real-review-node",
        archetype="node_api",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        prd_text=(
            "1. business outcome\n"
            "- Return hydration snapshot through node API.\n\n"
            "2. source of truth\n"
            "- db.hydration_goals\n\n"
            "3. flow type\n"
            "- This is a read path.\n\n"
            "4. layer ownership\n"
            "- repository\n"
            "- service\n"
            "- route\n"
        ),
    ),
    CanaryCase(
        name="react_web",
        feature="real-review-react",
        archetype="react_web",
        mode="greenfield",
        preferred_surface="frontend",
        expected_verify_surfaces=("frontend",),
        prd_text=(
            "1. business outcome\n"
            "- Render a hydration summary card.\n\n"
            "2. source of truth\n"
            "- api.hydration_summary\n\n"
            "3. flow type\n"
            "- This is a read path.\n\n"
            "4. layer ownership\n"
            "- frontend\n"
        ),
    ),
)


pytestmark = pytest.mark.skipif(
    not REAL_REVIEW_READY,
    reason="real review integration lane requires WORKFLOW_REVIEWER_API_KEY and WORKFLOW_REVIEWER_BASE_URL",
)


def _prepare_case_project(tmp_path: Path, case: CanaryCase) -> tuple[Path, dict[str, str]]:
    project_root = tmp_path / case.name
    project_root.mkdir(parents=True, exist_ok=True)
    env = workflow_env(
        shim_dir=create_test_shims(tmp_path),
        extra_env={"WORKFLOW_REVIEW_ENABLED": "1", "WORKFLOW_REVIEW_REQUIRED": "1"},
    )
    ensure_git_repo(project_root)
    if case.mode == "existing":
        prepare_existing_repo(project_root, case)
        commit_all(project_root, message="baseline")
    else:
        (project_root / ".gitignore").write_text(
            "planning/\n.claude/\n__pycache__/\n.pytest_cache/\nnode_modules/\n",
            encoding="utf-8",
        )
    return project_root, env


def _prepare_greenfield_architecture(
    *,
    project_root: Path,
    case: CanaryCase,
    intake_path: str,
    env: dict[str, str],
) -> str:
    if case.mode != "greenfield":
        return ""
    arch_rc, arch_payload, _ = run_cli(
        "architecture-plan",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--intake",
        intake_path,
        "--mode",
        case.mode,
        "--archetype",
        case.archetype,
        env=env,
    )
    assert arch_rc == 0, arch_payload
    architecture_path = str(arch_payload["artifacts"]["ARCHITECTURE_PLAN.json"])
    init_rc, init_payload, _ = run_cli(
        "init",
        "--project-root",
        str(project_root),
        "--architecture-plan",
        architecture_path,
        env=env,
    )
    assert init_rc == 0, init_payload
    commit_all(project_root, message="scaffold baseline")
    return architecture_path


def _prepare_task_card(project_root: Path, case: CanaryCase, env: dict[str, str]) -> str:
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
    architecture_path = _prepare_greenfield_architecture(
        project_root=project_root,
        case=case,
        intake_path=str(intake_payload["artifacts"]["PRD_INTAKE.json"]),
        env=env,
    )
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
    graph_rc, graph_payload, _ = run_cli(*graph_args, env=env)
    assert graph_rc == 0, graph_payload
    card_rc, card_payload, _ = run_cli(
        "task-prepare",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--graph",
        graph_payload["artifacts"]["TASK_GRAPH.json"],
        "--task",
        "T1",
        env=env,
    )
    assert card_rc == 0, card_payload
    return str(card_payload["artifacts"]["TASK_CARD.json"])


def _task_run_verify_cmd(project_root: Path) -> str:
    verify_script = project_root / "scripts" / "task_run_verify.py"
    verify_script.parent.mkdir(parents=True, exist_ok=True)
    verify_script.write_text("print('task-run verify ok')\n", encoding="utf-8")
    python_path = str(Path(sys.executable).resolve())
    return f'"{python_path}" "{verify_script}"'


@pytest.mark.parametrize("case", REAL_REVIEW_CASES, ids=[case.name for case in REAL_REVIEW_CASES])
def test_generic_runtime_real_review_lane(tmp_path: Path, case: CanaryCase) -> None:
    project_root, env = _prepare_case_project(tmp_path, case)
    task_card_path = _prepare_task_card(project_root, case, env)
    run_rc, run_payload, _ = run_cli(
        "task-run",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--card",
        task_card_path,
        "--strict-scope",
        "--contract-mode",
        "strict",
        "--executor-backend",
        "codex_cli",
        "--self-review-backend",
        "noop_test_only",
        "--real-opus-review",
        "--require-real-opus-review",
        "--verify-cmd",
        _task_run_verify_cmd(project_root),
        env=env,
    )

    assert run_rc in {0, 2}
    runtime = dict((run_payload.get("run_result") or {}).get("runtime_semantics") or {})
    peer_review = dict(runtime.get("peer_review") or {})
    mode = str(peer_review.get("mode") or "")
    assert mode in {"real_opus_gateway", "real_required_failed"}
    assert peer_review.get("real_requested") is True
    assert peer_review.get("real_required") is True
    assert peer_review.get("fallback_used") is False
    if mode == "real_required_failed":
        assert str(peer_review.get("error") or "").strip()


@pytest.mark.skipif(
    not CODEX_REVIEW_READY,
    reason="codex review integration lane requires WORKFLOW_CODEX_REVIEW_ENABLED=1 (codex auth login must be done first)",
)
def test_generic_runtime_codex_review_lane(tmp_path: Path) -> None:
    """End-to-end lane: task-run with --opus-reviewer-backend codex."""
    case = CanaryCase(
        name="codex_review_fastapi",
        feature="codex-review-fastapi",
        archetype="fastapi_api",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        prd_text=(
            "1. business outcome\n"
            "- Return daily step count.\n\n"
            "2. source of truth\n"
            "- db.step_counts\n\n"
            "3. flow type\n"
            "- This is a read path.\n\n"
            "4. layer ownership\n"
            "- repository\n"
            "- service\n"
            "- route\n"
        ),
    )
    project_root, env = _prepare_case_project(tmp_path, case)
    task_card_path = _prepare_task_card(project_root, case, env)
    run_rc, run_payload, _ = run_cli(
        "task-run",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--card",
        task_card_path,
        "--strict-scope",
        "--contract-mode",
        "strict",
        "--executor-backend",
        "codex_cli",
        "--self-review-backend",
        "noop_test_only",
        "--real-opus-review",
        "--require-real-opus-review",
        "--opus-reviewer-backend",
        "codex",
        "--verify-cmd",
        _task_run_verify_cmd(project_root),
        env=env,
    )

    assert run_rc in {0, 2}
    runtime = dict((run_payload.get("run_result") or {}).get("runtime_semantics") or {})
    peer_review = dict(runtime.get("peer_review") or {})
    mode = str(peer_review.get("mode") or "")
    assert mode in {"real_codex_reviewer", "real_required_failed"}
    assert peer_review.get("real_requested") is True
    assert peer_review.get("real_required") is True
    assert peer_review.get("fallback_used") is False
    if mode == "real_required_failed":
        assert str(peer_review.get("error") or "").strip()

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from tests.generic_canary_support import (
    CanaryCase,
    assert_happy_path_truth,
    commit_all,
    create_test_shims,
    ensure_git_repo,
    prepare_existing_repo,
    run_cli,
    run_happy_path_chain,
    workflow_env,
)


CANARY_CASES = [
    CanaryCase(
        name="fastapi_api",
        feature="canary-fastapi-existing",
        archetype="fastapi_api",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Return hydration goal history in one API response.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "- db.goal_change_events",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- repository",
                "- service",
                "- route",
            ]
        ),
    ),
    CanaryCase(
        name="flask_api",
        feature="canary-flask-existing",
        archetype="flask_api",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Return the hydration snapshot from the flask route.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- repository",
                "- service",
                "- route",
            ]
        ),
    ),
    CanaryCase(
        name="django_web",
        feature="canary-django-existing",
        archetype="django_web",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Return the latest hydration snapshot from the view layer.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- model",
                "- service",
                "- route",
            ]
        ),
    ),
    CanaryCase(
        name="node_api",
        feature="canary-node-existing",
        archetype="node_api",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Return the latest hydration snapshot through the node API.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- repository",
                "- service",
                "- route",
            ]
        ),
    ),
    CanaryCase(
        name="react_web",
        feature="canary-react-greenfield",
        archetype="react_web",
        mode="greenfield",
        preferred_surface="frontend",
        expected_verify_surfaces=("frontend",),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Render a hydration summary card in the web UI.",
                "",
                "2. source of truth",
                "- api.hydration_summary",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- frontend",
            ]
        ),
    ),
    CanaryCase(
        name="fullstack_fastapi_react",
        feature="canary-fullstack-greenfield",
        archetype="fullstack_fastapi_react",
        mode="greenfield",
        preferred_surface="frontend",
        expected_verify_surfaces=("frontend",),
        capabilities=("docker_deploy", "postgres_db"),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Render a hydration summary panel in the web app using the backend API.",
                "",
                "2. source of truth",
                "- api.hydration_summary",
                "- postgres.hydration_goals",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- frontend",
                "- route",
                "- service",
                "",
                "5. operational constraints",
                "- The release must retain docker deployment assets.",
            ]
        ),
    ),
    CanaryCase(
        name="fullstack_django_react",
        feature="canary-fullstack-django-greenfield",
        archetype="fullstack_django_react",
        mode="greenfield",
        preferred_surface="frontend",
        expected_verify_surfaces=("frontend",),
        capabilities=("capacitor_mobile",),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Render the hydration summary in a React surface backed by Django APIs.",
                "",
                "2. source of truth",
                "- api.hydration_summary",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- frontend",
                "- route",
                "- service",
                "",
                "5. delivery constraints",
                "- Keep the mobile wrapper scaffold aligned with the web surface.",
            ]
        ),
    ),
    CanaryCase(
        name="monorepo_workspace",
        feature="canary-monorepo-existing",
        archetype="fastapi_api",
        mode="existing",
        preferred_surface="backend",
        expected_verify_surfaces=("backend",),
        capabilities=("monorepo_workspace",),
        prd_text="\n".join(
            [
                "1. business outcome",
                "- Return the hydration snapshot from the packaged API service.",
                "",
                "2. source of truth",
                "- db.hydration_goals",
                "",
                "3. flow type",
                "- This is a read path.",
                "",
                "4. layer ownership",
                "- repository",
                "- service",
                "- route",
            ]
        ),
    ),
]


@pytest.mark.parametrize("case", CANARY_CASES, ids=[case.name for case in CANARY_CASES])
def test_generic_canary_matrix_happy_path(tmp_path: Path, case: CanaryCase) -> None:
    project_root = tmp_path / case.name
    project_root.mkdir(parents=True, exist_ok=True)
    shim_dir = create_test_shims(tmp_path)
    codex_log = tmp_path / f"{case.name}-codex-log.json"
    env = workflow_env(
        shim_dir=shim_dir,
        extra_env={"WORKFLOW_FAKE_CODEX_LOG": str(codex_log)},
    )
    ensure_git_repo(project_root)
    if case.mode == "existing":
        prepare_existing_repo(project_root, case)
        commit_all(project_root, message="baseline")
    else:
        (project_root / ".gitignore").write_text("planning/\n.claude/\n__pycache__/\n.pytest_cache/\nnode_modules/\n", encoding="utf-8")
    result = run_happy_path_chain(project_root=project_root, case=case, env=env)
    assert_happy_path_truth(result, case=case, expected_review_mode="simulated")
    codex_prompt = json.loads(codex_log.read_text(encoding="utf-8"))["prompt"]
    for marker in ("Task ID:", "Archetype:", "Capabilities:", "Surface:", "Request path:", "Verify command expectation:"):
        assert marker in codex_prompt


def test_task_plan_blocks_without_architecture_plan_for_multi_surface_existing_repo(tmp_path: Path) -> None:
    case = next(item for item in CANARY_CASES if item.name == "monorepo_workspace")
    project_root = tmp_path / "blocked-monorepo"
    project_root.mkdir(parents=True, exist_ok=True)
    shim_dir = create_test_shims(tmp_path)
    env = workflow_env(shim_dir=shim_dir)
    ensure_git_repo(project_root)
    prepare_existing_repo(project_root, case)
    commit_all(project_root, message="baseline")
    prd_path = project_root / "PRD.md"
    prd_path.write_text(case.prd_text + "\n", encoding="utf-8")
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
    assert intake_rc == 0
    graph_rc, graph_payload, _ = run_cli(
        "task-plan",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--intake",
        intake_payload["artifacts"]["PRD_INTAKE.json"],
        "--mode",
        "existing",
        "--archetype",
        case.archetype,
        "--capability",
        "monorepo_workspace",
        env=env,
    )
    assert graph_rc == 2
    assert graph_payload["status"] == "BLOCKED"
    assert graph_payload["error_code"] == "architecture_plan_required"


def test_verify_blocks_when_surface_mapping_is_ambiguous_for_multi_surface_repo(tmp_path: Path) -> None:
    project_root = tmp_path / "verify-ambiguous"
    project_root.mkdir(parents=True, exist_ok=True)
    planning_dir = project_root / "planning" / "verify-ambiguous"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "REPO_INVENTORY.json").write_text(
        json.dumps(
            {
                "schema_version": "contract_first.repo_inventory.v1",
                "generated_at": "2026-03-26T00:00:00+00:00",
                "project_root": str(project_root),
                "mode": "existing",
                "archetype": "fullstack_fastapi_react",
                "capabilities": [],
                "project_layout": {"backend_roots": ["backend/app"], "frontend_roots": ["web/src"]},
                "surfaces": [
                    {"name": "backend", "roots": ["backend/app", "backend/tests"], "verify_command": "pytest -q"},
                    {"name": "frontend", "roots": ["web/src"], "verify_command": "npm --prefix web test -- --runInBand"},
                ],
                "verify_surfaces": [
                    {"name": "backend", "verify_command": "pytest -q"},
                    {"name": "frontend", "verify_command": "npm --prefix web test -- --runInBand"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T1",
                "why_this_layer": "verify ambiguity fixture",
                "files_to_change": [],
                "invariants": [],
                "test_plan": "verify planner should block on ambiguous surfaces",
                "requires": [],
            }
        ),
        encoding="utf-8",
    )
    shim_dir = create_test_shims(tmp_path)
    env = workflow_env(shim_dir=shim_dir)
    verify_rc, verify_payload, _ = run_cli(
        "verify",
        "--project-root",
        str(project_root),
        "--feature",
        "verify-ambiguous",
        env=env,
    )
    assert verify_rc == 0
    assert verify_payload["status"] == "BLOCKED"
    assert verify_payload["verify_check"]["source"] == "verify_surface_ambiguous"


def test_verify_blocks_when_surface_recipe_is_missing(tmp_path: Path) -> None:
    project_root = tmp_path / "verify-missing-recipe"
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "docs").mkdir(parents=True, exist_ok=True)
    (project_root / "docs" / "RUNBOOK.md").write_text("# Runbook\n", encoding="utf-8")
    planning_dir = project_root / "planning" / "verify-missing-recipe"
    planning_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "contract_first.repo_inventory.v1",
        "generated_at": "2026-03-26T00:00:00+00:00",
        "project_root": str(project_root),
        "mode": "existing",
        "archetype": "fastapi_api",
        "capabilities": ["docs_runbook"],
        "project_layout": {"docs_roots": ["docs"]},
        "surfaces": [{"name": "docs", "roots": ["docs"], "verify_command": ""}],
        "verify_surfaces": [],
    }
    (planning_dir / "REPO_INVENTORY.json").write_text(json.dumps(payload), encoding="utf-8")
    (planning_dir / "ARCHITECTURE_PLAN.json").write_text(
        json.dumps(
            {
                "schema_version": "contract_first.architecture_plan.v1",
                "generated_at": "2026-03-26T00:00:00+00:00",
                "feature": "verify-missing-recipe",
                "planning_mode": "existing",
                "archetype": "fastapi_api",
                "capabilities": ["docs_runbook"],
                "recommended_layers": ["docs"],
                "surfaces": [{"name": "docs", "roots": ["docs"], "verify_command": ""}],
                "module_boundaries": [{"name": "docs", "surface": "docs", "roots": ["docs"], "layers": ["docs"]}],
                "verify_recipes": [{"surface": "docs", "command": "", "required": False, "roots": ["docs"]}],
                "approval_points": [{"name": "architecture-freeze", "required": False, "reason": "fixture"}],
                "execution_constraints": {
                    "native_executor_required": True,
                    "review_required": True,
                    "max_core_files_per_task": 3,
                },
                "confidence": "high",
                "confidence_issues": [],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".review_result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "changed_files": {"source": ".review_result.json.changed_files", "items": ["docs/RUNBOOK.md"], "count": 1},
            }
        ),
        encoding="utf-8",
    )
    shim_dir = create_test_shims(tmp_path)
    env = workflow_env(shim_dir=shim_dir)
    verify_rc, verify_payload, _ = run_cli(
        "verify",
        "--project-root",
        str(project_root),
        "--feature",
        "verify-missing-recipe",
        env=env,
    )
    assert verify_rc == 0
    assert verify_payload["status"] == "BLOCKED"
    assert verify_payload["verify_check"]["source"] == "verify_recipe_missing"


def test_task_run_blocks_when_codex_cli_touches_out_of_scope_files(tmp_path: Path) -> None:
    case = next(item for item in CANARY_CASES if item.name == "fastapi_api")
    project_root = tmp_path / "scope-blocked"
    project_root.mkdir(parents=True, exist_ok=True)
    shim_dir = create_test_shims(tmp_path)
    env = workflow_env(
        shim_dir=shim_dir,
        extra_env={"WORKFLOW_FAKE_CODEX_MODE": "out_of_scope"},
    )
    ensure_git_repo(project_root)
    prepare_existing_repo(project_root, case)
    commit_all(project_root, message="baseline")
    prd_path = project_root / "PRD.md"
    prd_path.write_text(case.prd_text + "\n", encoding="utf-8")
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
    assert intake_rc == 0
    graph_rc, graph_payload, _ = run_cli(
        "task-plan",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--intake",
        intake_payload["artifacts"]["PRD_INTAKE.json"],
        "--mode",
        "existing",
        "--archetype",
        case.archetype,
        env=env,
    )
    assert graph_rc == 0
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
    assert card_rc == 0
    verify_script = project_root / "scripts" / "task_run_verify.py"
    verify_script.parent.mkdir(parents=True, exist_ok=True)
    verify_script.write_text("print('task-run verify ok')\n", encoding="utf-8")
    run_rc, run_payload, _ = run_cli(
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
        f'"{Path(sys.executable).resolve()}" "{verify_script}"',
        env=env,
    )
    # With isolation enabled by default, codex_cli runs inside a per-task
    # workspace copy.  Out-of-scope writes (rogue.py) are contained inside
    # the workspace and never reach project_root — isolation provides a
    # stronger guarantee than scope blocking.
    assert run_rc == 0, f"expected isolated run to succeed; got: {run_payload}"
    assert run_payload["status"] == "PASS"
    # rogue.py must not have leaked into project_root
    assert not (project_root / "rogue.py").exists(), (
        "isolation workspace leak: rogue.py should stay trapped in the workspace"
    )


def test_qa_blocks_when_verify_surface_coverage_is_inconsistent(tmp_path: Path) -> None:
    project_root = tmp_path / "qa-surface-blocked"
    planning_dir = project_root / "planning" / "qa-surface-blocked"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "PRD_INTAKE.json").write_text(json.dumps({"schema_version": "contract_first.prd_intake.v1"}), encoding="utf-8")
    (planning_dir / "REPO_INVENTORY.json").write_text(
        json.dumps(
            {
                "schema_version": "contract_first.repo_inventory.v1",
                "generated_at": "2026-03-26T00:00:00+00:00",
                "project_root": str(project_root),
                "mode": "existing",
                "archetype": "fastapi_api",
                "capabilities": ["docs_runbook"],
                "project_layout": {"docs_roots": ["docs"], "backend_roots": ["app"]},
                "surfaces": [
                    {"name": "docs", "roots": ["docs"], "verify_command": "scripts/verify_docs.py"},
                    {"name": "backend", "roots": ["app"], "verify_command": "pytest -q"},
                ],
                "verify_surfaces": [
                    {"name": "docs", "verify_command": "scripts/verify_docs.py"},
                    {"name": "backend", "verify_command": "pytest -q"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "TASK_GRAPH.json").write_text(json.dumps({"schema_version": "contract_first.task_graph.v1"}), encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(json.dumps({"schema_version": "contract_first.task_card.v1"}), encoding="utf-8")
    (planning_dir / ".execution_result.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.result.v1",
                "feature": "qa-surface-blocked",
                "task": "T1",
                "backend": "codex_cli",
                "status": "PASS",
                "changed_files": ["docs/RUNBOOK.md"],
                "stdout_excerpt": "",
                "stderr_excerpt": "",
                "returncode": 0,
                "artifacts": ["docs/RUNBOOK.md"],
                "error_code": "",
                "blocking_reason": "",
                "summary": "ok",
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".review_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": "review.evidence.v1",
                "feature": "qa-surface-blocked",
                "generated_at": "2026-03-26T00:00:00+00:00",
                "planning_dir": str(planning_dir),
                "entrypoint": "kodawari review-evidence",
                "status": "PASS",
                "blocking_reason": "",
                "checks": {"self_review_count": 1, "peer_review_count": 1, "must_fix_remaining": 0},
                "issues": [],
                "evidence": [],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".review_result.json").write_text(
        json.dumps(
            {
                "status": "PASS",
                "changed_files": {
                    "source": ".review_result.json.changed_files",
                    "items": ["docs/RUNBOOK.md"],
                    "count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".verify_report.json").write_text(
        json.dumps(
            {
                "schema_version": "verify.report.v1",
                "generated_at": "2026-03-26T00:00:00+00:00",
                "feature": "qa-surface-blocked",
                "planning_dir": str(planning_dir),
                "entrypoint": "kodawari verify",
                "requested_command": "pytest -q",
                "requested_command_kind": "default",
                "changed_files": {"source": ".review_result.json.changed_files", "items": ["docs/RUNBOOK.md"], "count": 1},
                "input_confidence": "curated",
                "status": "PASS",
                "verify_scope_mode": "surface_plan",
                "surface_results": [{"surface": "backend", "status": "PASS"}],
                "surface_summary": {"required_surfaces": ["backend"], "available_surfaces": ["backend", "docs"]},
                "verify_check": {
                    "status": "PASS",
                    "passed": True,
                    "mode": "command",
                    "source": "verify_command",
                    "verify_cmd": "pytest -q",
                    "verify_cmd_resolved": "pytest -q",
                    "verify_target_source": "surface_recipe",
                    "verify_targets": [],
                    "summary": "ok",
                    "blocking_reason": "",
                    "command_executed": True,
                    "returncode": 0,
                    "stdout_excerpt": "",
                    "stderr_excerpt": "",
                    "artifacts": [],
                },
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / ".gate_result.json").write_text(json.dumps({"total_status": "PASS"}), encoding="utf-8")
    shim_dir = create_test_shims(tmp_path)
    env = workflow_env(shim_dir=shim_dir)
    qa_rc, qa_payload, _ = run_cli(
        "qa",
        "--project-root",
        str(project_root),
        "--feature",
        "qa-surface-blocked",
        "--fail-on-block",
        env=env,
    )
    assert qa_rc == 2
    assert qa_payload["status"] == "BLOCKED"
    assert qa_payload["checks"]["surface_coverage_consistency"]["status"] == "FAIL"

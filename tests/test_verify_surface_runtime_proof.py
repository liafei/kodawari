from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import sys

import pytest

from tests.generic_canary_support import create_test_shims, run_cli, workflow_env


@dataclass(frozen=True)
class SurfaceCase:
    name: str
    feature: str
    surface: str
    changed_file: str
    root: str
    verify_script: str


SURFACE_CASES = (
    SurfaceCase("workspace", "surface-workspace", "workspace", "packages/README.md", "packages", "verify_workspace.py"),
    SurfaceCase("scripts_deploy", "surface-scripts", "scripts_deploy", "scripts/deploy_release.py", "scripts", "verify_scripts.py"),
    SurfaceCase("docs", "surface-docs", "docs", "docs/RUNBOOK.md", "docs", "verify_docs.py"),
    SurfaceCase("mobile_wrapper", "surface-mobile", "mobile_wrapper", "mobile_wrapper/app.js", "mobile_wrapper", "verify_mobile.py"),
)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _verify_command(project_root: Path, script_name: str) -> str:
    script_path = project_root / "scripts" / script_name
    _write_text(script_path, "print('surface verify ok')\n")
    return f'"{Path(sys.executable).resolve()}" "{script_path}"'


def _repo_inventory(*, project_root: Path, case: SurfaceCase, verify_command: str) -> dict[str, object]:
    return {
        "schema_version": "contract_first.repo_inventory.v1",
        "generated_at": "2026-03-26T00:00:00+00:00",
        "project_root": str(project_root),
        "mode": "existing",
        "archetype": "fullstack_fastapi_react",
        "capabilities": [case.surface],
        "project_layout": {"code_roots": [case.root], "workspace_roots": ["packages"]},
        "surfaces": [{"name": case.surface, "roots": [case.root], "verify_command": verify_command}],
        "verify_surfaces": [{"name": case.surface, "verify_command": verify_command}],
    }


def _review_result(*, changed_file: str) -> dict[str, object]:
    return {
        "status": "PASS",
        "changed_files": {"source": ".review_result.json.changed_files", "items": [changed_file], "count": 1},
    }


def _prepare_surface_fixture(project_root: Path, case: SurfaceCase) -> None:
    verify_command = _verify_command(project_root, case.verify_script)
    planning_dir = project_root / "planning" / case.feature
    _write_text(project_root / case.changed_file, "surface change\n")
    _write_text(planning_dir / "REPO_INVENTORY.json", json.dumps(_repo_inventory(project_root=project_root, case=case, verify_command=verify_command)))
    _write_text(planning_dir / ".review_result.json", json.dumps(_review_result(changed_file=case.changed_file)))


@pytest.mark.parametrize("case", SURFACE_CASES, ids=[case.name for case in SURFACE_CASES])
def test_verify_surface_runtime_proof(tmp_path: Path, case: SurfaceCase) -> None:
    project_root = tmp_path / case.name
    project_root.mkdir(parents=True, exist_ok=True)
    _prepare_surface_fixture(project_root, case)
    env = workflow_env(shim_dir=create_test_shims(tmp_path))
    verify_rc, verify_payload, _ = run_cli(
        "verify",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        env=env,
    )

    assert verify_rc == 0
    assert verify_payload["status"] == "PASS"
    assert verify_payload["requested_command_kind"] == "default"
    assert verify_payload["verify_scope_mode"] == "surface_plan"
    assert verify_payload["surface_results"][0]["surface"] == case.surface
    assert verify_payload["surface_summary"]["required_surfaces"] == [case.surface]
    assert verify_payload["verify_check"]["covered_surfaces"] == [case.surface]

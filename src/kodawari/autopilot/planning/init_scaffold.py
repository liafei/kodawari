"""Minimal scaffold generator for generic project archetypes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.infra.io_atomic import atomic_write_text
from kodawari.project_model import normalize_archetype, normalize_capabilities


SCAFFOLD_MANIFEST_FILENAME = "SCAFFOLD_MANIFEST.json"
SCAFFOLD_MANIFEST_SCHEMA_VERSION = "scaffold.v1"


def _write_if_missing(path: Path, content: str, *, created: list[str], skipped: list[str]) -> None:
    if path.exists():
        skipped.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, content)
    created.append(str(path))


def _json_file(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _base_files(archetype: str, capabilities: list[str]) -> dict[str, str]:
    return {
        ".gitignore": "\n".join(["__pycache__/", ".pytest_cache/", "node_modules/", ".venv/", "dist/"]) + "\n",
        "README.md": (
            f"# {archetype}\n\n"
            "Scaffolded by kodawari init.\n\n"
            f"- capabilities: {', '.join(capabilities) or '(none)'}\n"
        ),
    }


def _python_api_files(*, app_root: str = "app") -> dict[str, str]:
    return {
        f"{app_root}/__init__.py": "",
        f"{app_root}/main.py": (
            '"""Scaffolded application entrypoint."""\n\n'
            "def healthcheck() -> dict[str, str]:\n"
            '    return {"status": "ok"}\n'
        ),
        f"{app_root}/service.py": (
            '"""Scaffolded service layer."""\n\n'
            "def summarize() -> str:\n"
            '    return "ok"\n'
        ),
        "tests/test_api.py": (
            f"from {app_root.replace('/', '.')} import main\n\n\n"
            "def test_healthcheck() -> None:\n"
            '    assert main.healthcheck()["status"] == "ok"\n'
        ),
        "requirements.txt": "pytest>=8\n",
    }


def _django_files(*, app_root: str = "app", manage_root: str = ".") -> dict[str, str]:
    manage_prefix = "" if manage_root == "." else f"{manage_root}/"
    return {
        f"{manage_prefix}manage.py": (
            '"""Scaffolded manage entrypoint."""\n\n'
            "def main() -> int:\n"
            "    return 0\n\n\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        ),
        f"{app_root}/__init__.py": "",
        f"{app_root}/views.py": (
            '"""Scaffolded view module."""\n\n'
            "def health_view() -> dict[str, str]:\n"
            '    return {"status": "ok"}\n'
        ),
        f"{app_root}/urls.py": 'urlpatterns = ["health_view"]\n',
        "tests/test_views.py": (
            f"from {app_root.replace('/', '.')} import views\n\n\n"
            "def test_health_view() -> None:\n"
            '    assert views.health_view()["status"] == "ok"\n'
        ),
        f"{manage_root}/requirements.txt".replace("./", ""): "pytest>=8\n",
    }


def _node_api_files(*, src_root: str = "src") -> dict[str, str]:
    return {
        f"{src_root}/server.js": (
            "function healthcheck() {\n"
            "  return { status: 'ok' };\n"
            "}\n\n"
            "module.exports = { healthcheck };\n"
        ),
        "tests/api.test.js": (
            "const assert = require('node:assert/strict');\n"
            f"const server = require('../{src_root}/server');\n\n"
            "assert.equal(server.healthcheck().status, 'ok');\n"
            "console.log('api smoke ok');\n"
        ),
        "package.json": _json_file(
            {
                "name": "workflow-init-node-api",
                "private": True,
                "scripts": {"test": "node tests/api.test.js"},
            }
        ),
    }


def _react_files(*, src_root: str = "src", package_root: str = ".") -> dict[str, str]:
    package_prefix = "" if package_root == "." else f"{package_root}/"
    return {
        f"{src_root}/App.js": (
            "function renderApp() {\n"
            "  return 'workflow-app';\n"
            "}\n\n"
            "module.exports = { renderApp };\n"
        ),
        f"{src_root}/App.test.js": (
            "const assert = require('node:assert/strict');\n"
            "const app = require('./App');\n\n"
            "assert.equal(app.renderApp(), 'workflow-app');\n"
            "console.log('frontend smoke ok');\n"
        ),
        f"{package_prefix}package.json": _json_file(
            {
                "name": "workflow-init-react-web",
                "private": True,
                "scripts": {"test": f"node {src_root}/App.test.js"},
            }
        ),
    }


def _capability_files(*, archetype: str, capabilities: list[str]) -> dict[str, str]:
    files: dict[str, str] = {}
    if "docker_deploy" in capabilities:
        files["Dockerfile"] = "FROM python:3.11-slim\nWORKDIR /app\nCOPY . .\nCMD [\"python\", \"-m\", \"pytest\", \"-q\"]\n"
        files["docker-compose.yml"] = (
            "services:\n"
            "  app:\n"
            "    build: .\n"
            "    command: python -m pytest -q\n"
        )
        files["scripts/verify_docker_deploy.py"] = (
            "from pathlib import Path\n\n"
            "assert Path('Dockerfile').exists()\n"
            "print('docker deploy verify ok')\n"
        )
    if "postgres_db" in capabilities:
        files[".env.example"] = "DATABASE_URL=postgresql://postgres:postgres@localhost:5432/app\n"
    if "docs_runbook" in capabilities:
        files["docs/RUNBOOK.md"] = "# Runbook\n\n- startup: run tests\n- release: run kodawari ship-readiness\n"
        files["scripts/verify_docs_runbook.py"] = (
            "from pathlib import Path\n\n"
            "assert Path('docs/RUNBOOK.md').exists()\n"
            "print('docs verify ok')\n"
        )
    if "capacitor_mobile" in capabilities:
        files["mobile/README.md"] = "# Mobile Wrapper\n\nScaffold placeholder for capacitor-style wrapper.\n"
        files["scripts/verify_mobile_wrapper.py"] = (
            "from pathlib import Path\n\n"
            "assert Path('mobile/README.md').exists()\n"
            "print('mobile wrapper verify ok')\n"
        )
    if "worker_scheduler" in capabilities:
        worker_path = "backend/app/worker.py" if archetype.startswith("fullstack_") else "app/worker.py"
        if archetype in {"node_api", "react_web"}:
            worker_path = "src/worker.js"
        files[worker_path] = (
            "def run_worker() -> str:\n    return 'scheduled'\n"
            if worker_path.endswith(".py")
            else "module.exports = { runWorker: () => 'scheduled' };\n"
        )
    if "monorepo_workspace" in capabilities:
        files["pnpm-workspace.yaml"] = "packages:\n  - packages/*\n"
        files["packages/README.md"] = "# Workspace\n\nScaffold placeholder for multi-package projects.\n"
        files["scripts/verify_workspace.py"] = (
            "from pathlib import Path\n\n"
            "assert Path('pnpm-workspace.yaml').exists()\n"
            "print('workspace verify ok')\n"
        )
    return files


def _archetype_files(archetype: str) -> dict[str, str]:
    if archetype == "fastapi_api":
        return _python_api_files(app_root="app")
    if archetype == "flask_api":
        return _python_api_files(app_root="app")
    if archetype == "django_web":
        return _django_files(app_root="app", manage_root=".")
    if archetype == "node_api":
        return _node_api_files(src_root="src")
    if archetype == "react_web":
        return _react_files(src_root="src", package_root=".")
    if archetype == "fullstack_fastapi_react":
        files = {}
        files.update(_python_api_files(app_root="backend/app"))
        files["backend/requirements.txt"] = files.pop("requirements.txt")
        files["backend/tests/test_api.py"] = files.pop("tests/test_api.py")
        files.update(_react_files(src_root="web/src", package_root="web"))
        return files
    if archetype == "fullstack_django_react":
        files = _django_files(app_root="backend/app", manage_root="backend")
        files["backend/tests/test_views.py"] = files.pop("tests/test_views.py")
        files.update(_react_files(src_root="web/src", package_root="web"))
        return files
    raise ValueError(f"unsupported archetype: {archetype}")


def scaffold_project(
    *,
    project_root: Path,
    archetype: str,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    resolved_archetype = normalize_archetype(archetype, default="auto")
    if resolved_archetype == "auto":
        raise ValueError("init requires an explicit non-auto archetype")
    resolved_capabilities = normalize_capabilities(capabilities)
    created: list[str] = []
    skipped: list[str] = []
    files = {}
    files.update(_base_files(resolved_archetype, resolved_capabilities))
    files.update(_archetype_files(resolved_archetype))
    files.update(_capability_files(archetype=resolved_archetype, capabilities=resolved_capabilities))
    for relative, content in files.items():
        _write_if_missing(
            (project_root / relative).resolve(),
            content,
            created=created,
            skipped=skipped,
        )
    return {
        "archetype": resolved_archetype,
        "capabilities": resolved_capabilities,
        "created_files": sorted(created),
        "skipped_files": sorted(skipped),
    }


def write_scaffold_manifest(
    planning_dir: Path,
    *,
    scaffold: dict[str, Any],
    project_root: Path,
) -> dict[str, Any]:
    """Persist the scaffold result alongside other planning artifacts so later
    rounds (and ``ensure_repo_inventory``) can prefer the explicit archetype
    chosen at init time over auto-detect on a near-empty filesystem.

    A3: this is the authoritative record of "init ran and chose THIS archetype",
    which auto-detect cannot reconstruct after the fact when only a few
    skeleton files exist.
    """
    planning_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCAFFOLD_MANIFEST_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(Path(project_root).resolve()),
        "archetype": str(scaffold.get("archetype") or ""),
        "capabilities": [str(item) for item in list(scaffold.get("capabilities") or [])],
        "created_files": [str(item) for item in list(scaffold.get("created_files") or [])],
        "skipped_files": [str(item) for item in list(scaffold.get("skipped_files") or [])],
    }
    atomic_write_text(
        planning_dir / SCAFFOLD_MANIFEST_FILENAME,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
    return payload


def read_scaffold_manifest(planning_dir: Path) -> dict[str, Any] | None:
    """Return parsed SCAFFOLD_MANIFEST.json or ``None`` if absent/corrupt.

    Returns ``None`` (not raises) on parse error so callers can degrade to
    auto-detect rather than failing the whole planning round on a stale or
    hand-edited manifest. Schema-version mismatch is treated as a soft
    degrade — the consumer logs and falls back to auto-detect.
    """
    path = Path(planning_dir) / SCAFFOLD_MANIFEST_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data

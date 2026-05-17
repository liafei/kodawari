"""Tests for backend/ layout detection fixes.

Covers fix groups:
  A. project_model.detect_archetype() and detect_capabilities() — backend/main.py, backend/worker.py
  B. task_graph.detect_project_layout() and detect_project_profile() — bare backend/ directory
  C. task_graph._existing_source_fallback() — mobile/www/index.html frontend fallback
  D. task_graph._discover_layer_keyword_source() — __init__.py / _*.py exclusion, exact segment match
  E. project_layout.kind correctness for single code root
  F. Integration: newsapp-like fixture produces correct graph layer assignments
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kodawari.project_model import (
    detect_archetype,
    detect_capabilities,
    detect_project_layout as pm_detect_project_layout,
)
from kodawari.autopilot.task_graph import (
    detect_project_layout,
    detect_project_profile,
    _existing_source_fallback,
    _discover_layer_keyword_source,
    _select_source_path,
    _task_executability,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _newsapp_fixture(root: Path) -> None:
    """Create a newsapp-like project structure."""
    _write(root / "backend" / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
    _write(root / "backend" / "api" / "router.py", "from fastapi import APIRouter\nrouter = APIRouter()")
    _write(root / "backend" / "api" / "v1" / "router.py", "from fastapi import APIRouter\nv1_router = APIRouter()")
    _write(root / "backend" / "api" / "v1" / "schemas.py", "from pydantic import BaseModel")
    _write(root / "backend" / "api" / "v1" / "services" / "__init__.py", "")
    _write(root / "backend" / "api" / "v1" / "services" / "account_repository.py", "class AccountRepo: pass")
    _write(root / "backend" / "api" / "v1" / "services" / "event_repository.py", "class EventRepo: pass")
    _write(root / "backend" / "api" / "v1" / "services" / "news_service.py", "class NewsService: pass")
    _write(root / "backend" / "worker.py", "# celery worker")
    _write(root / "mobile" / "www" / "index.html", "<html></html>")
    _write(root / "tests" / "test_api.py", "def test_health(): pass")
    _write(root / "docker-compose.prod.yml", "version: '3'")
    (root / "docs").mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Group A — project_model archetype / capability detection
# ---------------------------------------------------------------------------

class TestDetectArchetypeBackendMain:
    """detect_archetype() must recognise FastAPI in backend/main.py."""

    def test_fastapi_at_backend_main(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
        result = detect_archetype(tmp_path, "auto")
        assert result == "fastapi_api", f"Expected fastapi_api, got {result!r}"

    def test_flask_at_backend_main(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "from flask import Flask\napp = Flask(__name__)")
        result = detect_archetype(tmp_path, "auto")
        assert result == "flask_api", f"Expected flask_api, got {result!r}"

    def test_fastapi_at_app_main_still_works(self, tmp_path: Path) -> None:
        _write(tmp_path / "app" / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
        result = detect_archetype(tmp_path, "auto")
        assert result == "fastapi_api"

    def test_no_main_file_falls_back_to_fastapi_default(self, tmp_path: Path) -> None:
        result = detect_archetype(tmp_path, "auto")
        assert result == "fastapi_api"


class TestDetectCapabilitiesBackendWorker:
    """detect_capabilities() must detect worker_scheduler via backend/worker.py."""

    def test_worker_at_backend_worker_py(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "worker.py", "# celery worker")
        caps = detect_capabilities(project_root=tmp_path)
        assert "worker_scheduler" in caps

    def test_worker_at_app_worker_py_still_works(self, tmp_path: Path) -> None:
        _write(tmp_path / "app" / "worker.py", "# celery worker")
        caps = detect_capabilities(project_root=tmp_path)
        assert "worker_scheduler" in caps

    def test_no_worker_file(self, tmp_path: Path) -> None:
        caps = detect_capabilities(project_root=tmp_path)
        assert "worker_scheduler" not in caps


# ---------------------------------------------------------------------------
# Group B — task_graph layout / profile detection
# ---------------------------------------------------------------------------

class TestTaskGraphDetectProjectLayout:
    """detect_project_layout() must include 'backend' in code_roots."""

    def test_backend_dir_in_code_roots(self, tmp_path: Path) -> None:
        (tmp_path / "backend").mkdir()
        layout = detect_project_layout(tmp_path)
        assert "backend" in layout["code_roots"], f"code_roots={layout['code_roots']}"

    def test_app_dir_still_detected(self, tmp_path: Path) -> None:
        (tmp_path / "app").mkdir()
        layout = detect_project_layout(tmp_path)
        assert "app" in layout["code_roots"]

    def test_both_backend_and_app(self, tmp_path: Path) -> None:
        (tmp_path / "backend").mkdir()
        (tmp_path / "app").mkdir()
        layout = detect_project_layout(tmp_path)
        assert "backend" in layout["code_roots"]
        assert "app" in layout["code_roots"]


class TestDetectProjectProfile:
    """detect_project_profile() must return 'fastapi' for backend/main.py."""

    def test_fastapi_profile_from_backend_main(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
        profile = detect_project_profile(tmp_path)
        assert profile == "fastapi", f"Expected fastapi, got {profile!r}"

    def test_flask_profile_from_backend_main(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "from flask import Flask\napp = Flask(__name__)")
        profile = detect_project_profile(tmp_path)
        assert profile == "flask", f"Expected flask, got {profile!r}"

    def test_node_profile_from_package_json(self, tmp_path: Path) -> None:
        _write(tmp_path / "package.json", '{"name": "myapp"}')
        profile = detect_project_profile(tmp_path)
        assert profile == "node"

    def test_django_profile_from_manage_py(self, tmp_path: Path) -> None:
        _write(tmp_path / "manage.py", "#!/usr/bin/env python")
        profile = detect_project_profile(tmp_path)
        assert profile == "django"

    def test_fastapi_from_app_main_still_works(self, tmp_path: Path) -> None:
        _write(tmp_path / "app" / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
        profile = detect_project_profile(tmp_path)
        assert profile == "fastapi"


# ---------------------------------------------------------------------------
# Group C — frontend fallback includes mobile/www/index.html
# ---------------------------------------------------------------------------

class TestFrontendFallback:
    """_existing_source_fallback() must find mobile/www/index.html for frontend layer."""

    def test_mobile_www_index_html_found(self, tmp_path: Path) -> None:
        _write(tmp_path / "mobile" / "www" / "index.html", "<html></html>")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _existing_source_fallback(tmp_path, layout, "frontend")
        assert result == "mobile/www/index.html", f"Got {result!r}"

    def test_app_static_index_html_takes_priority(self, tmp_path: Path) -> None:
        """app/static/index.html takes priority over mobile/www/index.html."""
        _write(tmp_path / "app" / "static" / "index.html", "<html></html>")
        _write(tmp_path / "mobile" / "www" / "index.html", "<html></html>")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _existing_source_fallback(tmp_path, layout, "frontend")
        assert result == "app/static/index.html", f"Got {result!r}"

    def test_no_frontend_file_returns_empty(self, tmp_path: Path) -> None:
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _existing_source_fallback(tmp_path, layout, "frontend")
        assert result == ""


# ---------------------------------------------------------------------------
# Group D — keyword search: __init__.py exclusion + exact segment matching
# ---------------------------------------------------------------------------

class TestKeywordSearchExcludesInitFiles:
    """_discover_layer_keyword_source() must not return __init__.py or _*.py."""

    def test_init_py_in_services_dir_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "api" / "v1" / "services" / "__init__.py", "# package")
        _write(tmp_path / "backend" / "service.py", "class MyService: pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _discover_layer_keyword_source(tmp_path, layout, "service")
        assert "__init__" not in result, f"Should not return __init__.py, got {result!r}"
        assert result == "backend/service.py", f"Expected backend/service.py, got {result!r}"

    def test_underscore_file_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "_service_internal.py", "# private")
        _write(tmp_path / "backend" / "service.py", "class MyService: pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _discover_layer_keyword_source(tmp_path, layout, "service")
        assert not result.endswith("_service_internal.py"), f"Should skip _*.py, got {result!r}"
        assert result == "backend/service.py"

    def test_only_init_file_present_returns_empty(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "services" / "__init__.py", "# package")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _discover_layer_keyword_source(tmp_path, layout, "service")
        assert result == "", f"Expected empty, got {result!r}"


class TestKeywordSearchExactSegmentMatch:
    """Directory name 'services' must NOT match keyword 'service' via substring."""

    def test_services_dir_does_not_pollute_service_layer(self, tmp_path: Path) -> None:
        """A file under services/ whose stem does NOT contain 'service' must not match."""
        _write(tmp_path / "backend" / "api" / "v1" / "services" / "account_repository.py", "class Repo: pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _discover_layer_keyword_source(tmp_path, layout, "service")
        assert result == "", f"Should not match account_repository.py for service, got {result!r}"

    def test_exact_service_dir_still_matches(self, tmp_path: Path) -> None:
        """A file under a directory named exactly 'service' (singular) matches."""
        _write(tmp_path / "backend" / "service" / "handler.py", "class Handler: pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _discover_layer_keyword_source(tmp_path, layout, "service")
        assert result == "backend/service/handler.py", f"Got {result!r}"

    def test_stem_match_still_works_under_services_dir(self, tmp_path: Path) -> None:
        """A file whose stem contains 'service' still matches, regardless of directory."""
        _write(tmp_path / "backend" / "api" / "v1" / "services" / "news_service.py", "class NewsSvc: pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _discover_layer_keyword_source(tmp_path, layout, "service")
        assert "news_service" in result, f"Stem match should work, got {result!r}"

    def test_repository_not_polluted_by_repo_substring(self, tmp_path: Path) -> None:
        """'repos' directory should not match keyword 'repo'."""
        _write(tmp_path / "backend" / "repos" / "config.py", "CFG = {}")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _discover_layer_keyword_source(tmp_path, layout, "repository")
        assert result == "", f"Should not match config.py via repos/ dir, got {result!r}"


# ---------------------------------------------------------------------------
# Group E — kind correctness for single code root
# ---------------------------------------------------------------------------

class TestProjectLayoutKind:
    """kind must match the actual code root name, not assume app/src."""

    def test_kind_is_backend_when_only_backend_dir(self, tmp_path: Path) -> None:
        (tmp_path / "backend").mkdir()
        layout = detect_project_layout(tmp_path)
        assert layout["kind"] == "backend", f"Got kind={layout['kind']!r}"

    def test_kind_is_app_when_only_app_dir(self, tmp_path: Path) -> None:
        (tmp_path / "app").mkdir()
        layout = detect_project_layout(tmp_path)
        assert layout["kind"] == "app"

    def test_kind_is_src_when_only_src_dir(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        layout = detect_project_layout(tmp_path)
        assert layout["kind"] == "src"

    def test_kind_is_mixed_when_multiple_roots(self, tmp_path: Path) -> None:
        (tmp_path / "app").mkdir()
        (tmp_path / "backend").mkdir()
        layout = detect_project_layout(tmp_path)
        assert layout["kind"] == "mixed"

    def test_project_model_kind_backend(self, tmp_path: Path) -> None:
        """project_model.detect_project_layout() kind is also correct."""
        (tmp_path / "backend").mkdir()
        _write(tmp_path / "backend" / "main.py", "from fastapi import FastAPI\napp = FastAPI()")
        layout = pm_detect_project_layout(tmp_path, archetype="fastapi_api")
        assert layout["kind"] == "backend", f"Got kind={layout['kind']!r}"


# ---------------------------------------------------------------------------
# Group F — executability WARN on missing test files
# ---------------------------------------------------------------------------

class TestExecutabilityWarnMissingTests:
    """Missing test files should produce WARN, not silent PASS."""

    def test_all_test_files_missing_is_warn(self, tmp_path: Path) -> None:
        """WARN when source exists but ALL test files are missing."""
        _write(tmp_path / "backend" / "schemas.py", "class S: pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _task_executability(
            layer="schema",
            core_files=["backend/schemas.py", "tests/test_schema.py"],
            project_root=tmp_path,
            layout=layout,
            planning_mode="existing",
        )
        assert result["status"] == "WARN", f"Got {result!r}"
        assert any("test file not yet present" in issue for issue in result["issues"])

    def test_some_test_exists_is_pass(self, tmp_path: Path) -> None:
        """PASS when source exists and at least one test file exists (extra missing tests tolerated)."""
        _write(tmp_path / "backend" / "schemas.py", "class S: pass")
        _write(tmp_path / "tests" / "test_schema.py", "def test(): pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _task_executability(
            layer="schema",
            core_files=["backend/schemas.py", "tests/test_schema.py", "tests/test_schema_extra.py"],
            project_root=tmp_path,
            layout=layout,
            planning_mode="existing",
        )
        assert result["status"] == "PASS", f"Got {result!r}"

    def test_all_files_exist_is_pass(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "schemas.py", "class S: pass")
        _write(tmp_path / "tests" / "test_schema.py", "def test(): pass")
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _task_executability(
            layer="schema",
            core_files=["backend/schemas.py", "tests/test_schema.py"],
            project_root=tmp_path,
            layout=layout,
            planning_mode="existing",
        )
        assert result["status"] == "PASS", f"Got {result!r}"

    def test_missing_source_file_is_fail(self, tmp_path: Path) -> None:
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _task_executability(
            layer="schema",
            core_files=["backend/schemas.py", "tests/test_schema.py"],
            project_root=tmp_path,
            layout=layout,
            planning_mode="existing",
        )
        assert result["status"] == "FAIL", f"Got {result!r}"

    def test_missing_source_plus_test_is_fail_with_both_issues(self, tmp_path: Path) -> None:
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _task_executability(
            layer="schema",
            core_files=["backend/schemas.py", "tests/test_schema.py"],
            project_root=tmp_path,
            layout=layout,
            planning_mode="existing",
        )
        assert result["status"] == "FAIL"
        assert len(result["issues"]) == 2, f"Expected 2 issues, got {result['issues']}"

    def test_greenfield_mode_always_passes(self, tmp_path: Path) -> None:
        layout = {"code_roots": ["backend"], "test_roots": ["tests"], "workspace_roots": []}
        result = _task_executability(
            layer="schema",
            core_files=["backend/schemas.py", "tests/test_schema.py"],
            project_root=tmp_path,
            layout=layout,
            planning_mode="greenfield",
        )
        assert result["status"] != "FAIL", f"Got {result!r}"


# ---------------------------------------------------------------------------
# Group G — Integration: newsapp-like full graph verification
# ---------------------------------------------------------------------------

class TestNewsappIntegrationGraph:
    """End-to-end: newsapp-like fixture must produce correctly separated layers."""

    def test_schema_resolves_to_schemas_py(self, tmp_path: Path) -> None:
        _newsapp_fixture(tmp_path)
        layout = detect_project_layout(tmp_path)
        profile = detect_project_profile(tmp_path)
        result = _select_source_path("schema", tmp_path, profile=profile, layout=layout)
        assert result == "backend/api/v1/schemas.py", f"Got {result!r}"

    def test_route_resolves_to_router_not_main(self, tmp_path: Path) -> None:
        _newsapp_fixture(tmp_path)
        layout = detect_project_layout(tmp_path)
        profile = detect_project_profile(tmp_path)
        result = _select_source_path("route", tmp_path, profile=profile, layout=layout)
        assert "router" in result, f"Route should resolve to a router file, got {result!r}"
        assert result != "backend/main.py", "Route should NOT fall to backend/main.py"

    def test_service_and_repository_do_not_share_source(self, tmp_path: Path) -> None:
        _newsapp_fixture(tmp_path)
        layout = detect_project_layout(tmp_path)
        profile = detect_project_profile(tmp_path)
        svc = _select_source_path("service", tmp_path, profile=profile, layout=layout)
        repo = _select_source_path("repository", tmp_path, profile=profile, layout=layout)
        assert svc != repo, f"service and repository must not share source file: both={svc!r}"

    def test_frontend_resolves_to_mobile_www(self, tmp_path: Path) -> None:
        _newsapp_fixture(tmp_path)
        layout = detect_project_layout(tmp_path)
        profile = detect_project_profile(tmp_path)
        result = _select_source_path("frontend", tmp_path, profile=profile, layout=layout)
        assert result == "mobile/www/index.html", f"Got {result!r}"

    def test_archetype_and_layout_correct(self, tmp_path: Path) -> None:
        _newsapp_fixture(tmp_path)
        assert detect_archetype(tmp_path, "auto") == "fastapi_api"
        assert detect_project_profile(tmp_path) == "fastapi"
        layout = detect_project_layout(tmp_path)
        assert "backend" in layout["code_roots"]
        assert layout["kind"] == "backend"
        caps = detect_capabilities(project_root=tmp_path)
        assert "worker_scheduler" in caps
        assert "docker_deploy" in caps
        assert "capacitor_mobile" in caps

    def test_project_model_layout_includes_mobile_www_code_root(self, tmp_path: Path) -> None:
        _newsapp_fixture(tmp_path)
        layout = pm_detect_project_layout(tmp_path, archetype="fastapi_api")
        assert "backend" in layout["code_roots"]
        assert "mobile/www" in layout["code_roots"]
        assert layout["kind"] == "mixed"

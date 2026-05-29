"""Shared project-model helpers for generic kodawari planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ARCHETYPES: dict[str, dict[str, Any]] = {
    "fastapi_api": {
        "family": "python",
        "frameworks": ["fastapi"],
        "default_layout": {
            "kind": "app",
            "code_roots": ["app"],
            "test_roots": ["tests"],
            "workspace_roots": [],
        },
        "default_surfaces": ["backend"],
        "task_layers": ["schema", "repository", "service", "route"],
    },
    "flask_api": {
        "family": "python",
        "frameworks": ["flask"],
        "default_layout": {
            "kind": "app",
            "code_roots": ["app"],
            "test_roots": ["tests"],
            "workspace_roots": [],
        },
        "default_surfaces": ["backend"],
        "task_layers": ["repository", "service", "route"],
    },
    "django_web": {
        "family": "python",
        "frameworks": ["django"],
        "default_layout": {
            "kind": "app",
            "code_roots": ["app"],
            "test_roots": ["tests"],
            "workspace_roots": [],
        },
        "default_surfaces": ["backend"],
        "task_layers": ["model", "repository", "service", "route"],
    },
    "node_api": {
        "family": "node",
        "frameworks": ["node"],
        "default_layout": {
            "kind": "src",
            "code_roots": ["src"],
            "test_roots": ["tests"],
            "workspace_roots": [],
        },
        "default_surfaces": ["backend"],
        "task_layers": ["schema", "repository", "service", "route"],
    },
    "react_web": {
        "family": "node",
        "frameworks": ["react"],
        "default_layout": {
            "kind": "src",
            "code_roots": ["src"],
            "test_roots": ["src", "tests"],
            "workspace_roots": [],
        },
        "default_surfaces": ["frontend"],
        "task_layers": ["frontend", "util"],
    },
    "fullstack_fastapi_react": {
        "family": "fullstack",
        "frameworks": ["fastapi", "react"],
        "default_layout": {
            "kind": "mixed",
            "code_roots": ["backend/app", "web/src"],
            "test_roots": ["backend/tests", "web/tests"],
            "workspace_roots": [],
        },
        "default_surfaces": ["backend", "frontend"],
        "task_layers": ["schema", "repository", "service", "route", "frontend"],
    },
    "fullstack_django_react": {
        "family": "fullstack",
        "frameworks": ["django", "react"],
        "default_layout": {
            "kind": "mixed",
            "code_roots": ["backend/app", "web/src"],
            "test_roots": ["backend/tests", "web/tests"],
            "workspace_roots": [],
        },
        "default_surfaces": ["backend", "frontend"],
        "task_layers": ["model", "repository", "service", "route", "frontend"],
    },
}

CAPABILITIES: dict[str, dict[str, Any]] = {
    "postgres_db": {"surface": "backend", "frameworks": ["postgres"]},
    "docker_deploy": {"surface": "scripts_deploy", "frameworks": ["docker"]},
    "capacitor_mobile": {"surface": "mobile_wrapper", "frameworks": ["capacitor"]},
    "monorepo_workspace": {"surface": "workspace", "frameworks": ["workspace"]},
    "worker_scheduler": {"surface": "backend", "frameworks": ["worker"]},
    "docs_runbook": {"surface": "docs", "frameworks": ["docs"]},
}

ARCHETYPE_ALIASES = {
    "auto": "auto",
    "python": "fastapi_api",
    "fastapi": "fastapi_api",
    "flask": "flask_api",
    "django": "django_web",
    "node": "node_api",
    "react": "react_web",
}

SURFACE_ORDER = ("backend", "frontend", "mobile_wrapper", "scripts_deploy", "workspace", "docs")
FLAT_NODE_ENTRY_CANDIDATES = ("server.js", "server.mjs", "app.js", "app.mjs", "index.js", "index.mjs")


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def list_supported_archetypes() -> list[str]:
    return sorted(ARCHETYPES)


def list_supported_capabilities() -> list[str]:
    return sorted(CAPABILITIES)


def normalize_archetype(value: str | None, *, default: str = "auto") -> str:
    normalized = _clean_text(value, default=default).lower()
    normalized = ARCHETYPE_ALIASES.get(normalized, normalized)
    if normalized == "auto":
        return "auto"
    if normalized not in ARCHETYPES:
        supported = ", ".join(list_supported_archetypes())
        raise ValueError(f"unsupported archetype '{normalized}'. Supported: {supported}")
    return normalized


def normalize_capabilities(values: list[str] | None) -> list[str]:
    normalized: list[str] = []
    for raw in list(values or []):
        text = _clean_text(raw).lower()
        if not text:
            continue
        if text not in CAPABILITIES:
            supported = ", ".join(list_supported_capabilities())
            raise ValueError(f"unsupported capability '{text}'. Supported: {supported}")
        if text not in normalized:
            normalized.append(text)
    return normalized


def profile_from_archetype(archetype: str) -> str:
    mapping = {
        "fastapi_api": "fastapi",
        "flask_api": "flask",
        "django_web": "django",
        "node_api": "node",
        "react_web": "node",
        "fullstack_fastapi_react": "fastapi",
        "fullstack_django_react": "django",
    }
    return mapping.get(_clean_text(archetype).lower(), "python")


def derive_task_layers(
    *,
    archetype: str,
    capabilities: list[str] | None = None,
    prd_layers: list[str] | None = None,
) -> list[str]:
    explicit = [item for item in _string_list(prd_layers) if item]
    base = list(ARCHETYPES.get(archetype, {}).get("task_layers") or ["service", "repository", "route"])
    resolved: list[str] = []
    for layer in base + explicit:
        if layer in {"schema", "repository", "service", "route", "frontend", "model", "util"} and layer not in resolved:
            resolved.append(layer)
    if "capacitor_mobile" in list(capabilities or []) and "frontend" not in resolved:
        resolved.append("frontend")
    return resolved[:5]


def detect_archetype(project_root: Path | None, requested_archetype: str = "auto") -> str:
    normalized = normalize_archetype(requested_archetype, default="auto")
    if normalized != "auto":
        return normalized
    root = Path(project_root).resolve() if project_root is not None else None
    if root is None or not root.exists():
        return "fastapi_api"
    has_manage = any((root / candidate).exists() for candidate in ("manage.py", "backend/manage.py"))
    has_package_root = any((root / candidate).exists() for candidate in ("package.json", "web/package.json", "app/package.json"))
    has_fastapi = _contains_framework_text(root, ["app/main.py", "backend/app/main.py", "backend/main.py", "src/main.py"], marker="FastAPI")
    has_flask = _contains_framework_text(root, ["app/main.py", "backend/app/main.py", "backend/main.py", "src/main.py"], marker="Flask")
    has_react = _package_json_mentions(root, package_candidates=("package.json", "web/package.json", "app/package.json"), token="react")
    if has_react and has_fastapi:
        return "fullstack_fastapi_react"
    if has_react and has_manage:
        return "fullstack_django_react"
    if has_react:
        return "react_web"
    if has_manage:
        return "django_web"
    if has_fastapi:
        return "fastapi_api"
    if has_flask:
        return "flask_api"
    if has_package_root:
        return "node_api"
    return "fastapi_api"


def detect_capabilities(
    *,
    project_root: Path | None,
    requested_capabilities: list[str] | None = None,
    archetype: str | None = None,
) -> list[str]:
    explicit = normalize_capabilities(requested_capabilities)
    root = Path(project_root).resolve() if project_root is not None else None
    detected: list[str] = []
    if root is not None and root.exists():
        if _has_any_path(root, ("docker-compose.yml", "docker-compose.prod.yml", "Dockerfile", "Dockerfile.backend", "Dockerfile.web")):
            detected.append("docker_deploy")
        if _has_any_path(root, ("mobile", "capacitor.config.ts", "capacitor.config.json", "app/capacitor.config.ts")):
            detected.append("capacitor_mobile")
        if _has_any_path(root, ("packages", "pnpm-workspace.yaml", "turbo.json")):
            detected.append("monorepo_workspace")
        if _has_any_path(root, ("docs", "runbooks", "README.md")):
            detected.append("docs_runbook")
        if _package_json_mentions(root, package_candidates=("package.json", "web/package.json", "app/package.json"), token="postgres"):
            detected.append("postgres_db")
        if _has_any_path(root, ("celery.py", "worker.py", "app/worker.py", "backend/worker.py", "backend/app/worker.py", "scripts/worker.py")):
            detected.append("worker_scheduler")
    resolved: list[str] = []
    for item in explicit + detected:
        if item not in resolved:
            resolved.append(item)
    return resolved


def build_repo_inventory_payload(
    *,
    project_root: Path,
    archetype: str = "auto",
    capabilities: list[str] | None = None,
    mode: str = "existing",
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    resolved_archetype = detect_archetype(root, archetype)
    resolved_capabilities = detect_capabilities(
        project_root=root,
        requested_capabilities=capabilities,
        archetype=resolved_archetype,
    )
    layout = detect_project_layout(root, archetype=resolved_archetype)
    package_managers = detect_package_managers(root, archetype=resolved_archetype)
    surfaces = build_surface_inventory(
        project_root=root,
        archetype=resolved_archetype,
        capabilities=resolved_capabilities,
        layout=layout,
    )
    return {
        "project_root": str(root),
        "mode": _clean_text(mode, default="existing"),
        "archetype": resolved_archetype,
        "capabilities": list(resolved_capabilities),
        "languages": detect_languages(root, archetype=resolved_archetype),
        "frameworks": list(ARCHETYPES.get(resolved_archetype, {}).get("frameworks") or []),
        "package_managers": package_managers,
        "project_layout": layout,
        "surfaces": surfaces,
        "verify_surfaces": [dict(item) for item in surfaces if str(item.get("verify_command") or "").strip()],
    }


def detect_project_layout(project_root: Path, *, archetype: str) -> dict[str, Any]:
    layout = dict(ARCHETYPES.get(archetype, {}).get("default_layout") or {})
    code_roots = list(layout.get("code_roots") or [])
    test_roots = list(layout.get("test_roots") or [])
    workspace_roots = list(layout.get("workspace_roots") or [])
    flat_node_entry = ""
    if project_root.exists():
        detected_code = _existing_roots(
            project_root,
            code_roots
            + [
                "app",
                "src",
                "backend",
                "backend/app",
                "web/src",
                "frontend/src",
                "mobile/www",
                "packages/api/src",
                "packages/web/src",
            ],
        )
        if archetype == "node_api":
            flat_node_entry = _flat_node_entry_file(project_root)
        detected_tests = _existing_roots(project_root, test_roots + ["tests", "test", "backend/tests", "web/tests", "frontend/tests", "packages/api/tests", "packages/web/tests"])
        detected_workspaces = _existing_roots(project_root, workspace_roots + ["packages"])
        if detected_code:
            code_roots = detected_code
        elif flat_node_entry:
            code_roots = [flat_node_entry]
        if detected_tests:
            test_roots = detected_tests
        if detected_workspaces:
            workspace_roots = detected_workspaces
    kind = _clean_text(layout.get("kind"), default="mixed")
    if workspace_roots:
        kind = "monorepo"
    elif flat_node_entry and code_roots == [flat_node_entry]:
        kind = "flat"
    elif len(code_roots) == 1:
        kind = code_roots[0]
    elif len(code_roots) > 1:
        kind = "mixed"
    return {
        "kind": kind,
        "code_roots": code_roots,
        "test_roots": test_roots,
        "workspace_roots": workspace_roots,
    }


def _flat_node_entry_file(project_root: Path) -> str:
    for candidate in FLAT_NODE_ENTRY_CANDIDATES:
        if (project_root / candidate).is_file():
            return candidate
    return ""


def detect_languages(project_root: Path, *, archetype: str) -> list[str]:
    languages: list[str] = []
    if archetype in {"fastapi_api", "flask_api", "django_web", "fullstack_fastapi_react", "fullstack_django_react"}:
        languages.append("python")
    if archetype in {"node_api", "react_web", "fullstack_fastapi_react", "fullstack_django_react"}:
        languages.append("javascript")
    if project_root.exists() and _has_any_path(project_root, ("mobile/android", "mobile/ios")):
        languages.append("mobile_wrapper")
    return languages


def detect_package_managers(project_root: Path, *, archetype: str) -> list[str]:
    managers: list[str] = []
    if archetype in {"fastapi_api", "flask_api", "django_web", "fullstack_fastapi_react", "fullstack_django_react"}:
        managers.append("pip")
    if archetype in {"node_api", "react_web", "fullstack_fastapi_react", "fullstack_django_react"}:
        if (project_root / "pnpm-lock.yaml").exists():
            managers.append("pnpm")
        elif (project_root / "yarn.lock").exists():
            managers.append("yarn")
        else:
            managers.append("npm")
    return managers


def build_surface_inventory(
    *,
    project_root: Path,
    archetype: str,
    capabilities: list[str],
    layout: dict[str, Any],
) -> list[dict[str, Any]]:
    surfaces: list[dict[str, Any]] = []
    for name in ARCHETYPES.get(archetype, {}).get("default_surfaces") or []:
        surface = _surface_payload(
            name=name,
            project_root=project_root,
            archetype=archetype,
            capabilities=capabilities,
            layout=layout,
        )
        if surface:
            surfaces.append(surface)
    for capability in capabilities:
        surface_name = str(CAPABILITIES.get(capability, {}).get("surface") or "").strip()
        if not surface_name or surface_name in {item["name"] for item in surfaces if isinstance(item, dict)}:
            continue
        surface = _surface_payload(
            name=surface_name,
            project_root=project_root,
            archetype=archetype,
            capabilities=capabilities,
            layout=layout,
        )
        if surface:
            surfaces.append(surface)
    ordered: list[dict[str, Any]] = []
    for name in SURFACE_ORDER:
        for item in surfaces:
            if item.get("name") == name:
                ordered.append(item)
    return ordered


def surface_names_for_paths(
    changed_files: list[str],
    *,
    inventory: dict[str, Any] | None = None,
    archetype: str | None = None,
    capabilities: list[str] | None = None,
) -> list[str]:
    payload = dict(inventory or {})
    surfaces = [dict(item) for item in list(payload.get("surfaces") or []) if isinstance(item, dict)]
    if not surfaces:
        temp_layout = detect_project_layout(Path(payload.get("project_root") or "."), archetype=_clean_text(archetype, default="fastapi_api"))
        surfaces = build_surface_inventory(
            project_root=Path(payload.get("project_root") or "."),
            archetype=_clean_text(archetype, default="fastapi_api"),
            capabilities=list(capabilities or []),
            layout=temp_layout,
        )
    resolved: list[str] = []
    normalized_files = [str(item).strip().replace("\\", "/") for item in changed_files if str(item).strip()]
    for item in surfaces:
        roots = [str(raw).strip().replace("\\", "/") for raw in list(item.get("roots") or []) if str(raw).strip()]
        for changed in normalized_files:
            if any(changed == root or changed.startswith(f"{root}/") for root in roots if root):
                name = str(item.get("name") or "").strip()
                if name and name not in resolved:
                    resolved.append(name)
                    break
    if not resolved:
        for item in surfaces:
            name = str(item.get("name") or "").strip()
            if name and name not in resolved:
                resolved.append(name)
    return resolved


def default_verify_command_for_surface(surface: dict[str, Any]) -> str:
    return _clean_text(surface.get("verify_command"))


def _root_prefixed_npm_test(project_root: Path, *, package_dirs: list[str]) -> str:
    for package_dir in package_dirs:
        normalized = _clean_text(package_dir).replace("\\", "/")
        if not normalized:
            continue
        if (project_root / normalized / "package.json").exists():
            return f"npm --prefix {normalized} test -- --runInBand"
    if (project_root / "package.json").exists():
        return "npm test -- --runInBand"
    return "npm test -- --runInBand"


def _surface_roots(*, name: str, layout: dict[str, Any]) -> list[str]:
    code_roots = [str(item).strip() for item in list(layout.get("code_roots") or []) if str(item).strip()]
    test_roots = [str(item).strip() for item in list(layout.get("test_roots") or []) if str(item).strip()]
    combined = code_roots + test_roots
    if name == "backend":
        preferred = [item for item in combined if not item.startswith(("web/", "frontend/", "mobile/", "packages/web/"))]
        return preferred or combined
    if name == "frontend":
        preferred = [
            item
            for item in combined
            if item.startswith(("web/", "frontend/", "app/src", "src")) or item in {"src", "tests"}
        ]
        return preferred or combined
    if name == "workspace":
        workspaces = [str(item).strip() for item in list(layout.get("workspace_roots") or []) if str(item).strip()]
        return workspaces or ["packages"]
    return combined


def _surface_dict(
    name: str, framework: str, roots: list[str], verify_command: str, capabilities: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "framework": framework,
        "roots": roots,
        "verify_command": verify_command,
        "capabilities": [item for item in capabilities if str(CAPABILITIES.get(item, {}).get("surface") or "") == name],
    }


def _surface_payload(
    *,
    name: str,
    project_root: Path,
    archetype: str,
    capabilities: list[str],
    layout: dict[str, Any],
) -> dict[str, Any]:
    if name == "backend":
        framework = profile_from_archetype(archetype)
        roots = _existing_roots(project_root, _surface_roots(name=name, layout=layout)) or _surface_roots(name=name, layout=layout)
        verify_command = (
            "pytest -q"
            if framework in {"fastapi", "flask", "django", "python"}
            else _root_prefixed_npm_test(project_root, package_dirs=[".", "app", "backend"])
        )
        return _surface_dict(name, framework, roots, verify_command, capabilities)
    if name == "frontend":
        roots = _existing_roots(project_root, _surface_roots(name=name, layout=layout) + ["web/src", "frontend/src", "src", "web/tests", "frontend/tests", "tests"]) or ["src"]
        verify_command = _root_prefixed_npm_test(project_root, package_dirs=["web", "frontend", "app", "."])
        return _surface_dict(name, "react", roots, verify_command, capabilities)
    if name == "mobile_wrapper":
        roots = _existing_roots(project_root, ["mobile", "app/mobile", "mobile/android", "mobile/ios"]) or ["mobile"]
        verify_command = _capability_verify_command(project_root, preferred="scripts/verify_mobile_wrapper.py")
        return _surface_dict(name, "capacitor", roots, verify_command, capabilities)
    if name == "scripts_deploy":
        roots = _existing_roots(project_root, ["scripts", "docker", "deploy", "."]) or ["scripts"]
        verify_command = _capability_verify_command(project_root, preferred="scripts/verify_docker_deploy.py")
        return _surface_dict(name, "ops", roots, verify_command, capabilities)
    if name == "workspace":
        roots = _existing_roots(project_root, _surface_roots(name=name, layout=layout) + ["packages"]) or ["packages"]
        verify_command = _capability_verify_command(project_root, preferred="scripts/verify_workspace.py")
        if not verify_command and (project_root / "pnpm-workspace.yaml").exists():
            verify_command = "npm test --workspaces --if-present"
        return _surface_dict(name, "workspace", roots, verify_command, capabilities)
    if name == "docs":
        roots = _existing_roots(project_root, ["docs", "runbooks"]) or ["docs"]
        verify_command = _capability_verify_command(project_root, preferred="scripts/verify_docs_runbook.py")
        return _surface_dict(name, "docs", roots, verify_command, capabilities)
    return {}


def _capability_verify_command(project_root: Path, *, preferred: str) -> str:
    candidate = (project_root / preferred).resolve()
    if candidate.exists():
        return str(Path(preferred)).replace("\\", "/")
    return ""


def _contains_framework_text(project_root: Path, candidates: list[str], *, marker: str) -> bool:
    for relative in candidates:
        path = (project_root / relative).resolve()
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if marker in text:
            return True
    return False


def _package_json_mentions(project_root: Path, *, package_candidates: tuple[str, ...], token: str) -> bool:
    normalized_token = token.lower()
    for relative in package_candidates:
        path = (project_root / relative).resolve()
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        deps = {}
        deps.update(dict(payload.get("dependencies") or {}))
        deps.update(dict(payload.get("devDependencies") or {}))
        if any(normalized_token in str(key).lower() for key in deps):
            return True
    return False


def _existing_roots(project_root: Path, roots: list[str]) -> list[str]:
    resolved: list[str] = []
    for raw in roots:
        text = _clean_text(raw).replace("\\", "/")
        if not text:
            continue
        if (project_root / text).exists() and text not in resolved:
            resolved.append(text)
    return resolved


def _has_any_path(project_root: Path, candidates: tuple[str, ...]) -> bool:
    return any((project_root / candidate).exists() for candidate in candidates)

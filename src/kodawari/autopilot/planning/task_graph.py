"""Contract-first task graph helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.task_modes import verification_only_allows_empty_files
from kodawari.project_model import derive_task_layers, profile_from_archetype
from kodawari.source_of_truth import build_contract_coverage_hints, canonicalize_source_of_truth


SCHEMA_VERSION = "contract_first.task_graph.v1"
LAYER_ORDER = ["schema", "repository", "service", "route", "frontend", "model", "util"]
FLAT_NODE_ENTRY_CANDIDATES = ("server.js", "server.mjs", "app.js", "app.mjs", "index.js", "index.mjs")
LAYER_CORE_CANDIDATES = {
    "schema": {
        "source": ["app/schemas.py", "backend/app/schemas.py", "backend/schemas.py", "backend/api/v1/schemas.py", "src/schema.py", "src/schemas.py"],
        "tests": ["tests/test_api.py", "backend/tests/test_api.py", "tests/test_schema.py"],
    },
    "repository": {
        "source": [
            "app/repository.py",
            "app/repositories.py",
            "backend/app/repository.py",
            "backend/repository.py",
            "src/repository.py",
            "app/src/shared/api/runtimeClient.ts",
            "app/src/shared/api/httpClient.ts",
            "app/src/features/feed/index.ts",
            "app/src/features/auth/index.ts",
        ],
        "tests": [
            "tests/test_repository.py",
            "backend/tests/test_repository.py",
            "tests/test_api.py",
            "backend/tests/test_api.py",
            "app/src/shared/api/httpClient.test.ts",
            "app/src/shared/session/sessionStore.test.ts",
        ],
    },
    "service": {
        "source": [
            "app/services.py",
            "app/service.py",
            "backend/app/service.py",
            "backend/app/services.py",
            "backend/service.py",
            "backend/services.py",
            "src/service.py",
            "app/src/features/auth/useAuthSessionController.ts",
            "app/src/features/feed/index.ts",
            "app/src/features/auth/index.ts",
            "app/src/features/manage/index.ts",
            "app/src/shared/api/runtimeClient.ts",
        ],
        "tests": [
            "tests/test_service.py",
            "backend/tests/test_service.py",
            "tests/test_api.py",
            "backend/tests/test_api.py",
            "app/src/features/feed/FeedToolbar.test.tsx",
            "app/src/features/auth/AuthSessionPanel.test.tsx",
            "app/src/features/manage/OpsSnapshotPanel.test.tsx",
        ],
    },
    "route": {
        "source": [
            "app/main.py",
            "app/routes.py",
            "backend/app/main.py",
            "backend/app/routes.py",
            "backend/api/router.py",
            "backend/api/v1/router.py",
            "backend/routes.py",
            "backend/main.py",
            "src/routes.py",
            "src/main.py",
            "app/src/main.tsx",
            "app/src/app/AppShell.tsx",
            "app/src/features/app/AppChrome.tsx",
        ],
        "tests": [
            "tests/test_api.py",
            "backend/tests/test_api.py",
            "tests/test_routes.py",
            "app/src/app/LegacyApp.test.tsx",
            "app/src/features/feed/FeedToolbar.test.tsx",
        ],
    },
    "frontend": {
        "source": [
            "web/src/main.tsx",
            "web/src/main.jsx",
            "web/src/main.js",
            "web/src/main.ts",
            "web/src/App.js",
            "web/src/App.jsx",
            "web/src/App.tsx",
            "frontend/src/main.tsx",
            "frontend/src/main.jsx",
            "frontend/src/main.js",
            "frontend/src/main.ts",
            "frontend/src/App.js",
            "frontend/src/App.jsx",
            "frontend/src/App.tsx",
            "src/main.tsx",
            "src/main.jsx",
            "src/main.js",
            "src/main.ts",
            "src/App.js",
            "src/App.jsx",
            "src/App.tsx",
            "app/src/main.tsx",
            "app/src/main.jsx",
            "app/src/main.js",
            "app/src/main.ts",
            "app/src/App.js",
            "app/src/App.jsx",
            "app/src/App.tsx",
            "app/src/app/AppShell.tsx",
            "app/src/app/AppShell.jsx",
            "app/src/app/AppShell.js",
            "app/static/index.html",
            "src/frontend/ui.ts",
        ],
        "tests": [
            "web/src/main.test.tsx",
            "web/src/App.test.js",
            "frontend/src/main.test.tsx",
            "frontend/src/App.test.js",
            "src/main.test.tsx",
            "src/App.test.js",
            "app/src/main.test.tsx",
            "app/src/App.test.tsx",
            "tests/test_frontend_ui.py",
        ],
    },
    "model": {
        "source": [
            "app/models.py",
            "backend/app/models.py",
            "src/models.py",
            "app/src/shared/session/sessionStore.ts",
            "app/src/shared/api/runtimeClient.ts",
            "app/src/features/feed/index.ts",
        ],
        "tests": [
            "tests/test_models.py",
            "backend/tests/test_models.py",
            "tests/test_api.py",
            "app/src/shared/session/sessionStore.test.ts",
            "app/src/shared/api/httpClient.test.ts",
        ],
    },
    "util": {
        "source": [
            "app/utils.py",
            "backend/app/utils.py",
            "src/utils.py",
            "app/src/shared/utils.ts",
            "app/src/shared/api/httpClient.ts",
            "app/src/shared/session/sessionStore.ts",
            "frontend/src/shared/utils.ts",
            "web/src/shared/utils.ts",
            "src/shared/utils.ts",
        ],
        "tests": [
            "tests/test_utils.py",
            "backend/tests/test_utils.py",
            "app/src/shared/api/httpClient.test.ts",
            "app/src/shared/session/sessionStore.test.ts",
            "app/src/shared/utils.test.ts",
            "src/shared/utils.test.ts",
        ],
    },
}
PROFILE_LAYER_CANDIDATES = {
    "node": {
        "route": {
            "source": [
                "app/src/main.tsx",
                "app/src/app/AppShell.tsx",
                "app/src/features/app/AppChrome.tsx",
                "src/routes.ts",
                "src/routes.js",
                "src/server.ts",
                "src/server.js",
                "app/routes.ts",
                "app/routes.js",
                "server.js",
                "server.mjs",
                "app.js",
                "app.mjs",
                "index.js",
                "index.mjs",
            ],
            "tests": [
                "app/src/app/LegacyApp.test.tsx",
                "app/src/features/feed/FeedToolbar.test.tsx",
                "tests/routes.test.ts",
                "tests/routes.test.js",
                "tests/api.test.ts",
                "tests/api.test.js",
            ],
        },
        "service": {
            "source": [
                "app/src/features/auth/useAuthSessionController.ts",
                "app/src/features/feed/index.ts",
                "app/src/features/auth/index.ts",
                "app/src/features/manage/index.ts",
                "src/services.ts",
                "src/services.js",
                "src/service.ts",
                "src/service.js",
                "app/services.ts",
                "app/services.js",
                "server.js",
                "server.mjs",
                "app.js",
                "app.mjs",
                "index.js",
                "index.mjs",
            ],
            "tests": [
                "app/src/features/feed/FeedToolbar.test.tsx",
                "app/src/features/auth/AuthSessionPanel.test.tsx",
                "app/src/features/manage/OpsSnapshotPanel.test.tsx",
                "tests/services.test.ts",
                "tests/services.test.js",
                "tests/api.test.ts",
                "tests/api.test.js",
            ],
        },
        "repository": {
            "source": [
                "app/src/shared/api/runtimeClient.ts",
                "app/src/shared/api/httpClient.ts",
                "app/src/features/feed/index.ts",
                "app/src/features/auth/index.ts",
                "src/repository.ts",
                "src/repository.js",
                "src/repositories.ts",
                "src/repositories.js",
                "app/repository.ts",
                "app/repository.js",
                "server.js",
                "server.mjs",
                "app.js",
                "app.mjs",
                "index.js",
                "index.mjs",
            ],
            "tests": [
                "app/src/shared/api/httpClient.test.ts",
                "app/src/shared/session/sessionStore.test.ts",
                "tests/repository.test.ts",
                "tests/repository.test.js",
                "tests/api.test.ts",
                "tests/api.test.js",
            ],
        },
        "schema": {
            "source": [
                "src/schema.ts",
                "src/schema.js",
                "src/schemas.ts",
                "src/schemas.js",
                "app/schema.ts",
                "app/schema.js",
                "server.js",
                "server.mjs",
                "app.js",
                "app.mjs",
                "index.js",
                "index.mjs",
            ],
            "tests": ["tests/schema.test.ts", "tests/schema.test.js"],
        },
    },
    "django": {
        "route": {
            "source": ["app/views.py", "app/urls.py", "src/views.py"],
            "tests": ["tests/test_views.py", "tests/test_api.py"],
        }
    },
    "flask": {
        "route": {
            "source": ["app/main.py", "app/routes.py", "src/routes.py"],
            "tests": ["tests/test_routes.py", "tests/test_api.py"],
        }
    },
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _ordered_layers(prd_intake: dict[str, Any]) -> list[str]:
    layers = _string_list(prd_intake.get("layers"))
    normalized: list[str] = []
    for layer in LAYER_ORDER:
        if layer in layers:
            normalized.append(layer)
    if not normalized:
        normalized = ["service", "repository", "route"]
    return normalized[:5]


def _preferred_code_root(project_root: Path | None, *, layout: dict[str, Any] | None = None) -> str:
    if layout is not None:
        code_roots = [str(item).strip() for item in list(layout.get("code_roots") or []) if str(item).strip()]
        if code_roots:
            return code_roots[0]
    if project_root is None:
        return "src"
    app_dir = project_root / "app"
    src_dir = project_root / "src"
    if app_dir.exists():
        return "app"
    if src_dir.exists():
        return "src"
    return "src"


def detect_project_layout(project_root: Path | None) -> dict[str, Any]:
    if project_root is None:
        return {"kind": "unknown", "code_roots": ["src"], "test_roots": ["tests"], "workspace_roots": []}
    code_roots = [name for name in ("app", "src", "backend") if (project_root / name).exists()]
    test_roots = [name for name in ("tests", "test") if (project_root / name).exists()]
    workspace_roots: list[str] = []
    packages_dir = project_root / "packages"
    if packages_dir.exists():
        for item in packages_dir.iterdir():
            if not item.is_dir():
                continue
            workspace = f"packages/{item.name}"
            workspace_roots.append(workspace)
            for child in ("app", "src"):
                candidate = f"{workspace}/{child}"
                if (project_root / candidate).exists() and candidate not in code_roots:
                    code_roots.append(candidate)
            for child in ("tests", "test"):
                candidate = f"{workspace}/{child}"
                if (project_root / candidate).exists() and candidate not in test_roots:
                    test_roots.append(candidate)
    kind = "mixed"
    if workspace_roots:
        kind = "monorepo"
    elif len(code_roots) == 1:
        kind = code_roots[0]
    elif not code_roots:
        flat_node_entry = _flat_node_entry_file(project_root)
        if flat_node_entry:
            code_roots.append(flat_node_entry)
        kind = "flat"
    return {
        "kind": kind,
        "code_roots": code_roots or ["src"],
        "test_roots": test_roots or ["tests"],
        "workspace_roots": workspace_roots,
    }


def _flat_node_entry_file(project_root: Path) -> str:
    if not (project_root / "package.json").exists():
        return ""
    for candidate in FLAT_NODE_ENTRY_CANDIDATES:
        if (project_root / candidate).is_file():
            return candidate
    return ""


def detect_project_profile(project_root: Path | None, requested_profile: str = "auto") -> str:
    normalized = _clean_text(requested_profile, default="auto").lower()
    if normalized and normalized != "auto":
        return normalized
    if project_root is None:
        return "python"
    if (project_root / "package.json").exists():
        return "node"
    if (project_root / "manage.py").exists():
        return "django"
    for main_candidate in ("app/main.py", "backend/main.py", "src/main.py"):
        candidate_path = project_root / main_candidate
        if candidate_path.exists():
            main_text = candidate_path.read_text(encoding="utf-8", errors="ignore")
            if "FastAPI" in main_text:
                return "fastapi"
            if "Flask" in main_text:
                return "flask"
    return "python"


def _existing_paths(project_root: Path | None, candidates: list[str]) -> list[str]:
    if project_root is None:
        return []
    existing: list[str] = []
    for path in candidates:
        if (project_root / path).exists():
            existing.append(path)
    return existing


def _layer_candidates(layer: str, profile: str) -> dict[str, list[str]]:
    base = dict(LAYER_CORE_CANDIDATES.get(layer) or {})
    profiled = dict((PROFILE_LAYER_CANDIDATES.get(profile) or {}).get(layer) or {})
    return {
        "source": list(profiled.get("source") or base.get("source") or []),
        "tests": list(profiled.get("tests") or base.get("tests") or []),
    }


def _discover_layer_keyword_source(project_root: Path | None, layout: dict[str, Any], layer: str) -> str:
    if project_root is None:
        return ""
    keywords = {
        "repository": ("repository", "repo", "store", "client", "gateway"),
        "service": ("service", "usecase", "controller", "orchestrator"),
        "model": ("model", "entity", "store", "state"),
        "util": ("util", "utils", "helper"),
    }.get(layer)
    if not keywords:
        return ""
    roots: list[str] = []
    for raw in list(layout.get("code_roots") or []):
        normalized = _clean_text(raw).replace("\\", "/")
        if normalized and normalized not in roots:
            roots.append(normalized)
    for extra in ("app/src", "src", "backend", "backend/app", "web/src", "frontend/src"):
        if project_root is not None and (project_root / extra).exists() and extra not in roots:
            roots.append(extra)
    allowed_suffixes = {".py", ".ts", ".tsx", ".js", ".jsx"}
    matches: list[str] = []
    for root in roots:
        resolved_root = project_root / root
        if not resolved_root.exists() or not resolved_root.is_dir():
            continue
        for path in sorted(resolved_root.rglob("*"), key=lambda item: item.as_posix().lower()):
            if not path.is_file() or path.suffix.lower() not in allowed_suffixes:
                continue
            filename = path.name.lower()
            if filename.startswith("test_") or ".test." in filename or ".spec." in filename:
                continue
            if filename == "__init__.py" or filename.startswith("_"):
                continue
            stem = path.stem.lower()
            relative = path.relative_to(project_root).as_posix()
            relative_lower = relative.lower()
            stem_hit = any(keyword in stem for keyword in keywords)
            if not stem_hit:
                # Only match on exact directory segment names, not substrings.
                # e.g. keyword "service" must NOT match directory "services".
                dir_segments = relative_lower.split("/")[:-1]
                stem_hit = any(seg == keyword for seg in dir_segments for keyword in keywords)
            if stem_hit:
                matches.append(relative)
    return sorted(matches, key=lambda item: item.lower())[0] if matches else ""


def _existing_source_fallback(project_root: Path | None, layout: dict[str, Any], layer: str) -> str:
    if project_root is None:
        return ""
    keyword_source = _discover_layer_keyword_source(project_root, layout, layer)
    if keyword_source:
        return keyword_source
    fallback_candidates: list[str] = []
    if layer == "frontend":
        fallback_candidates.extend(["app/static/index.html", "mobile/www/index.html", "src/frontend/ui.ts"])
    for root in list(layout.get("code_roots") or []):
        fallback_candidates.extend(
            [
                f"{root}/main.py",
                f"{root}/routes.py",
                f"{root}/schemas.py",
                f"{root}/models.py",
            ]
        )
    existing = _existing_paths(project_root, fallback_candidates)
    return existing[0] if existing else ""


def _select_source_path(
    layer: str,
    project_root: Path | None,
    *,
    profile: str,
    layout: dict[str, Any],
) -> str:
    candidates = _layer_candidates(layer, profile).get("source", [])
    existing = _existing_paths(project_root, candidates)
    if existing:
        return existing[0]
    fallback = _existing_source_fallback(project_root, layout, layer)
    if fallback:
        return fallback
    preferred_root = _preferred_code_root(project_root, layout=layout)
    for path in candidates:
        if path.startswith(f"{preferred_root}/"):
            return path
    return candidates[0] if candidates else "src/app.py"


def _existing_test_fallback(project_root: Path | None, layout: dict[str, Any], source_path: str) -> str:
    if project_root is None:
        return ""
    stem = Path(source_path).stem
    fallback_candidates: list[str] = []
    for root in list(layout.get("test_roots") or []):
        fallback_candidates.extend(
            [
                f"{root}/test_{stem}.py",
                f"{root}/test_api.py",
                f"{root}/{stem}.test.ts",
                f"{root}/api.test.ts",
                f"{root}/{stem}.test.js",
                f"{root}/api.test.js",
            ]
        )
    existing = _existing_paths(project_root, fallback_candidates)
    return existing[0] if existing else ""


def _discover_existing_test_path(project_root: Path | None, layout: dict[str, Any]) -> str:
    if project_root is None:
        return ""
    allowed_suffixes = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs"}
    roots = [str(item).strip().replace("\\", "/") for item in list(layout.get("test_roots") or []) if str(item).strip()]
    if not roots and (project_root / "tests").exists():
        roots = ["tests"]
    for root in roots:
        resolved_root = project_root / root
        if not resolved_root.exists() or not resolved_root.is_dir():
            continue
        matches = sorted(
            path
            for path in resolved_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in allowed_suffixes
            and (
                path.name.lower().startswith("test")
                or ".test." in path.name.lower()
                or ".spec." in path.name.lower()
            )
        )
        if matches:
            return matches[0].relative_to(project_root).as_posix()
    return ""


def _select_test_path(
    layer: str,
    source_path: str,
    project_root: Path | None,
    *,
    profile: str,
) -> str:
    test_candidates = _layer_candidates(layer, profile).get("tests", [])
    existing_tests = _existing_paths(project_root, test_candidates)
    if existing_tests:
        return existing_tests[0]
    layout = detect_project_layout(project_root)
    if layer == "frontend":
        frontend_test = _discover_frontend_test_path(project_root, layout, source_path)
        if frontend_test:
            return frontend_test
    fallback = _existing_test_fallback(project_root, layout, source_path)
    if fallback:
        return fallback
    discovered = _discover_existing_test_path(project_root, layout)
    if discovered:
        return discovered
    default_test = f"tests/test_{Path(source_path).stem}.py"
    if project_root is not None and (project_root / "tests" / "test_api.py").exists():
        if layer in {"schema", "repository", "service", "route", "model"}:
            return "tests/test_api.py"
    return test_candidates[0] if test_candidates else default_test


def _task_name(layer: str) -> str:
    return {
        "schema": "Prepare schema contract",
        "repository": "Implement repository SoT access",
        "service": "Implement service business logic",
        "route": "Expose route contract",
        "frontend": "Integrate frontend contract",
        "model": "Adjust model mapping",
        "util": "Refine shared utility behavior",
    }.get(layer, f"Implement {layer} layer task")


def _invariants(layer: str, source_of_truth: list[str]) -> list[str]:
    invariants = [
        f"{layer} layer changes stay inside layer ownership boundaries.",
        "No second source of truth is introduced.",
    ]
    if source_of_truth:
        invariants.append(f"Read/write path stays consistent with declared SoT: {', '.join(source_of_truth)}.")
    return invariants[:5]


def _test_proof(layer: str) -> str:
    return f"Add/adjust {layer} unit tests and run scoped API/integration checks for this layer."


def _is_test_path(path: str, layout: dict[str, Any]) -> bool:
    normalized = _clean_text(path).replace("\\", "/")
    if normalized.startswith("tests/"):
        return True
    for root in list(layout.get("test_roots") or []):
        if normalized.startswith(f"{root}/"):
            return True
    name = Path(normalized).name
    return name.startswith("test_") or ".test." in name or ".spec." in name


def _frontend_root_candidates(project_root: Path | None, layout: dict[str, Any], source_path: str) -> list[str]:
    candidates: list[str] = []
    source = Path(source_path)
    parent = source.parent.as_posix()
    if len(source.parts) >= 2:
        candidates.append("/".join(source.parts[:2]))
    if parent and parent != ".":
        candidates.append(parent)
    for root in list(layout.get("code_roots") or []):
        normalized = _clean_text(root).replace("\\", "/")
        if normalized.startswith(("web/src", "frontend/src", "app/src", "src")):
            candidates.append(normalized)
    for extra in ("web/src", "frontend/src", "app/src", "src", "mobile/www"):
        if project_root is not None and (project_root / extra).exists():
            candidates.append(extra)
    deduped: list[str] = []
    for item in candidates:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _discover_frontend_test_path(project_root: Path | None, layout: dict[str, Any], source_path: str) -> str:
    if project_root is None:
        return ""
    source = Path(source_path)
    suffix = source.suffix or ".tsx"
    exact_candidates = [
        source.parent / f"{source.stem}.test{suffix}",
        source.parent / f"{source.stem}.spec{suffix}",
    ]
    for candidate in exact_candidates:
        if (project_root / candidate).exists():
            return candidate.as_posix()
    for root in _frontend_root_candidates(project_root, layout, source_path):
        resolved_root = project_root / root
        if not resolved_root.exists() or not resolved_root.is_dir():
            continue
        matches = sorted(
            path
            for path in resolved_root.rglob("*")
            if path.is_file() and (".test." in path.name or ".spec." in path.name)
        )
        if matches:
            return matches[0].relative_to(project_root).as_posix()
    return ""


def _task_executability(
    *,
    layer: str,
    core_files: list[str],
    project_root: Path | None,
    layout: dict[str, Any],
    planning_mode: str,
) -> dict[str, Any]:
    if project_root is None:
        return {
            "status": "WARN",
            "issues": ["project_root unavailable; executability check skipped."],
        }
    issues: list[str] = []
    missing_tests: list[str] = []
    has_any_test = False
    for path in core_files:
        resolved = project_root / path
        is_test = _is_test_path(path, layout)
        if resolved.exists():
            if is_test:
                has_any_test = True
            continue
        if is_test:
            missing_tests.append(f"{layer} test file not yet present: {path}")
            continue
        if planning_mode == "greenfield":
            continue
        issues.append(f"{layer} core file does not exist in project layout: {path}")
    if issues:
        return {"status": "FAIL", "issues": issues + missing_tests}
    if missing_tests and not has_any_test:
        return {"status": "WARN", "issues": missing_tests}
    return {"status": "PASS", "issues": []}


def _task_coverage_hints(
    *,
    layer: str,
    core_files: list[str],
    layout: dict[str, Any],
    path_type: str,
    source_of_truth_canonical: list[str],
) -> list[str]:
    hints = [f"layer:{layer}"]
    if any(not _is_test_path(path, layout) for path in core_files):
        hints.append(f"path:{_clean_text(path_type, default='read').lower()}")
        if layer in {"schema", "repository", "service", "model"}:
            hints.extend(f"sot:{item}" for item in source_of_truth_canonical if _clean_text(item))
    deduped: list[str] = []
    for item in hints:
        text = _clean_text(item).lower()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _boundary_severity(layers: list[str]) -> str:
    if {"route", "service", "repository"} <= set(layers):
        return "high"
    if {"service", "repository"} <= set(layers):
        return "medium"
    return "low"


def _boundary_recommended_split(layers: list[str]) -> list[str]:
    split: list[str] = []
    if "route" in layers:
        split.append("route: keep request/response binding in the current route file")
    if "service" in layers:
        split.append("service: extract business orchestration to a service module")
    if "repository" in layers:
        split.append("repository: extract data access to a repository module")
    return split


def _boundary_usage(tasks: list[dict[str, Any]], layout: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    usage: dict[str, dict[str, list[str]]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        task_id = _clean_text(task.get("task_id"))
        layer = _clean_text(task.get("layer_owner"))
        for raw in _string_list(task.get("core_files")):
            if _is_test_path(raw, layout):
                continue
            entry = usage.setdefault(raw, {"layers": [], "tasks": []})
            if layer and layer not in entry["layers"]:
                entry["layers"].append(layer)
            if task_id and task_id not in entry["tasks"]:
                entry["tasks"].append(task_id)
    return usage


def _boundary_item(file_path: str, payload: dict[str, list[str]]) -> dict[str, Any] | None:
    layers = sorted(payload["layers"])
    if len(layers) <= 1:
        return None
    return {
        "file": file_path,
        "layers": layers,
        "tasks": sorted(payload["tasks"]),
        "severity": _boundary_severity(layers),
        "owners": list(layers),
        "touched_in_feature": True,
        "recommended_split": _boundary_recommended_split(layers),
    }


def _boundary_debt(tasks: list[dict[str, Any]], layout: dict[str, Any]) -> dict[str, Any]:
    usage = _boundary_usage(tasks, layout)
    items: list[dict[str, Any]] = []
    for file_path, payload in sorted(usage.items()):
        item = _boundary_item(file_path, payload)
        if item is not None:
            items.append(item)
    return {
        "status": "WARN" if items else "PASS",
        "details": (
            f"Multiple logical layers map to the same physical source file ({len(items)} shared file(s))."
            if items
            else "Logical layer ownership maps cleanly to distinct source files."
        ),
        "items": items,
    }


def _surface_for_layer(layer: str, *, surfaces: list[str]) -> str:
    if layer == "frontend":
        if "frontend" in surfaces:
            return "frontend"
        if "mobile_wrapper" in surfaces:
            return "mobile_wrapper"
    if layer == "util" and "scripts_deploy" in surfaces:
        return "scripts_deploy"
    if "backend" in surfaces:
        return "backend"
    return surfaces[0] if surfaces else "backend"


def _resolved_graph_context(
    *,
    prd_intake: dict[str, Any],
    project_root: Path | None,
    project_profile: str,
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
) -> dict[str, Any]:
    resolved_archetype = _clean_text(architecture_plan.get("archetype") or repo_inventory.get("archetype"))
    capabilities = _string_list(architecture_plan.get("capabilities") or repo_inventory.get("capabilities"))
    layers = _string_list(architecture_plan.get("recommended_layers"))
    if not layers:
        layers = derive_task_layers(
            archetype=resolved_archetype or "fastapi_api",
            capabilities=capabilities,
            prd_layers=_string_list(prd_intake.get("layers")),
        )
    layout = dict(repo_inventory.get("project_layout") or detect_project_layout(project_root))
    resolved_profile = profile_from_archetype(resolved_archetype) if resolved_archetype else detect_project_profile(project_root, requested_profile=project_profile)
    surface_payloads = [dict(item) for item in list(architecture_plan.get("surfaces") or repo_inventory.get("surfaces") or []) if isinstance(item, dict)]
    surface_names = [str(item.get("name") or "").strip() for item in surface_payloads if str(item.get("name") or "").strip()]
    return {
        "archetype": resolved_archetype,
        "capabilities": capabilities,
        "layers": layers or _ordered_layers(prd_intake),
        "layout": layout,
        "project_profile": resolved_profile,
        "surface_names": surface_names,
    }


def _build_task_entry(
    *,
    layer: str,
    index: int,
    project_root: Path | None,
    resolved_profile: str,
    layout: dict[str, Any],
    planning_mode: str,
    surface_names: list[str],
    source_of_truth: list[str],
    source_of_truth_canonical: list[str],
    path_type: str,
) -> tuple[dict[str, Any], list[str]]:
    task_id = f"T{index}"
    source_path = _select_source_path(layer, project_root, profile=resolved_profile, layout=layout)
    test_path = _select_test_path(layer, source_path, project_root, profile=resolved_profile)
    core_files = [item for item in [source_path, test_path] if item]
    deduped_core_files = list(dict.fromkeys(core_files))
    core_capped = deduped_core_files[:3]
    executability = _task_executability(
        layer=layer,
        core_files=deduped_core_files,
        project_root=project_root,
        layout=layout,
        planning_mode=planning_mode,
    )
    issues = []
    if str(executability.get("status") or "").upper() == "FAIL":
        issues = [f"{task_id}: {issue}" for issue in list(executability.get("issues") or []) if str(issue).strip()]
    # Declare any core_files entry that does not exist on disk as a new_file so
    # downstream preflight does not refuse the task. Only honor this when a
    # project_root is available; otherwise leave new_files empty (consumers will
    # warn rather than fail).
    new_files = _infer_new_files(core_capped, project_root)
    task = {
        "task_id": task_id,
        "task_name": _task_name(layer),
        "depends_on": [f"T{index - 1}"] if index > 1 else [],
        "core_files": core_capped,
        "new_files": new_files,
        "layer_owner": layer,
        "surface": _surface_for_layer(layer, surfaces=surface_names),
        "invariants": _invariants(layer, source_of_truth),
        "test_proof": _test_proof(layer),
        "coverage_hints": _task_coverage_hints(
            layer=layer,
            core_files=core_capped,
            layout=layout,
            path_type=path_type,
            source_of_truth_canonical=source_of_truth_canonical,
        ),
        "executability": executability,
    }
    return task, issues


def _infer_new_files(core_files: list[str], project_root: Path | None) -> list[str]:
    """Return the subset of core_files that do not exist at project_root."""
    if project_root is None or not core_files:
        return []
    missing: list[str] = []
    for rel in core_files:
        candidate = project_root / rel
        if not candidate.exists():
            missing.append(rel)
    return missing


def _graph_source_context(prd_intake: dict[str, Any]) -> tuple[list[str], list[str], str]:
    source_of_truth = _string_list(prd_intake.get("source_of_truth"))
    source_of_truth_canonical = _string_list(prd_intake.get("source_of_truth_canonical")) or canonicalize_source_of_truth(source_of_truth)
    path_type = _clean_text(prd_intake.get("path_type"), default="read").lower()
    return source_of_truth, source_of_truth_canonical, path_type


def _collect_graph_tasks(
    *,
    layers: list[str],
    project_root: Path | None,
    context: dict[str, Any],
    planning_mode: str,
    source_of_truth: list[str],
    source_of_truth_canonical: list[str],
    path_type: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    tasks: list[dict[str, Any]] = []
    executability_issues: list[str] = []
    for index, layer in enumerate(layers, start=1):
        task, issues = _build_task_entry(
            layer=layer,
            index=index,
            project_root=project_root,
            resolved_profile=str(context["project_profile"]),
            layout=dict(context["layout"]),
            planning_mode=planning_mode,
            surface_names=list(context["surface_names"]),
            source_of_truth=source_of_truth,
            source_of_truth_canonical=source_of_truth_canonical,
            path_type=path_type,
        )
        tasks.append(task)
        executability_issues.extend(issues)
    return tasks, executability_issues


def _graph_payload(
    *,
    prd_intake: dict[str, Any],
    plan: dict[str, Any],
    planning_mode: str,
    context: dict[str, Any],
    path_type: str,
    source_of_truth_canonical: list[str],
    boundary_debt: dict[str, Any],
    tasks: list[dict[str, Any]],
    executability_issues: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "business_outcome": _clean_text(prd_intake.get("business_outcome")),
        "planning_mode": _clean_text(plan.get("planning_mode"), default=planning_mode),
        "semantic_source": _clean_text(plan.get("semantic_source"), default="task_graph.heuristic"),
        "archetype": str(context["archetype"]),
        "capabilities": list(context["capabilities"]),
        "surfaces": list(context["surface_names"]),
        "project_layout": dict(context["layout"]),
        "project_profile": str(context["project_profile"]),
        "coverage_hints": build_contract_coverage_hints(
            layers=list(context["layers"]),
            path_type=path_type,
            source_of_truth_canonical=source_of_truth_canonical,
        ),
        "boundary_debt": boundary_debt,
        "tasks": tasks,
        "executability": {
            "status": "FAIL" if executability_issues else "PASS",
            "issues": executability_issues,
        },
    }


def build_task_graph(
    prd_intake: dict[str, Any],
    *,
    project_root: Path | None = None,
    project_profile: str = "auto",
    repo_inventory: dict[str, Any] | None = None,
    architecture_plan: dict[str, Any] | None = None,
    planning_mode: str = "existing",
) -> dict[str, Any]:
    inventory = dict(repo_inventory or {})
    plan = dict(architecture_plan or {})
    context = _resolved_graph_context(
        prd_intake=prd_intake,
        project_root=project_root,
        project_profile=project_profile,
        repo_inventory=inventory,
        architecture_plan=plan,
    )
    source_of_truth, source_of_truth_canonical, path_type = _graph_source_context(prd_intake)
    tasks, executability_issues = _collect_graph_tasks(
        layers=list(context["layers"]),
        project_root=project_root,
        context=context,
        planning_mode=planning_mode,
        source_of_truth=source_of_truth,
        source_of_truth_canonical=source_of_truth_canonical,
        path_type=path_type,
    )
    boundary_debt = _boundary_debt(tasks, dict(context["layout"]))
    return _graph_payload(
        prd_intake=prd_intake,
        plan=plan,
        planning_mode=planning_mode,
        context=context,
        path_type=path_type,
        source_of_truth_canonical=source_of_truth_canonical,
        boundary_debt=boundary_debt,
        tasks=tasks,
        executability_issues=executability_issues,
    )


def validate_task_graph(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return ["tasks must be a non-empty list"]
    executability = dict(payload.get("executability") or {})
    if str(executability.get("status") or "").upper() == "FAIL":
        errors.extend([str(item) for item in list(executability.get("issues") or []) if str(item).strip()])
    for idx, item in enumerate(tasks, start=1):
        if not isinstance(item, dict):
            errors.append(f"tasks[{idx}] must be an object")
            continue
        for key in ("task_id", "task_name", "core_files", "layer_owner", "invariants", "test_proof"):
            if key not in item:
                errors.append(f"tasks[{idx}] missing required field: {key}")
        core_files = _string_list(item.get("core_files"))
        if not core_files and not verification_only_allows_empty_files(payload, item):
            errors.append(f"tasks[{idx}].core_files must be non-empty")
        if len(core_files) > 3:
            errors.append(f"tasks[{idx}].core_files exceeds 3 items")
        task_exec = dict(item.get("executability") or {})
        if str(task_exec.get("status") or "").upper() == "FAIL":
            errors.extend([str(task_issue) for task_issue in list(task_exec.get("issues") or []) if str(task_issue).strip()])
    return errors


# Markdown rendering is in task_graph_render.py (split to stay under 1000-line redline).
# Re-exported here so all existing callers remain unaffected.
from kodawari.autopilot.planning.task_graph_render import render_task_graph_markdown  # noqa: F401


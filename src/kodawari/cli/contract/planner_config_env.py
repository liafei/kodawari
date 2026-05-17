"""PlanningConfig construction from environment and model defaults."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from kodawari.autopilot.core.model_config import load_model_config
from kodawari.autopilot.planning.context_scout import recommend_scout_budget
from kodawari.autopilot.planning.planning_orchestrator import (
    DEFAULT_BLOCKING_SEVERITIES,
    PlanningConfig,
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def env_text(name: str, default: str = "") -> str:
    return _clean_text(os.environ.get(name, default))


def env_present(name: str) -> bool:
    return os.environ.get(name) is not None


def _env_any(names: tuple[str, ...]) -> bool:
    return any(env_present(name) for name in names)


def _normalized_driver(value: str) -> str:
    return _clean_text(value).lower().replace("-", "_")


def _driver_env_differs(name: str, default: str) -> bool:
    if not env_present(name):
        return False
    configured = _normalized_driver(env_text(name))
    if not configured:
        return False
    return configured != _normalized_driver(default)


def _env_or_role_model(env_name: str, default: str, *, clear_default: bool = False) -> str:
    if env_present(env_name):
        return env_text(env_name)
    return "" if clear_default else default


def _transport_default(value: str, env_name: str, *, clear_default: bool = False) -> str:
    if env_present(env_name):
        return env_text(env_name)
    return "" if clear_default else value


def env_int(name: str, default: int) -> int:
    raw = env_text(name)
    if not raw:
        return int(default)
    try:
        return max(1, int(raw))
    except ValueError:
        return int(default)


def env_optional_int(name: str) -> int | None:
    raw = env_text(name)
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_text(name).lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "y", "on"}


def env_blocking_severities() -> frozenset[str] | None:
    """Parse comma-separated planning severities from the active environment."""
    raw = env_text("WORKFLOW_PLAN_BLOCKING_SEVERITIES").strip()
    if not raw:
        return None
    parts = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return frozenset(parts) if parts else None


_MAX_ROUND_FILE_SCAN_LIMIT = 80
_MAX_ROUND_SKIP_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".workflow_runtime",
    "planning",
})


def planning_candidate_roots(project_root: Path, repo_inventory: dict[str, Any] | None) -> list[Path]:
    payload = dict(repo_inventory or {})
    layout = dict(payload.get("project_layout") or {})
    roots: list[str] = []
    for key in ("code_roots", "test_roots", "workspace_roots"):
        roots.extend(_string_list(layout.get(key)))
    for surface in list(payload.get("surfaces") or []):
        if not isinstance(surface, dict):
            continue
        roots.extend(_string_list(surface.get("roots")))
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        candidate = (project_root / root).resolve()
        if not candidate.exists():
            continue
        normalized = str(candidate).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    if deduped:
        return deduped
    return [project_root.resolve()]


def planning_candidate_files(project_root: Path, repo_inventory: dict[str, Any] | None) -> list[str]:
    project_root = project_root.resolve()
    collected: list[str] = []
    seen: set[str] = set()
    for root in planning_candidate_roots(project_root, repo_inventory):
        if root.is_file():
            try:
                relative = root.relative_to(project_root)
            except ValueError:
                continue
            normalized = str(relative).replace("\\", "/")
            if normalized and normalized not in seen:
                seen.add(normalized)
                collected.append(normalized)
            if len(collected) >= _MAX_ROUND_FILE_SCAN_LIMIT:
                return collected
            continue
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _MAX_ROUND_SKIP_DIRS]
            for filename in filenames:
                candidate = (Path(current) / filename).resolve()
                try:
                    relative = candidate.relative_to(project_root)
                except ValueError:
                    continue
                normalized = str(relative).replace("\\", "/")
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                collected.append(normalized)
                if len(collected) >= _MAX_ROUND_FILE_SCAN_LIMIT:
                    return collected
    return collected


def planning_candidate_line_counts(project_root: Path, candidate_files: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    root = project_root.resolve()
    for relative in candidate_files:
        path = (root / relative).resolve()
        if not path.exists() or not path.is_file():
            continue
        try:
            counts[relative] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            counts[relative] = 0
    return counts


def suggest_max_rounds(
    *,
    project_root: Path | None,
    task_direction: str,
    repo_inventory: dict[str, Any] | None,
) -> int:
    root = (project_root or Path.cwd()).resolve()
    if not _clean_text(task_direction):
        return 3
    candidate_files = planning_candidate_files(root, repo_inventory)
    if not candidate_files:
        return 3
    line_counts = planning_candidate_line_counts(root, candidate_files)
    recommendation = recommend_scout_budget(
        user_text=task_direction,
        candidate_files=candidate_files,
        file_line_counts=line_counts,
        files_estimate_override=len(candidate_files),
    )
    tier = str(recommendation.tier or "").strip().lower()
    if tier == "quick":
        return 3
    if tier == "standard":
        return 5
    return 7


def _plan_reviewer_defaults(models: Any) -> tuple[str, str, str, Any]:
    explicit = models.get_role("plan_reviewer", fallback=False)
    if explicit is not None:
        transport = models.transports.get(explicit.transport)
        return (
            explicit.model,
            transport.primary_executable() if transport else "codex",
            transport.driver if transport else "codex_cli",
            transport,
        )
    legacy = models.get_role("reviewer", fallback=False)
    if legacy is not None:
        transport = models.transports.get(legacy.transport)
        if transport is not None and transport.kind == "subprocess" and transport.interface in {"agent", "tool_use"}:
            return legacy.model, transport.primary_executable() or "codex", transport.driver or "codex_cli", transport
    planner = models.get_role("planner", fallback=False)
    planner_model = planner.model if planner is not None else models.planner_model
    for transport in models.transports.values():
        if transport.kind == "subprocess" and transport.interface == "agent":
            return planner_model, transport.primary_executable() or "codex", transport.driver or "codex_cli", transport
    return models.plan_reviewer_model or models.reviewer_model, "codex", "codex_cli", None


def planning_config_from_env(
    project_root: Path | None = None,
    *,
    task_direction: str = "",
    repo_inventory: dict[str, Any] | None = None,
    suggest_max_rounds_fn: Callable[..., int] = suggest_max_rounds,
    env_blocking_severities_fn: Callable[[], frozenset[str] | None] = env_blocking_severities,
) -> PlanningConfig:
    models = load_model_config(project_root or Path.cwd())
    planner_model_default = models.planner_model
    planner_executable_default = models.role_executable("planner") or "claude"
    planner_driver_default = models.role_driver("planner") or "claude_cli"
    planner_transport = models.transport_for_role("planner", fallback=False)
    planner_base_url_default = ""
    planner_api_key_env_default = ""
    planner_api_format_default = ""
    if planner_transport is not None:
        planner_base_url_default = planner_transport.base_url
        if planner_transport.base_url_env and not planner_base_url_default:
            planner_base_url_default = env_text(planner_transport.base_url_env)
        planner_api_key_env_default = planner_transport.api_key_env
        planner_api_format_default = planner_transport.api_format
    reviewer_model_default, reviewer_executable_default, reviewer_driver_default, plan_reviewer_transport = _plan_reviewer_defaults(models)
    planner_transport_for_config = planner_transport
    plan_reviewer_transport_for_config = plan_reviewer_transport
    planner_transport_overridden = _env_any(
        (
            "WORKFLOW_PLANNER_DRIVER",
            "WORKFLOW_PLANNER_EXECUTABLE",
            "WORKFLOW_PLANNER_BASE_URL",
            "WORKFLOW_PLANNER_API_KEY_ENV",
            "WORKFLOW_PLANNER_API_FORMAT",
        )
    )
    plan_reviewer_transport_overridden = _env_any(
        ("WORKFLOW_PLAN_REVIEWER_DRIVER", "WORKFLOW_PLAN_REVIEWER_EXECUTABLE")
    )
    planner_cross_driver_override = _driver_env_differs("WORKFLOW_PLANNER_DRIVER", planner_driver_default)
    plan_reviewer_cross_driver_override = _driver_env_differs("WORKFLOW_PLAN_REVIEWER_DRIVER", reviewer_driver_default)
    if planner_transport_overridden:
        planner_transport_for_config = None
    if plan_reviewer_transport_overridden:
        plan_reviewer_transport_for_config = None
    severities = env_blocking_severities_fn()
    max_rounds = env_optional_int("WORKFLOW_PLANNING_MAX_ROUNDS")
    if max_rounds is None:
        max_rounds = suggest_max_rounds_fn(
            project_root=project_root,
            task_direction=task_direction,
            repo_inventory=repo_inventory,
        )
    return PlanningConfig(
        planner_executable=env_text("WORKFLOW_PLANNER_EXECUTABLE", planner_executable_default) or "claude",
        reviewer_executable=env_text("WORKFLOW_PLAN_REVIEWER_EXECUTABLE", reviewer_executable_default) or "codex",
        planner_transport=planner_transport_for_config,
        plan_reviewer_transport=plan_reviewer_transport_for_config,
        planner_driver=env_text("WORKFLOW_PLANNER_DRIVER", planner_driver_default) or planner_driver_default,
        reviewer_driver=env_text("WORKFLOW_PLAN_REVIEWER_DRIVER", reviewer_driver_default) or reviewer_driver_default,
        planner_base_url=_transport_default(
            planner_base_url_default,
            "WORKFLOW_PLANNER_BASE_URL",
            clear_default=planner_cross_driver_override,
        ),
        planner_api_key_env=_transport_default(
            planner_api_key_env_default,
            "WORKFLOW_PLANNER_API_KEY_ENV",
            clear_default=planner_cross_driver_override,
        ),
        planner_api_format=_transport_default(
            planner_api_format_default,
            "WORKFLOW_PLANNER_API_FORMAT",
            clear_default=planner_cross_driver_override,
        ),
        planner_context_max_chars=env_int("WORKFLOW_PLANNER_CONTEXT_MAX_CHARS", 0),
        planner_timeout_seconds=env_int("WORKFLOW_PLANNER_TIMEOUT", 600),
        reviewer_timeout_seconds=env_int("WORKFLOW_PLAN_REVIEWER_TIMEOUT", 180),
        planner_model=_env_or_role_model(
            "WORKFLOW_PLANNER_MODEL",
            planner_model_default,
            clear_default=planner_cross_driver_override,
        ),
        reviewer_model=_env_or_role_model(
            "WORKFLOW_PLAN_REVIEWER_MODEL",
            reviewer_model_default,
            clear_default=plan_reviewer_cross_driver_override,
        ),
        max_rounds=max_rounds,
        deadlock_streak_limit=env_int("WORKFLOW_PLANNING_DEADLOCK_STREAK_LIMIT", 2),
        auto_approve_enabled=not env_bool("WORKFLOW_REQUIRE_PLANNING_APPROVAL", False),
        blocking_severities=severities if severities is not None else DEFAULT_BLOCKING_SEVERITIES,
        decision_policy=env_text("WORKFLOW_PLAN_DECISION_POLICY", "strict-gate") or "strict-gate",
        task_splitter_enabled=env_bool("WORKFLOW_TASK_SPLITTER", False),
    )


__all__ = [
    "env_blocking_severities",
    "planning_config_from_env",
    "suggest_max_rounds",
]

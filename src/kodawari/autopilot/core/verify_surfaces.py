"""Multi-surface verify planning helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.autopilot.core.runtime_checks import build_verify_check
from kodawari.infra.contract_first_schema import load_contract_first_artifact


class VerifySurfacePlanningError(ValueError):
    """Raised when verify surface planning cannot produce deterministic targets."""

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = str(error_code)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_paths(values: list[Any] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        text = _clean_text(raw).replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _load_optional_contract(planning_dir: Path, name: str, schema_name: str) -> dict[str, Any] | None:
    path = (planning_dir / name).resolve()
    if not path.exists():
        return None
    return load_contract_first_artifact(path, schema_name=schema_name)


def load_verify_surface_context(planning_dir: Path) -> dict[str, Any]:
    conversation = (
        _load_optional_contract(planning_dir, "PLANNING_CONVERSATION.json", "planning_conversation")
        or {}
    )
    architecture = _load_optional_contract(planning_dir, "ARCHITECTURE_PLAN.json", "architecture_plan") or {}
    if not architecture and conversation:
        architecture = {
            "verify_recipes": list(conversation.get("verify_recipes") or []),
        }
    return {
        "repo_inventory": _load_optional_contract(planning_dir, "REPO_INVENTORY.json", "repo_inventory") or {},
        "architecture_plan": architecture,
        "planning_conversation": conversation,
    }


def build_verify_surface_plan(
    *,
    planning_dir: Path,
    requested_command: str,
    requested_command_kind: str,
    changed_files: list[str],
    task_card_files: list[str],
    task_surface: str = "",
) -> dict[str, Any] | None:
    explicit_plan = _explicit_surface_plan(
        requested_command=requested_command,
        requested_command_kind=requested_command_kind,
        changed_files=changed_files,
        task_surface=task_surface,
    )
    if explicit_plan is not None:
        return explicit_plan
    context = load_verify_surface_context(planning_dir)
    repo_inventory = dict(context["repo_inventory"])
    architecture_plan = dict(context["architecture_plan"])
    if not repo_inventory and not architecture_plan:
        return None
    selected, selection_source = _select_surfaces(
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        allow_architecture_fallback=bool(context.get("planning_conversation")),
        changed_files=changed_files,
        task_card_files=task_card_files,
        task_surface=task_surface,
    )
    planned_surfaces = _planned_surfaces(
        selected=selected,
        repo_inventory=repo_inventory,
        architecture_plan=architecture_plan,
        changed_files=changed_files,
    )
    return {
        "verify_scope_mode": "surface_plan",
        "surface_results": planned_surfaces,
        "surface_summary": {
            "selection_source": selection_source,
            "required_surfaces": [item["surface"] for item in planned_surfaces],
            "available_surfaces": _available_surface_names(repo_inventory),
        },
    }


def _explicit_surface_plan(
    *,
    requested_command: str,
    requested_command_kind: str,
    changed_files: list[str],
    task_surface: str = "",
) -> dict[str, Any] | None:
    if requested_command_kind not in {"file", "inline"}:
        return None
    surface_name = _clean_text(task_surface) or "custom"
    scope_mode = "surface_plan" if surface_name != "custom" else "custom"
    return {
        "verify_scope_mode": scope_mode,
        "surface_results": [
            {
                "surface": surface_name,
                "verify_cmd": requested_command,
                "command_source": "explicit",
                "roots": [],
                "changed_files": list(changed_files),
                "required": True,
            }
        ],
        "surface_summary": {
            "selection_source": "explicit_command",
            "required_surfaces": [surface_name],
            "available_surfaces": [surface_name],
        },
    }


def _select_surfaces(
    *,
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
    allow_architecture_fallback: bool = False,
    changed_files: list[str],
    task_card_files: list[str],
    task_surface: str = "",
) -> tuple[list[str], str]:
    selected = surface_coverage_for_files(changed_files, repo_inventory=repo_inventory)
    if selected:
        return selected, "changed_files"
    selected = surface_coverage_for_files(task_card_files, repo_inventory=repo_inventory)
    if selected:
        return selected, "task_card_files"
    available = _available_surface_names(repo_inventory)
    if len(available) == 1:
        return available, "single_surface_repo"
    required_surfaces = [
        _clean_text(item.get("surface"))
        for item in list(architecture_plan.get("verify_recipes") or [])
        if isinstance(item, dict) and bool(item.get("required")) and _clean_text(item.get("surface"))
    ]
    deduped_required: list[str] = []
    for surface in required_surfaces:
        if surface not in deduped_required:
            deduped_required.append(surface)
    if allow_architecture_fallback and deduped_required:
        return deduped_required, "architecture_verify_recipes"
    hint = _clean_text(task_surface)
    if hint and hint in available:
        return [hint], "task_card_surface"
    raise VerifySurfacePlanningError(
        error_code="verify_surface_ambiguous",
        message="verify surface mapping is ambiguous; provide --command-file/--command or add architecture verify recipes.",
    )


def _planned_surfaces(
    *,
    selected: list[str],
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
    changed_files: list[str],
) -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for surface in selected:
        verify_cmd, command_source = _surface_command(
            surface=surface,
            repo_inventory=repo_inventory,
            architecture_plan=architecture_plan,
        )
        planned.append(
            {
                "surface": surface,
                "verify_cmd": verify_cmd,
                "command_source": command_source,
                "roots": _surface_roots(repo_inventory, surface),
                "changed_files": _surface_changed_files(changed_files, repo_inventory, surface),
                "required": True,
            }
        )
    return planned


def _surface_command(
    *,
    surface: str,
    repo_inventory: dict[str, Any],
    architecture_plan: dict[str, Any],
) -> tuple[str, str]:
    command = _architecture_verify_command(surface=surface, architecture_plan=architecture_plan)
    if command:
        return command, "ARCHITECTURE_PLAN.json.verify_recipes"
    command = _inventory_verify_command(surface=surface, repo_inventory=repo_inventory)
    if command:
        return command, "REPO_INVENTORY.json.verify_surfaces"
    raise VerifySurfacePlanningError(
        error_code="verify_recipe_missing",
        message=f"surface '{surface}' has no deterministic verify recipe.",
    )


def _architecture_verify_command(*, surface: str, architecture_plan: dict[str, Any]) -> str:
    recipes = list(architecture_plan.get("verify_recipes") or [])
    for item in recipes:
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("surface")) != surface:
            continue
        command = _clean_text(item.get("command"))
        if command:
            return command
    return ""


def _inventory_verify_command(*, surface: str, repo_inventory: dict[str, Any]) -> str:
    surfaces = list(repo_inventory.get("verify_surfaces") or [])
    for item in surfaces:
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("name")) != surface:
            continue
        command = _clean_text(item.get("verify_command"))
        if command:
            return command
    return ""


def _available_surface_names(repo_inventory: dict[str, Any]) -> list[str]:
    surfaces = list(repo_inventory.get("verify_surfaces") or repo_inventory.get("surfaces") or [])
    names: list[str] = []
    for item in surfaces:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"))
        if name and name not in names:
            names.append(name)
    return names


def _surface_roots(repo_inventory: dict[str, Any], surface: str) -> list[str]:
    surfaces = list(repo_inventory.get("surfaces") or [])
    for item in surfaces:
        if not isinstance(item, dict):
            continue
        if _clean_text(item.get("name")) != surface:
            continue
        return _normalize_paths(list(item.get("roots") or []))
    return []


def _surface_changed_files(
    changed_files: list[str],
    repo_inventory: dict[str, Any],
    surface: str,
) -> list[str]:
    roots = _surface_roots(repo_inventory, surface)
    if not roots:
        return _normalize_paths(changed_files)
    matched = [
        path
        for path in _normalize_paths(changed_files)
        if any(path == root or path.startswith(f"{root}/") for root in roots)
    ]
    return matched or _normalize_paths(changed_files)


def surface_coverage_for_files(
    changed_files: list[str],
    *,
    repo_inventory: dict[str, Any],
) -> list[str]:
    normalized = _normalize_paths(changed_files)
    surfaces = list(repo_inventory.get("surfaces") or [])
    matched: list[str] = []
    for path in normalized:
        for name in _best_surface_matches_for_path(path, surfaces):
            if name not in matched:
                matched.append(name)
    return matched


def _best_surface_matches_for_path(path: str, surfaces: list[Any]) -> list[str]:
    matches = _matching_surfaces_for_path(path, surfaces)
    if not matches:
        return []
    best_score = max(item["score"] for item in matches)
    return [str(item["name"]) for item in matches if item["score"] == best_score]


def _matching_surfaces_for_path(path: str, surfaces: list[Any]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in surfaces:
        if not isinstance(item, dict):
            continue
        name = _clean_text(item.get("name"))
        roots = _normalize_paths(list(item.get("roots") or []))
        if not name or not roots:
            continue
        for root in roots:
            if path == root or path.startswith(f"{root}/"):
                matches.append({"name": name, "score": _root_specificity(root)})
                break
    return matches


def _root_specificity(root: str) -> tuple[int, int]:
    normalized = _clean_text(root).replace("\\", "/").strip("/")
    if not normalized:
        return (0, 0)
    return (len(normalized.split("/")), len(normalized))


def execute_verify_surface_plan(
    *,
    project_root: Path,
    feature: str,
    task_label: str,
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    planned_surfaces = [dict(item) for item in list(plan.get("surface_results") or []) if isinstance(item, dict)]
    results = [
        _execute_surface(
            project_root=project_root,
            feature=feature,
            task_label=task_label,
            surface=item,
        )
        for item in planned_surfaces
    ]
    return results, aggregate_surface_results(results)


def _execute_surface(
    *,
    project_root: Path,
    feature: str,
    task_label: str,
    surface: dict[str, Any],
) -> dict[str, Any]:
    payload = build_verify_check(
        project_root=project_root,
        feature=feature,
        task_label=task_label,
        verify_cmd=_clean_text(surface.get("verify_cmd")),
        changed_files=_normalize_paths(surface.get("changed_files")),
        qa_payload=None,
    )
    if _is_broad_default_fallback(payload):
        payload = _blocked_surface_payload(surface=surface)
    payload["surface"] = _clean_text(surface.get("surface"))
    payload["command_source"] = _clean_text(surface.get("command_source"))
    payload["roots"] = _normalize_paths(surface.get("roots"))
    payload["changed_files"] = _normalize_paths(surface.get("changed_files"))
    payload["required"] = bool(surface.get("required", True))
    return payload


def _is_broad_default_fallback(payload: dict[str, Any]) -> bool:
    verify_cmd = _clean_text(payload.get("verify_cmd"))
    resolved = _clean_text(payload.get("verify_cmd_resolved"))
    source = _clean_text(payload.get("verify_target_source"))
    if verify_cmd != "pytest -q":
        return False
    if resolved != "pytest -q":
        return False
    return source == "default" and not bool(payload.get("command_executed"))


def _blocked_surface_payload(*, surface: dict[str, Any]) -> dict[str, Any]:
    verify_cmd = _clean_text(surface.get("verify_cmd"))
    name = _clean_text(surface.get("surface"),) or "unknown"
    return {
        "status": "BLOCKED",
        "passed": False,
        "mode": "surface_planner",
        "source": "verify_surface_planner",
        "verify_cmd": verify_cmd,
        "verify_cmd_resolved": verify_cmd,
        "verify_target_source": "surface_default_ambiguous",
        "verify_targets": [],
        "summary": f"surface '{name}' requires a more deterministic verify target",
        "blocking_reason": (
            f"surface '{name}' resolved to a broad default verify command; add deterministic "
            "tests, architecture verify recipes, or use --command-file/--command"
        ),
        "command_executed": False,
        "artifacts": [],
        "returncode": None,
        "stdout_excerpt": "",
        "stderr_excerpt": "",
    }


def aggregate_surface_results(surface_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not surface_results:
        return {
            "status": "UNKNOWN",
            "passed": False,
            "mode": "surface_plan",
            "source": "verify_surface_plan",
            "verify_cmd": "",
            "verify_cmd_resolved": "",
            "verify_target_source": "surface_plan",
            "verify_targets": [],
            "summary": "verify surface plan is empty",
            "blocking_reason": "verify surface plan is empty",
            "command_executed": False,
            "artifacts": [],
            "returncode": None,
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "covered_surfaces": [],
        }
    failing = next((item for item in surface_results if _clean_text(item.get("status")) != "PASS"), None)
    status = "PASS" if failing is None else _clean_text(failing.get("status"),) or "BLOCKED"
    return {
        "status": status,
        "passed": status == "PASS",
        "mode": "surface_plan",
        "source": "verify_surface_plan",
        "verify_cmd": "surface_plan",
        "verify_cmd_resolved": "surface_plan",
        "verify_target_source": "surface_planner",
        "verify_targets": [_clean_text(item.get("surface")) for item in surface_results if _clean_text(item.get("surface"))],
        "summary": _surface_summary(surface_results, failing=failing),
        "blocking_reason": "" if failing is None else _clean_text(failing.get("blocking_reason")),
        "command_executed": all(bool(item.get("command_executed")) for item in surface_results),
        "artifacts": _aggregate_artifacts(surface_results),
        "returncode": 0 if failing is None else failing.get("returncode"),
        "stdout_excerpt": "" if failing is None else _clean_text(failing.get("stdout_excerpt")),
        "stderr_excerpt": "" if failing is None else _clean_text(failing.get("stderr_excerpt")),
        "covered_surfaces": [_clean_text(item.get("surface")) for item in surface_results if _clean_text(item.get("surface"))],
    }


def _surface_summary(surface_results: list[dict[str, Any]], *, failing: dict[str, Any] | None) -> str:
    names = [_clean_text(item.get("surface")) for item in surface_results if _clean_text(item.get("surface"))]
    if failing is None:
        return f"verify=PASS; surfaces={names}"
    failing_name = _clean_text(failing.get("surface")) or "unknown"
    return f"verify=BLOCKED; surface={failing_name}; { _clean_text(failing.get('summary')) }".strip()


def _aggregate_artifacts(surface_results: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in surface_results:
        values.extend(_normalize_paths(item.get("artifacts")))
    return _normalize_paths(values)


__all__ = [
    "VerifySurfacePlanningError",
    "aggregate_surface_results",
    "build_verify_surface_plan",
    "execute_verify_surface_plan",
    "load_verify_surface_context",
    "surface_coverage_for_files",
]


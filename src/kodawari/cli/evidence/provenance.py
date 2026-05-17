"""Shared provenance helpers for kodawari CLI entrypoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def looks_like_kodawari_repo(candidate: Path) -> bool:
    return (candidate / "pyproject.toml").exists() and (candidate / "src" / "kodawari" / "cli" / "main.py").exists()


def find_kodawari_repo_root(start: Path) -> Path | None:
    try:
        resolved = start.resolve()
    except OSError:
        return None
    for candidate in (resolved, *resolved.parents):
        if looks_like_kodawari_repo(candidate):
            return candidate
    return None


def stringify_path(value: Path | None) -> str | None:
    if value is None:
        return None
    return str(value)


def repo_alignment(left: Path | None, right: Path | None) -> str:
    if left is None or right is None:
        return "unknown"
    return "match" if left == right else "mismatch"


def same_resolved_path(left: str | None, right: str | None) -> bool | None:
    if not left or not right:
        return None
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return None


def resolved_wrapper_repo_root() -> Path | None:
    raw = os.environ.get("WORKFLOWCTL_REPO_ROOT")
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def repo_from_planning_dir(planning_dir: Path | None) -> Path | None:
    if planning_dir is None:
        return None
    candidate = planning_dir.resolve().parent.parent
    if not looks_like_kodawari_repo(candidate):
        return None
    return candidate


def repo_from_project_root(project_root: Path | None) -> Path | None:
    if project_root is None:
        return None
    candidate = project_root.resolve()
    if not looks_like_kodawari_repo(candidate):
        return None
    return candidate


def canonical_wrapper_hint(repo_root: Path | None) -> str | None:
    if repo_root is None:
        return None
    wrapper = (repo_root / "scripts" / "kodawari.ps1").resolve()
    if not wrapper.exists():
        return None
    return str(wrapper)


def repo_match(left: Path | None, right: Path | None) -> bool | None:
    if left is None or right is None:
        return None
    return left == right


def entrypoint_resolution(
    *,
    target_repo: Path | None,
    module_repo: Path | None,
    cwd_repo: Path | None,
    wrapper_repo: Path | None,
    wrapper_path: str | None,
    canonical_wrapper_env: str | None,
) -> dict[str, Any]:
    target_repo_text = stringify_path(target_repo)
    module_match = repo_match(module_repo, target_repo)
    cwd_match = repo_match(cwd_repo, target_repo)
    wrapper_match = repo_match(wrapper_repo, target_repo)
    wrapper_is_canonical = same_resolved_path(wrapper_path, canonical_wrapper_env)
    direct_non_canonical = bool(wrapper_path) and wrapper_is_canonical is False
    likely_mis_hit = module_match is False and (cwd_match is True or wrapper_match is True or direct_non_canonical)
    return {
        "target_repo_root": target_repo_text,
        "module_matches_target_repo": module_match,
        "cwd_matches_target_repo": cwd_match,
        "wrapper_matches_target_repo": wrapper_match,
        "wrapper_is_canonical": wrapper_is_canonical,
        "likely_old_install_mis_hit": likely_mis_hit,
        "recommended_entrypoint": canonical_wrapper_hint(target_repo),
    }


def target_repo_root(
    *,
    project_root: Path | None,
    planning_dir: Path | None,
    cwd_repo: Path | None,
    wrapper_repo: Path | None,
    module_repo: Path | None,
) -> Path | None:
    return (
        repo_from_project_root(project_root)
        or repo_from_planning_dir(planning_dir)
        or cwd_repo
        or wrapper_repo
        or module_repo
    )


def _resolved_planning_dir_texts(resolved_planning_dirs: list[Path] | None) -> list[str]:
    return [str(Path(item).resolve()) for item in list(resolved_planning_dirs or [])]


def _cli_repo_alignment(
    *,
    module_repo: Path | None,
    cwd_repo: Path | None,
    wrapper_repo: Path | None,
    project_root: Path | None,
) -> dict[str, str]:
    return {
        "module_vs_cwd_repo": repo_alignment(module_repo, cwd_repo),
        "module_vs_wrapper_repo": repo_alignment(module_repo, wrapper_repo),
        "module_vs_project_repo": repo_alignment(module_repo, repo_from_project_root(project_root)),
    }


def _cli_common_payload(
    *,
    command: str,
    cwd: Path,
    project_root_text: str | None,
    planning_dir_text: str | None,
    resolved_planning_dirs: list[str],
    module_repo: Path | None,
    cwd_repo: Path | None,
    wrapper_repo: Path | None,
    wrapper_path: str | None,
    canonical_wrapper_env: str | None,
    wrapper_is_canonical: bool | None,
) -> dict[str, Any]:
    return {
        "command": str(command),
        "cwd": str(cwd),
        "project_root": project_root_text,
        "planning_dir": planning_dir_text,
        "resolved_planning_dirs": resolved_planning_dirs,
        "module_repo_root": stringify_path(module_repo),
        "cwd_repo_root": stringify_path(cwd_repo),
        "wrapper_repo_root": stringify_path(wrapper_repo),
        "wrapper_path": wrapper_path,
        "canonical_wrapper_env": canonical_wrapper_env,
        "wrapper_is_canonical": wrapper_is_canonical,
        "wrapper_invocation_cwd": os.environ.get("WORKFLOWCTL_INVOCATION_CWD"),
        "invoked_via_wrapper": bool(wrapper_path),
    }


def build_cli_provenance(
    *,
    command: str,
    project_root: Path | None = None,
    planning_dir: Path | None = None,
    resolved_planning_dirs: list[Path] | None = None,
    module_file: Path,
) -> dict[str, Any]:
    cwd = Path.cwd().resolve()
    module_repo = find_kodawari_repo_root(module_file)
    cwd_repo = find_kodawari_repo_root(cwd)
    wrapper_repo = resolved_wrapper_repo_root()
    wrapper_path = os.environ.get("WORKFLOWCTL_WRAPPER")
    canonical_wrapper_env = os.environ.get("WORKFLOWCTL_CANONICAL_WRAPPER")
    target_repo = target_repo_root(
        project_root=project_root,
        planning_dir=planning_dir,
        cwd_repo=cwd_repo,
        wrapper_repo=wrapper_repo,
        module_repo=module_repo,
    )
    resolution = entrypoint_resolution(
        target_repo=target_repo,
        module_repo=module_repo,
        cwd_repo=cwd_repo,
        wrapper_repo=wrapper_repo,
        wrapper_path=wrapper_path,
        canonical_wrapper_env=canonical_wrapper_env,
    )
    payload = _cli_common_payload(
        command=command,
        cwd=cwd,
        project_root_text=stringify_path(project_root.resolve() if project_root is not None else None),
        planning_dir_text=stringify_path(planning_dir.resolve() if planning_dir is not None else None),
        resolved_planning_dirs=_resolved_planning_dir_texts(resolved_planning_dirs),
        module_repo=module_repo,
        cwd_repo=cwd_repo,
        wrapper_repo=wrapper_repo,
        wrapper_path=wrapper_path,
        canonical_wrapper_env=canonical_wrapper_env,
        wrapper_is_canonical=resolution["wrapper_is_canonical"],
    )
    payload["repo_alignment"] = _cli_repo_alignment(
        module_repo=module_repo,
        cwd_repo=cwd_repo,
        wrapper_repo=wrapper_repo,
        project_root=project_root,
    )
    payload.update({"canonical_wrapper_hint": resolution["recommended_entrypoint"], "entrypoint_resolution": resolution})
    return payload

def build_stability_report_provenance(project_root: Path, planning_dirs: list[Path], *, module_file: Path) -> dict[str, Any]:
    cwd = Path.cwd().resolve()
    module_repo = find_kodawari_repo_root(module_file)
    cwd_repo = find_kodawari_repo_root(cwd)
    project_repo = project_root if looks_like_kodawari_repo(project_root) else None
    wrapper_path = os.environ.get("WORKFLOWCTL_WRAPPER")
    canonical_wrapper_env = os.environ.get("WORKFLOWCTL_CANONICAL_WRAPPER")
    resolution = entrypoint_resolution(
        target_repo=project_repo,
        module_repo=module_repo,
        cwd_repo=cwd_repo,
        wrapper_repo=resolved_wrapper_repo_root(),
        wrapper_path=wrapper_path,
        canonical_wrapper_env=canonical_wrapper_env,
    )
    return {
        "command": "stability-report",
        "cwd": str(cwd),
        "project_root": str(project_root),
        "resolved_planning_dirs": [str(path.resolve()) for path in planning_dirs],
        "module_repo_root": stringify_path(module_repo),
        "cwd_repo_root": stringify_path(cwd_repo),
        "wrapper_repo_root": os.environ.get("WORKFLOWCTL_REPO_ROOT"),
        "wrapper_path": wrapper_path,
        "canonical_wrapper_env": canonical_wrapper_env,
        "wrapper_is_canonical": resolution["wrapper_is_canonical"],
        "wrapper_invocation_cwd": os.environ.get("WORKFLOWCTL_INVOCATION_CWD"),
        "repo_alignment": {
            "module_vs_cwd_repo": repo_alignment(module_repo, cwd_repo),
            "module_vs_project_repo": repo_alignment(module_repo, project_repo),
        },
        "canonical_wrapper_hint": resolution["recommended_entrypoint"],
        "entrypoint_resolution": resolution,
    }

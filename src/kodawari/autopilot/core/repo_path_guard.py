"""Repository path guards for model/tool supplied paths.

This module is intentionally small and dependency-light so review bundling,
executor tools, and future MCP/API runtimes can share the same trust boundary:
model supplied paths are data, not authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from kodawari.autopilot.core.permission_policy import PermissionTier, evaluate_permission


DEFAULT_MAX_READ_BYTES = 256_000


@dataclass(frozen=True)
class RepoPathGuardResult:
    allowed: bool
    path: str
    reason: str = ""
    resolved_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "allowed": bool(self.allowed),
            "reason": self.reason,
        }
        if self.resolved_path is not None:
            payload["resolved_path"] = str(self.resolved_path)
        return payload


def _normalize_candidate(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _has_parent_traversal(path: str) -> bool:
    return any(part == ".." for part in PurePosixPath(path).parts)


def _is_absolute_or_drive_path(path: str) -> bool:
    candidate = Path(path)
    return bool(candidate.is_absolute() or candidate.drive)


def _resolve_repo_path(project_root: Path, path: str) -> RepoPathGuardResult:
    normalized = _normalize_candidate(path)
    if not normalized:
        return RepoPathGuardResult(False, normalized, "path is empty")
    if "\x00" in normalized:
        return RepoPathGuardResult(False, normalized, "path contains NUL byte")
    if _is_absolute_or_drive_path(normalized):
        return RepoPathGuardResult(False, normalized, "absolute paths are not allowed")
    if _has_parent_traversal(normalized):
        return RepoPathGuardResult(False, normalized, "parent traversal is not allowed")
    root = project_root.resolve()
    resolved = (root / normalized).resolve()
    if not resolved.is_relative_to(root):
        return RepoPathGuardResult(False, normalized, "path resolves outside project root", resolved)
    return RepoPathGuardResult(True, normalized, "", resolved)


def guard_repo_read_path(
    *,
    project_root: Path,
    path: str,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
    require_file: bool = True,
) -> RepoPathGuardResult:
    base = _resolve_repo_path(project_root, path)
    if not base.allowed:
        return base
    normalized = base.path
    decision = evaluate_permission(tool="Read", path=normalized)
    if decision.tier is PermissionTier.BLOCK:
        return RepoPathGuardResult(False, normalized, decision.reason, base.resolved_path)
    resolved = base.resolved_path
    if require_file:
        if resolved is None or not resolved.exists():
            return RepoPathGuardResult(False, normalized, "file does not exist", resolved)
        if not resolved.is_file():
            return RepoPathGuardResult(False, normalized, "path is not a file", resolved)
        try:
            if resolved.stat().st_size > max(0, int(max_bytes)):
                return RepoPathGuardResult(False, normalized, "file exceeds read size limit", resolved)
        except OSError as exc:
            return RepoPathGuardResult(False, normalized, f"file stat failed: {exc}", resolved)
    return RepoPathGuardResult(True, normalized, "", resolved)


def guard_repo_write_path(*, project_root: Path, path: str) -> RepoPathGuardResult:
    base = _resolve_repo_path(project_root, path)
    if not base.allowed:
        return base
    normalized = base.path
    for tool in ("Write", "Edit"):
        decision = evaluate_permission(tool=tool, path=normalized)
        if decision.tier is PermissionTier.BLOCK:
            return RepoPathGuardResult(False, normalized, decision.reason, base.resolved_path)
    return RepoPathGuardResult(True, normalized, "", base.resolved_path)


def filter_repo_read_paths(
    *,
    project_root: Path,
    paths: list[str],
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
    require_file: bool = True,
) -> tuple[list[str], list[dict[str, Any]]]:
    allowed: list[str] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in paths:
        result = guard_repo_read_path(
            project_root=project_root,
            path=item,
            max_bytes=max_bytes,
            require_file=require_file,
        )
        if result.allowed:
            if result.path not in seen:
                seen.add(result.path)
                allowed.append(result.path)
            continue
        if result.path:
            rejected.append(result.to_dict())
    return allowed, rejected


__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "RepoPathGuardResult",
    "filter_repo_read_paths",
    "guard_repo_read_path",
    "guard_repo_write_path",
]

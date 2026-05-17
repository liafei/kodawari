"""Canonical locations for local workflow runtime state."""

from __future__ import annotations

from pathlib import Path

WORKFLOW_RUNTIME_DIRNAME = ".workflow_runtime"


def workflow_runtime_root(project_root: Path | str | None = None) -> Path:
    root = Path.cwd() if project_root is None else Path(project_root)
    return (root.resolve() / WORKFLOW_RUNTIME_DIRNAME).resolve()


def reviewer_home(project_root: Path | str | None, backend: str) -> Path:
    normalized = str(backend or "").strip().lower()
    suffix = "reviewer_codex_home" if normalized == "codex" else "reviewer_claude_home"
    return (workflow_runtime_root(project_root) / "reviewer_homes" / suffix).resolve()


__all__ = ["WORKFLOW_RUNTIME_DIRNAME", "reviewer_home", "workflow_runtime_root"]


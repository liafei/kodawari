"""Reviewer backend health probing helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import os
import time
from typing import Any, Callable

from kodawari.autopilot.review.gateways.cli import CliReviewerConfig
from kodawari.autopilot.review.gateways.codex import CodexReviewerConfig
from kodawari.autopilot.core.runtime_paths import reviewer_home


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _resolve_workspace_root(workspace_root: Path | str | None) -> Path:
    if workspace_root is None:
        return Path.cwd().resolve()
    return Path(workspace_root).resolve()


def _is_codex_executable_name(executable: str) -> bool:
    stem = Path(executable).stem.lower()
    return stem == "codex" or stem.startswith("codex-") or stem.startswith("codex_")


def _is_claude_executable_name(executable: str) -> bool:
    stem = Path(executable).stem.lower()
    return stem == "claude" or stem.startswith("claude-") or stem.startswith("claude_")


def _ensure_dir_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe_path = path / ".write_probe"
        probe_path.write_text("ok", encoding="utf-8")
        probe_path.unlink(missing_ok=True)
    except OSError as exc:
        return False, str(exc)
    return True, ""


def _default_reviewer_home(backend: str, workspace_root: Path) -> Path:
    normalized = "codex" if backend == "codex" else "claude"
    return reviewer_home(workspace_root, normalized)


def _resolve_reviewer_home(backend: str, workspace_root: Path) -> Path:
    if backend == "codex":
        configured = _clean_text(os.environ.get("WORKFLOW_REVIEWER_CODEX_HOME"))
    else:
        configured = _clean_text(os.environ.get("WORKFLOW_REVIEWER_CLAUDE_HOME"))
    if configured:
        return Path(configured).resolve()
    return _default_reviewer_home(backend, workspace_root)


@dataclass(frozen=True)
class ReviewerHealthReport:
    backend: str
    available: bool
    probe_status: str
    detail: str
    probed_at: str
    auth_probe_ok: bool = False
    schema_probe_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "available": self.available,
            "probe_status": self.probe_status,
            "detail": self.detail,
            "probed_at": self.probed_at,
            "auth_probe_ok": self.auth_probe_ok,
            "schema_probe_ok": self.schema_probe_ok,
        }


@dataclass
class ReviewerHealthCache:
    ttl_seconds: int = 600
    _entries: dict[str, tuple[float, ReviewerHealthReport]] = field(default_factory=dict)

    def _key(self, *, backend: str, workspace_root: Path) -> str:
        return f"{backend}::{str(workspace_root).lower()}"

    def get(self, *, backend: str, workspace_root: Path) -> ReviewerHealthReport | None:
        key = self._key(backend=backend, workspace_root=workspace_root)
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, report = entry
        if time.time() >= expires_at:
            self._entries.pop(key, None)
            return None
        return report

    def put(self, *, backend: str, workspace_root: Path, report: ReviewerHealthReport) -> None:
        key = self._key(backend=backend, workspace_root=workspace_root)
        ttl = max(0, int(self.ttl_seconds))
        self._entries[key] = (time.time() + ttl, report)


def probe_reviewer_backend(
    *,
    backend: str,
    workspace_root: Path | str | None,
    cli_config: CliReviewerConfig | None = None,
    codex_config: CodexReviewerConfig | None = None,
    reviewer_base_url: str = "",
    reviewer_api_key: str = "",
    cache: ReviewerHealthCache | None = None,
    cli_available_fn: Callable[[CliReviewerConfig | None], bool] | None = None,
    codex_available_fn: Callable[[CodexReviewerConfig | None], bool] | None = None,
) -> ReviewerHealthReport:
    normalized_backend = _clean_text(backend).lower()
    workspace = _resolve_workspace_root(workspace_root)

    if cache is not None:
        cached = cache.get(backend=normalized_backend, workspace_root=workspace)
        if cached is not None:
            return cached

    report = _probe_uncached(
        backend=normalized_backend,
        workspace_root=workspace,
        cli_config=cli_config,
        codex_config=codex_config,
        reviewer_base_url=reviewer_base_url,
        reviewer_api_key=reviewer_api_key,
        cli_available_fn=cli_available_fn,
        codex_available_fn=codex_available_fn,
    )
    if cache is not None:
        cache.put(backend=normalized_backend, workspace_root=workspace, report=report)
    return report


def _probe_uncached(
    *,
    backend: str,
    workspace_root: Path,
    cli_config: CliReviewerConfig | None,
    codex_config: CodexReviewerConfig | None,
    reviewer_base_url: str,
    reviewer_api_key: str,
    cli_available_fn: Callable[[CliReviewerConfig | None], bool] | None,
    codex_available_fn: Callable[[CodexReviewerConfig | None], bool] | None,
) -> ReviewerHealthReport:
    now = _utc_now_iso()
    cli_available = cli_available_fn or (lambda cfg: False)
    codex_available = codex_available_fn or (lambda cfg: False)

    if backend in {"cli", "mcp"}:
        cfg = cli_config or CliReviewerConfig()
        executable = _clean_text(cfg.executable) or "claude"
        if not _is_claude_executable_name(executable):
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="executable_invalid",
                detail=f"{backend} reviewer: '{executable}' is not a claude binary (expected claude or claude-*)",
                probed_at=now,
            )
        if not cli_available(cfg):
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="executable_missing",
                detail=f"{backend} reviewer executable not found: {executable}",
                probed_at=now,
            )
        reviewer_home = _resolve_reviewer_home(backend, workspace_root)
        writable, error = _ensure_dir_writable(reviewer_home)
        if not writable:
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="home_unwritable",
                detail=f"{backend} reviewer home is not writable: {reviewer_home} ({error})",
                probed_at=now,
            )
        return ReviewerHealthReport(
            backend=backend,
            available=True,
            probe_status="ok",
            detail="reviewer backend is healthy",
            probed_at=now,
            auth_probe_ok=True,
            schema_probe_ok=True,
        )

    if backend == "codex":
        cfg = codex_config or CodexReviewerConfig()
        executable = _clean_text(cfg.executable) or "codex"
        if not _is_codex_executable_name(executable):
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="executable_invalid",
                detail=f"codex reviewer: '{executable}' is not a codex binary (expected codex or codex-*)",
                probed_at=now,
            )
        if not codex_available(cfg):
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="executable_missing",
                detail=f"codex reviewer executable not found: {executable}",
                probed_at=now,
            )
        reviewer_home = _resolve_reviewer_home(backend, workspace_root)
        writable, error = _ensure_dir_writable(reviewer_home)
        if not writable:
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="home_unwritable",
                detail=f"codex reviewer home is not writable: {reviewer_home} ({error})",
                probed_at=now,
            )
        return ReviewerHealthReport(
            backend=backend,
            available=True,
            probe_status="ok",
            detail="reviewer backend is healthy",
            probed_at=now,
            auth_probe_ok=True,
            schema_probe_ok=True,
        )

    if backend == "api":
        base_url = _clean_text(reviewer_base_url)
        api_key = _clean_text(reviewer_api_key)
        if not base_url:
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="config_missing",
                detail="WORKFLOW_REVIEWER_BASE_URL (or WORKFLOW_OPUS_GATEWAY) is empty",
                probed_at=now,
            )
        if not api_key:
            return ReviewerHealthReport(
                backend=backend,
                available=False,
                probe_status="config_missing",
                detail="WORKFLOW_REVIEWER_API_KEY (or WORKFLOW_OPUS_API_KEY) is empty",
                probed_at=now,
            )
        return ReviewerHealthReport(
            backend=backend,
            available=True,
            probe_status="ok",
            detail="reviewer backend is healthy",
            probed_at=now,
            auth_probe_ok=True,
            schema_probe_ok=True,
        )

    return ReviewerHealthReport(
        backend=backend or "unknown",
        available=False,
        probe_status="unsupported_backend",
        detail=f"unsupported reviewer backend {backend!r}; expected one of: api, cli, mcp, codex",
        probed_at=now,
    )


__all__ = [
    "ReviewerHealthCache",
    "ReviewerHealthReport",
    "probe_reviewer_backend",
]

"""Peer-review preflight orchestration for the local adapter facade."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class PeerReviewPreflightDeps:
    real_requested: bool
    real_required: bool
    backend: str
    workspace_root: Path
    doctor_enabled: bool
    cli_config: Any
    codex_config: Any
    reviewer_base_url: str
    reviewer_api_key: str
    health_cache: Any
    gateway_error: str
    cli_available_fn: Callable[[Any], bool]
    codex_available_fn: Callable[[Any], bool]
    probe_backend_fn: Callable[..., Any]
    write_health_fn: Callable[..., None]
    failed_review_fn: Callable[..., dict[str, Any]]


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def peer_review_preflight(*, context: dict[str, Any], deps: PeerReviewPreflightDeps) -> dict[str, Any]:
    if not deps.real_requested:
        return {"ready": True}
    backend = deps.backend
    health_report = None
    if backend.startswith("unsupported:"):
        error = f"unsupported reviewer backend {backend[len('unsupported:'):]!r}; expected one of: api, cli, mcp, codex"
        deps.write_health_fn(
            context=context,
            report={
                "backend": backend,
                "available": False,
                "probe_status": "unsupported_backend",
                "detail": error,
            },
        )
        return {
            "ready": False,
            "blocking_error": error,
            "review": deps.failed_review_fn(error=error, real_required=deps.real_required),
        }
    if deps.doctor_enabled:
        health_report = deps.probe_backend_fn(
            backend=backend,
            workspace_root=deps.workspace_root,
            cli_config=deps.cli_config if backend in {"cli", "mcp"} else None,
            codex_config=deps.codex_config if backend == "codex" else None,
            reviewer_base_url=deps.reviewer_base_url,
            reviewer_api_key=deps.reviewer_api_key,
            cache=deps.health_cache,
            cli_available_fn=deps.cli_available_fn,
            codex_available_fn=deps.codex_available_fn,
        )
        deps.write_health_fn(context=context, report=health_report.to_dict())
        if health_report.available:
            return {"ready": True}
        error = health_report.detail
    elif backend in {"cli", "mcp"} and deps.cli_available_fn(deps.cli_config):
        return {"ready": True}
    elif backend == "codex" and deps.codex_available_fn(deps.codex_config):
        return {"ready": True}
    elif backend in {"cli", "mcp", "codex"}:
        error = _reviewer_executable_error(backend=backend, cli_config=deps.cli_config, codex_config=deps.codex_config)
    else:
        error = deps.gateway_error
        if not error:
            return {"ready": True}
    deps.write_health_fn(
        context=context,
        report={
            "backend": backend,
            "available": False,
            "probe_status": "unavailable",
            "detail": error,
        },
    )
    if not deps.real_required:
        return {
            "ready": True,
            "reviewer_doctor_degraded": True,
            "degraded_reason": error,
            "reviewer_health": health_report.to_dict() if health_report is not None else {
                "backend": backend,
                "available": False,
                "probe_status": "unavailable",
                "detail": error,
            },
        }
    return {
        "ready": False,
        "blocking_error": error,
        "review": deps.failed_review_fn(error=error, real_required=deps.real_required),
    }


def _reviewer_executable_error(*, backend: str, cli_config: Any, codex_config: Any) -> str:
    if backend == "codex":
        exe = _clean_text(getattr(codex_config, "executable", "")) or "codex"
        exe_stem = Path(exe).stem.lower()
        if exe_stem != "codex" and not exe_stem.startswith("codex-") and not exe_stem.startswith("codex_"):
            return f"codex reviewer: '{exe}' is not a codex binary (expected codex or codex-*)"
        return f"codex reviewer executable not found: {exe}"
    exe = _clean_text(getattr(cli_config, "executable", "")) or "claude"
    exe_stem = Path(exe).stem.lower()
    if exe_stem != "claude" and not exe_stem.startswith("claude-") and not exe_stem.startswith("claude_"):
        return f"{backend} reviewer: '{exe}' is not a claude binary (expected claude or claude-*)"
    return f"{backend} reviewer executable not found: {exe}"

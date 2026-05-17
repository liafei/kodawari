"""Autopilot release-tail helpers built on canonical delivery builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kodawari.cli.delivery.delivery_workflow import (
    build_qa_report,
    build_review_report,
    build_ship_readiness_report,
    build_verify_report,
)

TailBuilder = Callable[..., dict[str, Any]]
_TERMINAL_STATUSES = {"BLOCKED", "FAIL", "ERROR"}


@dataclass(frozen=True)
class AutopilotReleaseTailConfig:
    base_branch: str = "main"
    changed_files_override: list[str] = field(default_factory=list)
    scope_allow: list[str] = field(default_factory=list)
    verify_command_file: str | None = None
    verify_command: str | None = None
    eval_report_path: str | None = None
    auto_eval: bool = False
    risk_profile: str = "medium"


@dataclass(frozen=True)
class _TailStep:
    name: str
    builder: TailBuilder
    kwargs: dict[str, Any]


@dataclass(frozen=True)
class _TailContext:
    project_root: Path
    planning_dir: Path
    feature: str
    config: AutopilotReleaseTailConfig
    builders: dict[str, TailBuilder]


def run_autopilot_release_tail(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    config: AutopilotReleaseTailConfig | None = None,
    builders: dict[str, TailBuilder] | None = None,
) -> dict[str, Any]:
    resolved = _TailContext(
        project_root=Path(project_root).resolve(),
        planning_dir=Path(planning_dir).resolve(),
        feature=str(feature).strip(),
        config=config or AutopilotReleaseTailConfig(),
        builders=_resolve_builders(builders),
    )
    stages: dict[str, dict[str, Any]] = {}
    completed: list[str] = []
    blocked_stage = ""
    for step in _tail_steps(resolved):
        payload = _run_step(resolved, step)
        stages[step.name] = payload
        if _is_terminal_status(payload):
            blocked_stage = step.name
            break
        completed.append(step.name)
    return _tail_payload(
        context=resolved,
        stages=stages,
        completed=completed,
        blocked_stage=blocked_stage,
    )


def _resolve_builders(overrides: dict[str, TailBuilder] | None) -> dict[str, TailBuilder]:
    resolved = {
        "review": build_review_report,
        "verify": build_verify_report,
        "qa": build_qa_report,
        "ship_readiness": build_ship_readiness_report,
    }
    for name, builder in dict(overrides or {}).items():
        if callable(builder):
            resolved[str(name)] = builder
    return resolved


def _tail_steps(context: _TailContext) -> list[_TailStep]:
    cfg = context.config
    return [
        _TailStep(
            name="review",
            builder=context.builders["review"],
            kwargs={
                "base_branch": cfg.base_branch,
                "changed_files_override": list(cfg.changed_files_override),
                "scope_allow": list(cfg.scope_allow),
            },
        ),
        _TailStep(
            name="verify",
            builder=context.builders["verify"],
            kwargs={
                "base_branch": cfg.base_branch,
                "changed_files_override": list(cfg.changed_files_override),
                "verify_command_file": cfg.verify_command_file,
                "verify_command": cfg.verify_command,
            },
        ),
        _TailStep(
            name="qa",
            builder=context.builders["qa"],
            kwargs={},
        ),
        _TailStep(
            name="ship_readiness",
            builder=context.builders["ship_readiness"],
            kwargs={
                "eval_report_path": cfg.eval_report_path,
                "auto_eval": bool(cfg.auto_eval),
                "risk_profile": cfg.risk_profile,
            },
        ),
    ]


def _run_step(context: _TailContext, step: _TailStep) -> dict[str, Any]:
    payload = step.builder(
        project_root=context.project_root,
        planning_dir=context.planning_dir,
        feature=context.feature,
        **dict(step.kwargs),
    )
    if not isinstance(payload, dict):
        raise ValueError(f"autopilot release step '{step.name}' must return a dict payload")
    return dict(payload)


def _is_terminal_status(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").upper()
    return status in _TERMINAL_STATUSES


def _tail_payload(
    *,
    context: _TailContext,
    stages: dict[str, dict[str, Any]],
    completed: list[str],
    blocked_stage: str,
) -> dict[str, Any]:
    blocked_payload = dict(stages.get(blocked_stage) or {})
    status = str(blocked_payload.get("status") or "PASS").upper() if blocked_stage else "PASS"
    blocking_reason = _blocking_reason(blocked_payload) if blocked_stage else ""
    next_action = str(blocked_payload.get("next_action") or "") if blocked_stage else ""
    return {
        "status": status,
        "entrypoint": "kodawari autopilot",
        "feature": context.feature,
        "planning_dir": str(context.planning_dir),
        "completed_stages": completed,
        "blocked_stage": blocked_stage or None,
        "blocking_reason": blocking_reason,
        "next_action": next_action,
        "stages": stages,
    }


def _blocking_reason(payload: dict[str, Any]) -> str:
    for key in ("blocking_reason", "summary", "details"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return str(payload.get("status") or "").upper()


__all__ = [
    "AutopilotReleaseTailConfig",
    "run_autopilot_release_tail",
]


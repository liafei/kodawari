"""Invocation helpers shared by legacy runtime shell commands.

REMOVE_AFTER: 2026-08-01
REMOVAL_PLAN: Consolidate into autopilot_runtime_flow.py; delete once legacy_cmds is removed.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
from typing import Any
import warnings


LEGACY_CLI_REMOVE_AFTER = "2026-08-01"


def legacy_deprecation_payload(*, entrypoint: str, replacement: str) -> dict[str, Any]:
    return {
        "status": "deprecated",
        "entrypoint": entrypoint,
        "replacement": replacement,
        "remove_after": LEGACY_CLI_REMOVE_AFTER,
    }


def warn_legacy_entrypoint(*, entrypoint: str, replacement: str) -> None:
    warnings.warn(
        f"{entrypoint} is deprecated and will be removed after {LEGACY_CLI_REMOVE_AFTER}; use {replacement}.",
        DeprecationWarning,
        stacklevel=2,
    )


def parse_handler_stdout(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {"raw_output": text}
    if isinstance(payload, dict):
        return payload
    return {"value": payload}


def invoke_cli_handler(handler: Any, namespace: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = int(handler(namespace))
    return rc, parse_handler_stdout(buffer.getvalue())


def legacy_step_result(
    *,
    name: str,
    rc: int,
    payload: dict[str, Any] | None = None,
    skipped: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "skipped": skipped,
        "rc": int(rc),
        "reason": reason,
        "payload": dict(payload or {}),
    }


def legacy_autopilot_namespace(args: argparse.Namespace, *, command: str) -> argparse.Namespace:
    max_cycles = int(getattr(args, "max_cycles", 8))
    if command == "quick-develop":
        max_cycles = max(max_cycles, 8)
    return argparse.Namespace(
        project_root=str(Path(args.project_root).resolve()),
        feature=str(args.feature),
        tier="heavy",
        prd=None,
        requirements_file=getattr(args, "requirements_file", None),
        profile=getattr(args, "profile", "profiles/generic.yaml"),
        verify_cmd=getattr(args, "verify_cmd", "pytest -q"),
        max_cycles=max_cycles,
        token_budget=int(getattr(args, "token_budget", 300000)),
        executor_backend=getattr(args, "executor_backend", ""),
        executor_command=getattr(args, "executor_command", ""),
        self_review_backend=getattr(args, "self_review_backend", ""),
        self_review_command=getattr(args, "self_review_command", ""),
        real_peer_review=bool(getattr(args, "real_peer_review", False) or getattr(args, "real_opus_review", False)),
        require_real_peer_review=bool(getattr(args, "require_real_peer_review", False) or getattr(args, "require_real_opus_review", False)),
        opus_reviewer_backend=str(getattr(args, "opus_reviewer_backend", "") or ""),
        executor_model=str(getattr(args, "executor_model", "") or ""),
        reviewer_backend=str(getattr(args, "reviewer_backend", "") or ""),
        reviewer_model=str(getattr(args, "reviewer_model", "") or ""),
        reviewer_api_format=str(getattr(args, "reviewer_api_format", "") or ""),
        reviewer_base_url=str(getattr(args, "reviewer_base_url", "") or ""),
        peer_review_max_tokens=int(getattr(args, "peer_review_max_tokens", 4096)),
        task_cycle=command in {"research", "develop", "quick-develop", "optimize-existing-develop"},
        enable_peer_review=command in {"research", "develop", "optimize-existing-develop"},
        task_label=None,
        task_scope=None,
    )


def legacy_status_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(Path(args.project_root).resolve()),
        feature=str(args.feature),
        planning_dir=getattr(args, "planning_dir", None),
    )


def legacy_gate_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(Path(args.project_root).resolve()),
        path=[],
        feature=str(args.feature),
        planning_dir=getattr(args, "planning_dir", None),
        profile=getattr(args, "gate_profile", "advisory"),
        output=None,
        fail_on_block=False,
    )


def legacy_stability_namespace(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=str(Path(args.project_root).resolve()),
        run_id=[str(args.feature)],
        planning_dir=[],
        all_runs=False,
        updated_since=None,
        updated_until=None,
        task_max_cycles=None,
        task_auto_runs=None,
        timeout_per_round=None,
        token_budget_target=None,
        output=None,
    )

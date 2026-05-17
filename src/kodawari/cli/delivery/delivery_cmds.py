"""Thin CLI wrappers for review, verify, QA, and ship-readiness commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable

from kodawari.cli.delivery.delivery_workflow import (
    build_qa_report,
    build_review_report,
    build_ship_readiness_report,
    build_verify_report,
    resolve_planning_dir as resolve_delivery_planning_dir,
)
from kodawari.cli.main_support import (
    _build_cli_provenance,
    _command_preflight,
    _normalized_error_payload,
    _preflight_blocked_payload,
    _write_optional_json_output,
)
from kodawari.cli.contract.command_contract import normalize_mutating_payload

Builder = Callable[[Path, Path, str, argparse.Namespace], dict[str, Any]]


def _run_delivery_report_command(
    *,
    args: argparse.Namespace,
    command: str,
    builder: Builder,
    error_code: str,
    remediation: list[str],
    default_next_action: str,
) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dir: Path | None = None
    try:
        planning_dir, feature = resolve_delivery_planning_dir(
            project_root=project_root,
            feature=getattr(args, "feature", None),
            planning_dir=getattr(args, "planning_dir", None),
        )
        preflight = _command_preflight(
            command=command,
            project_root=project_root,
            planning_dir=planning_dir,
        )
        if str(preflight.get("status")) == "BLOCKED":
            print(
                json.dumps(
                    _preflight_blocked_payload(
                        command=command,
                        project_root=project_root,
                        planning_dir=planning_dir,
                        preflight=preflight,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        payload = builder(project_root, planning_dir, feature, args)
    except ValueError as exc:
        payload = _normalized_error_payload(
            command=command,
            project_root=project_root,
            planning_dir=planning_dir,
            error=str(exc),
            error_code=error_code,
            remediation=remediation,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    payload["preflight"] = preflight
    payload["provenance"] = _build_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
    )
    payload = normalize_mutating_payload(
        payload,
        default_next_action="" if str(payload.get("status") or "").upper() == "PASS" else default_next_action,
    )
    _write_optional_json_output(payload, getattr(args, "output", None))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if bool(getattr(args, "fail_on_block", False)) and str(payload.get("status") or "").upper() == "BLOCKED":
        return 2
    return 0


def _build_review(project_root: Path, planning_dir: Path, feature: str, args: argparse.Namespace) -> dict[str, Any]:
    return build_review_report(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        base_branch=str(getattr(args, "base_branch", "main")),
        changed_files_override=list(getattr(args, "changed_file", []) or []),
        scope_allow=list(getattr(args, "scope_allow", []) or []),
    )


def _build_verify(project_root: Path, planning_dir: Path, feature: str, args: argparse.Namespace) -> dict[str, Any]:
    return build_verify_report(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        base_branch=str(getattr(args, "base_branch", "main")),
        changed_files_override=list(getattr(args, "changed_file", []) or []),
        verify_command_file=getattr(args, "command_file", None),
        verify_command=getattr(args, "command", None),
    )


def _build_qa(project_root: Path, planning_dir: Path, feature: str, args: argparse.Namespace) -> dict[str, Any]:
    del args
    return build_qa_report(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
    )


def _build_ship_readiness(
    project_root: Path,
    planning_dir: Path,
    feature: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    return build_ship_readiness_report(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        eval_report_path=getattr(args, "eval_report_path", None),
        auto_eval=bool(getattr(args, "auto_eval", False)),
        risk_profile=str(getattr(args, "risk_profile", "medium")),
    )


def _cmd_review(args: argparse.Namespace) -> int:
    return _run_delivery_report_command(
        args=args,
        command="review",
        builder=_build_review,
        error_code="review_failed",
        remediation=["Fix the planning inputs and rerun `kodawari review`."],
        default_next_action="Fix the failing review checks and rerun review.",
    )


def _cmd_verify(args: argparse.Namespace) -> int:
    return _run_delivery_report_command(
        args=args,
        command="verify",
        builder=_build_verify,
        error_code="verify_failed",
        remediation=["Fix the verify inputs and rerun `kodawari verify`."],
        default_next_action="Fix the verify blockers and rerun verify.",
    )


def _cmd_qa(args: argparse.Namespace) -> int:
    return _run_delivery_report_command(
        args=args,
        command="qa",
        builder=_build_qa,
        error_code="qa_failed",
        remediation=["Fix the planning inputs and rerun `kodawari qa`."],
        default_next_action="Resolve the failing QA checks and rerun qa.",
    )


def _cmd_ship_readiness(args: argparse.Namespace) -> int:
    return _run_delivery_report_command(
        args=args,
        command="ship-readiness",
        builder=_build_ship_readiness,
        error_code="ship_readiness_failed",
        remediation=["Fix the readiness inputs and rerun `kodawari ship-readiness`."],
        default_next_action="Fix the blocking readiness item and rerun ship-readiness.",
    )


__all__ = [
    "_cmd_qa",
    "_cmd_review",
    "_cmd_ship_readiness",
    "_cmd_verify",
]


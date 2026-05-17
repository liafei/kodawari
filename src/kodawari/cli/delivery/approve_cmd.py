"""kodawari approve — write a decision response for a pending decision request."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kodawari.cli.runtime.autopilot_decision_runtime import (
    DECISION_REQUEST_FILENAME,
    DECISION_RESPONSE_FILENAME,
    build_decision_response,
    load_decision_request,
    load_decision_response,
    response_matches_request,
    valid_option_ids,
    write_decision_response,
)
from kodawari.cli.core.main_support import (
    MERGED_CONTRACT_VERSION,
    _build_cli_provenance,
    _normalized_error_payload,
    _resolve_feature_planning_dir,
    _write_optional_json_output,
)


def _cmd_approve(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dir = _resolve_approve_planning_dir(args, project_root)
    return _run_approve(args, project_root, planning_dir)


def _resolve_approve_planning_dir(args: argparse.Namespace, project_root: Path) -> Path:
    feature = getattr(args, "feature", "") or ""
    planning_dir_arg = getattr(args, "planning_dir", None) or None
    if planning_dir_arg:
        return Path(planning_dir_arg).resolve()
    if not feature:
        raise ValueError("approve requires --feature when --planning-dir is not provided")
    return _resolve_feature_planning_dir(
        project_root=project_root, feature=feature, planning_dir=None
    )


def _run_approve(args: argparse.Namespace, project_root: Path, planning_dir: Path) -> int:
    request = load_decision_request(planning_dir)
    if request is None:
        payload = _error(
            project_root=project_root,
            planning_dir=planning_dir,
            error=f"No decision request found at {planning_dir / DECISION_REQUEST_FILENAME}",
            error_code="no_decision_request",
            remediation=["Run kodawari autopilot first to generate a decision request."],
        )
        _emit(payload)
        return 1

    existing = load_decision_response(planning_dir)
    force = getattr(args, "force", False)
    if existing and response_matches_request(request, existing) and not force:
        payload = _error(
            project_root=project_root,
            planning_dir=planning_dir,
            error="Decision already responded; use --force to overwrite.",
            error_code="decision_already_responded",
            remediation=["Pass --force to overwrite the existing response."],
        )
        _emit(payload)
        return 1

    option = _pick_option(args, request)
    if option is None:
        payload = _error(
            project_root=project_root,
            planning_dir=planning_dir,
            error="No option available: request has no recommended_option and --option was not given.",
            error_code="no_option_selected",
            remediation=["Pass --option <option_id> explicitly."],
        )
        _emit(payload)
        return 1

    valid = valid_option_ids(request)
    if valid and option not in valid and not force:
        payload = _error(
            project_root=project_root,
            planning_dir=planning_dir,
            error=f"Option '{option}' is not in valid options {valid}.",
            error_code="invalid_option",
            remediation=[f"Valid options: {', '.join(valid)}. Pass --force to override."],
        )
        _emit(payload)
        return 1

    rationale = getattr(args, "rationale", "") or ""
    decision_id = str(request.get("decision_id") or "")
    response = build_decision_response(
        decision_id=decision_id,
        selected_option=option,
        rationale=rationale,
    )
    write_decision_response(planning_dir, response)

    payload = _success_payload(
        project_root=project_root,
        planning_dir=planning_dir,
        request=request,
        response=response,
        feature_hint=str(getattr(args, "feature", "") or ""),
    )
    output = getattr(args, "output", None)
    _write_optional_json_output(payload, output)
    _emit(payload)
    return 0


def _pick_option(args: argparse.Namespace, request: dict[str, Any]) -> str | None:
    option = str(getattr(args, "option", "") or "").strip()
    if option:
        return option
    recommended = str(request.get("recommended_option") or "").strip()
    return recommended or None


def _success_payload(
    *,
    project_root: Path,
    planning_dir: Path,
    request: dict[str, Any],
    response: dict[str, Any],
    feature_hint: str = "",
) -> dict[str, Any]:
    feature = _resume_feature_hint(
        request=request,
        planning_dir=planning_dir,
        explicit_feature=feature_hint,
    )
    provenance = _build_cli_provenance(
        command="approve",
        project_root=project_root,
        planning_dir=planning_dir,
    )
    return {
        "contract_version": MERGED_CONTRACT_VERSION,
        "command": "approve",
        "project_root": str(project_root),
        "planning_dir": str(planning_dir),
        "decision_id": str(request.get("decision_id") or ""),
        "decision_kind": str(request.get("decision_kind") or ""),
        "selected_option": str(response.get("selected_option") or ""),
        "rationale": str(response.get("rationale") or ""),
        "responded_at": str(response.get("responded_at") or ""),
        "response_file": str(planning_dir / DECISION_RESPONSE_FILENAME),
        "next_action": _next_action(feature),
        "provenance": provenance,
    }


def _next_action(feature: str) -> str:
    hint = f"kodawari autopilot --feature {feature}" if feature else "kodawari autopilot --feature <feature>"
    return f"Re-run {hint} to resume execution."


def _resume_feature_hint(
    *,
    request: dict[str, Any],
    planning_dir: Path,
    explicit_feature: str = "",
) -> str:
    feature = str(explicit_feature or "").strip()
    if feature:
        return feature
    decision_id = str(request.get("decision_id") or "").strip()
    if ":" in decision_id:
        prefix = decision_id.split(":", 1)[0].strip()
        if prefix:
            return prefix
    return planning_dir.name


def _error(
    *,
    project_root: Path,
    planning_dir: Path,
    error: str,
    error_code: str,
    remediation: list[str] | None = None,
) -> dict[str, Any]:
    return _normalized_error_payload(
        command="approve",
        project_root=project_root,
        planning_dir=planning_dir,
        error=error,
        error_code=error_code,
        remediation=remediation,
    )


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


__all__ = ["_cmd_approve"]


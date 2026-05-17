"""Work facade wrappers for kodawari."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kodawari.cli.runtime.autopilot_cmd import run_autopilot_command
from kodawari.cli.contract.command_contract import build_error_payload
from kodawari.cli.core.legacy_runtime_invocation import invoke_cli_handler
from kodawari.cli.main_support import _build_cli_provenance, _resolve_feature_planning_dir, _write_json_output
from kodawari.cli.runtime.work_all_runtime import run_work_all_command


def _ensure_feature(args: argparse.Namespace, *, command: str) -> int | None:
    feature = str(getattr(args, "feature", "") or "").strip()
    if feature:
        return None
    project_root = Path(getattr(args, "project_root", ".")).resolve()
    payload = build_error_payload(
        command=command,
        project_root=project_root,
        planning_dir=project_root / "planning",
        module_file=Path(__file__),
        error="--feature is required",
        error_code="feature_required",
        remediation=[f"Provide `--feature <name>` when running `kodawari {command}`."],
        next_action=f"Rerun `kodawari {command}` with `--feature <name>`.",
        extra={"_rc": 2, "status": "FAIL"},
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def _ensure_work_all_prd(args: argparse.Namespace) -> int | None:
    prd = str(getattr(args, "prd", "") or "").strip()
    if prd:
        return None
    project_root = Path(getattr(args, "project_root", ".")).resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    planning_dir = (project_root / "planning" / feature).resolve() if feature else (project_root / "planning").resolve()
    payload = build_error_payload(
        command="work-all",
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
        error="work all requires --prd to run full contract-first artifact chain.",
        error_code="work_all_prd_required",
        remediation=[
            "Provide `--prd <path>` when running `kodawari work all`.",
            "Use `kodawari work` for non-fullchain execution routing.",
        ],
        next_action="Rerun `kodawari work all` with `--prd <path>`.",
        extra={"_rc": 2, "status": "BLOCKED"},
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def run_work_command(args: argparse.Namespace) -> int:
    missing = _ensure_feature(args, command="work")
    if missing is not None:
        return missing
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=getattr(args, "planning_dir", None),
    )
    rc, payload = invoke_cli_handler(run_autopilot_command, args)
    wrapped = dict(payload)
    wrapped.update(
        {
            "_rc": int(rc),
            "entrypoint": "kodawari work",
            "canonical_command": "kodawari autopilot",
            "planning_dir": str(planning_dir),
            "provenance": _build_cli_provenance(
                command="work",
                project_root=project_root,
                planning_dir=planning_dir,
            ),
        }
    )
    _write_json_output(getattr(args, "output", None), wrapped)
    print(json.dumps(wrapped, ensure_ascii=False, indent=2))
    return int(wrapped.get("_rc", 0) or 0)


def run_work_all_facade_command(args: argparse.Namespace) -> int:
    missing = _ensure_feature(args, command="work all")
    if missing is not None:
        return missing
    prd_missing = _ensure_work_all_prd(args)
    if prd_missing is not None:
        return prd_missing
    return run_work_all_command(args)


__all__ = ["run_work_all_facade_command", "run_work_command"]


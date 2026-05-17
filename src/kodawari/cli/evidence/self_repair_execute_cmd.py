"""CLI command for Phase 3 self-repair execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kodawari.cli.evidence.self_repair import SELF_REPAIR_FILENAME
from kodawari.cli.evidence.self_repair_execute import (
    execute_self_repair_proposal,
    write_execution_record,
)
from kodawari.cli.main_support import _resolve_feature_planning_dir


def run_self_repair_execute_command(args: argparse.Namespace) -> int:
    proposal_path = _resolve_proposal_path(args)
    if proposal_path is None:
        return 2
    sdk_root_arg = getattr(args, "sdk_root", None)
    record = execute_self_repair_proposal(
        proposal_path=proposal_path,
        sdk_root=Path(sdk_root_arg).resolve() if sdk_root_arg else None,
        dry_run=bool(getattr(args, "dry_run", False)),
        confidence_min=getattr(args, "confidence_min", None),
    )
    if bool(getattr(args, "write", False)):
        write_target = proposal_path.parent if proposal_path.is_file() else proposal_path
        record["artifact"] = str(write_execution_record(write_target, record))
    print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if record.get("status") in {"executed", "dry_run"} else 1


def _resolve_proposal_path(args: argparse.Namespace) -> Path | None:
    proposal_arg = getattr(args, "proposal", None)
    if proposal_arg:
        return Path(proposal_arg).resolve()
    project_root = Path(getattr(args, "project_root", ".") or ".").resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    planning_dir_arg = getattr(args, "planning_dir", None)
    if not (feature or planning_dir_arg):
        print(
            json.dumps(
                {
                    "schema_version": "workflow.self_repair.cli_error.v1",
                    "status": "error",
                    "reason": "proposal_or_feature_or_planning_dir_required",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return None
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=planning_dir_arg,
    )
    return planning_dir / SELF_REPAIR_FILENAME


__all__ = ["run_self_repair_execute_command"]

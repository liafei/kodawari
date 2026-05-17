"""CLI command for Phase 4 self-repair learning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kodawari.cli.evidence.self_repair_execute import SELF_REPAIR_EXECUTION_FILENAME
from kodawari.cli.evidence.self_repair_learn import learn_from_self_repair
from kodawari.cli.main_support import _resolve_feature_planning_dir


def run_self_repair_learn_command(args: argparse.Namespace) -> int:
    project_root = Path(getattr(args, "project_root", ".") or ".").resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    planning_dir_arg = getattr(args, "planning_dir", None)
    execution_record_arg = getattr(args, "execution_record", None)

    if execution_record_arg:
        execution_record_path = Path(execution_record_arg).resolve()
    elif feature or planning_dir_arg:
        planning_dir = _resolve_feature_planning_dir(
            project_root=project_root,
            feature=feature,
            planning_dir=planning_dir_arg,
        )
        execution_record_path = planning_dir / SELF_REPAIR_EXECUTION_FILENAME
    else:
        payload = {
            "schema_version": "workflow.self_repair.cli_error.v1",
            "status": "error",
            "reason": "execution_record_or_feature_or_planning_dir_required",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    target_after_arg = getattr(args, "target_after", None)
    target_after_planning_dir = Path(target_after_arg).resolve() if target_after_arg else None

    sdk_root_arg = getattr(args, "sdk_root", None)
    sdk_root = Path(sdk_root_arg).resolve() if sdk_root_arg else None

    project_for_lesson_arg = getattr(args, "lesson_project_root", None)
    project_root_for_lesson = Path(project_for_lesson_arg).resolve() if project_for_lesson_arg else None

    result = learn_from_self_repair(
        execution_record_path=execution_record_path,
        target_after_planning_dir=target_after_planning_dir,
        sdk_root=sdk_root,
        project_root_for_lesson=project_root_for_lesson,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


__all__ = ["run_self_repair_learn_command"]

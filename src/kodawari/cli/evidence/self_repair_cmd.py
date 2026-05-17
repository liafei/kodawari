"""CLI command for generating workflow self-repair proposals."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kodawari.cli.evidence.self_repair import (
    build_self_repair_proposal,
    write_self_repair_markdown,
    write_self_repair_proposal,
)
from kodawari.cli.main_support import _resolve_feature_planning_dir
from kodawari.infra.io_atomic import atomic_write_canonical_json


def run_self_repair_command(args: argparse.Namespace) -> int:
    project_root = Path(getattr(args, "project_root", ".") or ".").resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    planning_dir_arg = getattr(args, "planning_dir", None)
    if not feature and not planning_dir_arg:
        payload = {
            "schema_version": "workflow.self_repair.cli_error.v1",
            "status": "error",
            "reason": "feature_or_planning_dir_required",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=planning_dir_arg,
    )
    payload = build_self_repair_proposal(project_root=project_root, planning_dir=planning_dir)
    if bool(getattr(args, "write", False)):
        artifact_path = write_self_repair_proposal(planning_dir, payload)
        payload["artifact"] = str(artifact_path)
        if bool(getattr(args, "markdown", False)):
            markdown_path = write_self_repair_markdown(planning_dir, payload)
            payload["markdown_artifact"] = str(markdown_path)
    output = str(getattr(args, "output", "") or "").strip()
    if output:
        atomic_write_canonical_json(Path(output).resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if str(payload.get("status") or "") == "ready" else 1


__all__ = ["run_self_repair_command"]

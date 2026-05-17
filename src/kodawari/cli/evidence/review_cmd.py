"""Review facade built on canonical review + verify artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.cli.delivery.delivery_cmds import _cmd_review, _cmd_verify
from kodawari.cli.core.legacy_runtime_invocation import invoke_cli_handler, legacy_step_result
from kodawari.cli.main_support import _build_cli_provenance, _resolve_feature_planning_dir, _write_json_output


def _emit(payload: dict[str, Any], *, output: str | None) -> int:
    _write_json_output(output, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return int(payload.get("_rc", 0) or 0)


def _combined_status(*payloads: dict[str, Any]) -> str:
    statuses = [str(item.get("status") or "").upper() for item in payloads if isinstance(item, dict)]
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if any(status == "BLOCKED" for status in statuses):
        return "BLOCKED"
    return "PASS"


def run_review_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=getattr(args, "planning_dir", None),
    )
    review_ns = argparse.Namespace(
        project_root=str(project_root),
        feature=feature,
        planning_dir=str(planning_dir),
        base_branch=str(getattr(args, "base_branch", "main") or "main"),
        changed_file=list(getattr(args, "changed_file", []) or []),
        scope_allow=list(getattr(args, "scope_allow", []) or []),
        output=None,
        fail_on_block=False,
    )
    verify_ns = argparse.Namespace(
        project_root=str(project_root),
        feature=feature,
        planning_dir=str(planning_dir),
        base_branch=str(getattr(args, "base_branch", "main") or "main"),
        changed_file=list(getattr(args, "changed_file", []) or []),
        command_file=getattr(args, "command_file", None),
        command=getattr(args, "command", None),
        output=None,
        fail_on_block=False,
    )
    review_rc, review_payload = invoke_cli_handler(_cmd_review, review_ns)
    verify_rc, verify_payload = invoke_cli_handler(_cmd_verify, verify_ns)
    status = _combined_status(review_payload, verify_payload)
    payload = dict(review_payload)
    payload.update(
        {
            "_rc": 0 if review_rc == 0 and verify_rc == 0 and status == "PASS" else int(review_rc or verify_rc or 2),
            "status": status,
            "entrypoint": "kodawari review",
            "canonical_command": "kodawari review + kodawari verify",
            "planning_dir": str(planning_dir),
            "verify": dict(verify_payload),
            "verify_status": str(verify_payload.get("status") or "").upper(),
            "verify_source": str(verify_payload.get("verify_report_source") or verify_payload.get("source") or ""),
            "artifacts": {
                **dict(review_payload.get("artifacts") or {}),
                **dict(verify_payload.get("artifacts") or {}),
            },
            "steps": [
                legacy_step_result(name="review", rc=review_rc, payload=review_payload),
                legacy_step_result(name="verify", rc=verify_rc, payload=verify_payload),
            ],
            "provenance": _build_cli_provenance(
                command="review",
                project_root=project_root,
                planning_dir=planning_dir,
            ),
        }
    )
    if status != "PASS" and not str(payload.get("blocking_reason") or "").strip():
        payload["blocking_reason"] = str(
            verify_payload.get("blocking_reason")
            or review_payload.get("blocking_reason")
            or verify_payload.get("summary")
            or review_payload.get("summary")
            or "review facade blocked"
        )
    return _emit(payload, output=getattr(args, "output", None))


__all__ = ["run_review_command"]


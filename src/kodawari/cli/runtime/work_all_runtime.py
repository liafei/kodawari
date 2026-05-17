"""Canonical runtime for `kodawari work all`."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kodawari.cli.runtime.autopilot_cmd import run_autopilot_command
from kodawari.cli.runtime.autopilot_interaction_state import InteractionState
from kodawari.cli.io_atomic import atomic_write_json, load_json_dict
from kodawari.cli.core.legacy_runtime_invocation import invoke_cli_handler
from kodawari.cli.core.main_support import _build_cli_provenance, _resolve_feature_planning_dir
from kodawari.cli.contract.plan_cmd import run_plan_command
from kodawari.cli.delivery.release_cmd import run_release_command
from kodawari.cli.evidence.review_cmd import run_review_command


WORK_ALL_MANIFEST_FILENAME = ".work_all_manifest.json"
WORK_ALL_MANIFEST_VERSION = "workflow.work_all_manifest.v2"


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _step_status(payload: dict[str, Any], rc: int, *, step_name: str = "") -> str:
    status = str(payload.get("status") or ("PASS" if rc == 0 else "FAIL")).upper()
    if status == "OK":
        status = "PASS"
    if str(step_name or "") == "work" and status == "PASS":
        interaction = str(payload.get("interaction_state") or "").upper()
        if interaction == InteractionState.AWAITING_DECISION.value:
            return InteractionState.AWAITING_DECISION.value
        if interaction in {InteractionState.AWAITING_ENVIRONMENT.value, InteractionState.BLOCKED.value}:
            return "BLOCKED"
    return status


def _manifest_path(planning_dir: Path) -> Path:
    return planning_dir / WORK_ALL_MANIFEST_FILENAME


def _load_manifest(planning_dir: Path) -> dict[str, Any]:
    payload = load_json_dict(_manifest_path(planning_dir), required=False)
    return payload if isinstance(payload, dict) else {}


def _step_record(name: str, rc: int, payload: dict[str, Any], *, skipped: bool = False, reason: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": _step_status(payload, rc, step_name=name) if not skipped else "SKIPPED",
        "rc": int(rc),
        "skipped": bool(skipped),
        "reason": reason,
        "summary": str(payload.get("summary") or payload.get("blocking_reason") or ""),
        "interaction_state": str(payload.get("interaction_state") or ""),
        "next_action_type": str(payload.get("next_action_type") or ""),
        "artifacts": dict(payload.get("artifacts") or {}),
        "generated_at": _utc_now_iso(),
    }


def _resume_skip(step_name: str, manifest: dict[str, Any], *, force_rerun: bool) -> dict[str, Any] | None:
    if force_rerun:
        return None
    for item in list(manifest.get("steps") or []):
        step = dict(item or {})
        if str(step.get("name") or "") != step_name:
            continue
        status = str(step.get("status") or "").upper()
        if status == "PASS":
            return step
        if status == "SKIPPED" and bool(step.get("skipped")) and str(step.get("reason") or "") == "resume_skip":
            return step
    return None


def _should_stop(name: str, payload: dict[str, Any], rc: int) -> bool:
    status = _step_status(payload, rc, step_name=name)
    if status in {"FAIL", "BLOCKED"}:
        return True
    if status == "AWAITING_DECISION":
        return True
    if name == "work":
        interaction = str(payload.get("interaction_state") or "").upper()
        if interaction in {
            InteractionState.AWAITING_DECISION.value,
            InteractionState.AWAITING_ENVIRONMENT.value,
            InteractionState.BLOCKED.value,
        }:
            return True
    return False


def run_work_all_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=getattr(args, "planning_dir", None),
    )
    planning_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = _manifest_path(planning_dir)
    previous_manifest = _load_manifest(planning_dir)
    force_rerun = bool(getattr(args, "force_rerun", False))
    steps: list[dict[str, Any]] = []
    final_rc = 0
    final_status = "PASS"
    work_namespace = argparse.Namespace(**vars(args))
    # `work all --replan` means "refresh planning before work starts".  The
    # work/autopilot step must consume the freshly materialized TASK_GRAPH
    # instead of triggering a second model-planning conversation.
    setattr(work_namespace, "replan", False)
    step_specs = (
        (
            "plan",
            run_plan_command,
            argparse.Namespace(
                project_root=str(project_root),
                feature=feature,
                planning_dir=str(planning_dir),
                task=str(getattr(args, "task", "") or ""),
                prd=getattr(args, "prd", None),
                requirements_file=getattr(args, "requirements_file", None),
                planner_route=str(getattr(args, "planner_route", "auto") or "auto"),
                replan=bool(getattr(args, "replan", False)),
            ),
        ),
        (
            "work",
            run_autopilot_command,
            work_namespace,
        ),
        (
            "review",
            run_review_command,
            argparse.Namespace(
                project_root=str(project_root),
                feature=feature,
                planning_dir=str(planning_dir),
                base_branch=str(getattr(args, "base_branch", "main") or "main"),
                changed_file=list(getattr(args, "changed_file", []) or []),
                scope_allow=list(getattr(args, "scope_allow", []) or []),
                command_file=getattr(args, "command_file", None),
                command=getattr(args, "command", None),
                output=None,
                fail_on_block=False,
            ),
        ),
        (
            "release",
            run_release_command,
            argparse.Namespace(
                project_root=str(project_root),
                feature=feature,
                planning_dir=str(planning_dir),
                eval_report_path=getattr(args, "eval_report_path", None),
                auto_eval=bool(getattr(args, "auto_eval", False)),
                risk_profile=str(getattr(args, "risk_profile", "medium") or "medium"),
                gate_profile=str(getattr(args, "release_gate_profile", "strict") or "strict"),
                gate_path=list(getattr(args, "release_gate_path", []) or ["src"]),
                output=None,
                fail_on_block=False,
            ),
        ),
    )
    for name, handler, namespace in step_specs:
        skipped = _resume_skip(name, previous_manifest, force_rerun=force_rerun)
        if skipped is not None:
            steps.append(_step_record(name, 0, {"status": "PASS", "summary": str(skipped.get("summary") or "")}, skipped=True, reason="resume_skip"))
            continue
        rc, payload = invoke_cli_handler(handler, namespace)
        step = _step_record(name, rc, payload)
        steps.append(step)
        final_rc = int(rc)
        final_status = str(step.get("status") or "FAIL")
        atomic_write_json(
            manifest_path,
            {
                "schema_version": WORK_ALL_MANIFEST_VERSION,
                "entrypoint": "kodawari work all",
                "feature": feature,
                "project_root": str(project_root),
                "planning_dir": str(planning_dir),
                "requested_at": _utc_now_iso(),
                "requested_prd": str(getattr(args, "prd", None) or getattr(args, "requirements_file", None) or "").strip(),
                "executor_backend": str(getattr(args, "executor_backend", "") or ""),
                "self_review_backend": str(getattr(args, "self_review_backend", "") or ""),
                "steps": steps,
                "updated_at": _utc_now_iso(),
            },
        )
        if _should_stop(name, payload, rc):
            break
    payload = {
        "_rc": int(final_rc),
        "status": final_status,
        "entrypoint": "kodawari work all",
        "feature": feature,
        "project_root": str(project_root),
        "planning_dir": str(planning_dir),
        "manifest_path": str(manifest_path),
        "resume_supported": True,
        "steps": steps,
        "summary": "work all completed" if final_status == "PASS" else "work all stopped before terminal pass",
        "provenance": _build_cli_provenance(
            command="work all",
            project_root=project_root,
            planning_dir=planning_dir,
        ),
    }
    atomic_write_json(
        manifest_path,
        {
            "schema_version": WORK_ALL_MANIFEST_VERSION,
            **payload,
            "updated_at": _utc_now_iso(),
        },
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return int(payload.get("_rc", 0) or 0)


__all__ = ["WORK_ALL_MANIFEST_FILENAME", "WORK_ALL_MANIFEST_VERSION", "run_work_all_command"]


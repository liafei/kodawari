"""Canonical runtime for `kodawari work all`."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.prd_contract import extract_prd_slices
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
MULTI_SLICE_STATE_FILENAME = ".multi_slice_state.json"
MULTI_SLICE_STATE_VERSION = "workflow.multi_slice_state.v1"


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


def _read_prd_slices(prd_path: str | None) -> list[dict[str, Any]]:
    """Return the slice list declared in the PRD, or [] for single-slice PRDs."""
    if not prd_path:
        return []
    try:
        text = Path(prd_path).read_text(encoding="utf-8")
    except OSError:
        return []
    return extract_prd_slices(text)


def _multi_slice_state_path(planning_dir: Path) -> Path:
    return planning_dir / MULTI_SLICE_STATE_FILENAME


def _load_multi_slice_state(planning_dir: Path) -> dict[str, Any]:
    payload = load_json_dict(_multi_slice_state_path(planning_dir), required=False)
    return payload if isinstance(payload, dict) else {}


def _save_multi_slice_state(
    planning_dir: Path,
    *,
    feature: str,
    slices: list[dict[str, Any]],
    completed_positions: list[int],
    current_position: int | None,
    status: str,
) -> None:
    payload = {
        "schema_version": MULTI_SLICE_STATE_VERSION,
        "feature": feature,
        "total_slices": len(slices),
        "completed_positions": sorted(set(completed_positions)),
        "current_position": current_position,
        "status": status,
        "slices": [
            {
                "position": int(s.get("position", idx)),
                "declared_index": int(s.get("declared_index", idx + 1)),
                "title": str(s.get("title") or f"slice {idx + 1}"),
            }
            for idx, s in enumerate(slices)
        ],
        "updated_at": _utc_now_iso(),
    }
    atomic_write_json(_multi_slice_state_path(planning_dir), payload)


def _write_slice_prd(
    *,
    slice_dir: Path,
    feature: str,
    slice_info: dict[str, Any],
    total: int,
) -> Path:
    """Write a single-slice PRD that the planner sees as the unit-of-work
    for this slice. We include a small header pointing back at the full
    PRD context so the planner doesn't lose the project-level framing."""
    slice_dir.mkdir(parents=True, exist_ok=True)
    path = slice_dir / "PRD_SLICE.md"
    position = int(slice_info.get("position", 0))
    title = str(slice_info.get("title") or f"slice {position + 1}")
    body = str(slice_info.get("content") or "").strip()
    text = (
        f"# {feature} — slice {position + 1}/{total}: {title}\n\n"
        f"> This is slice {position + 1} of {total} from a multi-slice PRD. "
        f"Focus on the deliverables declared in this slice only; other slices "
        f"will be planned and shipped independently.\n\n"
        f"{body}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path


def run_work_all_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    feature = str(args.feature).strip()
    planning_dir = _resolve_feature_planning_dir(
        project_root=project_root,
        feature=feature,
        planning_dir=getattr(args, "planning_dir", None),
    )
    planning_dir.mkdir(parents=True, exist_ok=True)

    # Multi-slice detection (Stage E1: epic replan). When the PRD declares
    # ## Slice N: / ## 切片 N: markers, run plan+work once per slice with a
    # slice-specific planning_dir, then a single final review+release at the
    # parent level. PRDs without slice markers fall through to the historical
    # single-slice path unchanged.
    prd_path = getattr(args, "prd", None) or getattr(args, "requirements_file", None)
    slices = _read_prd_slices(prd_path)
    if slices:
        return _run_work_all_multi_slice(
            args=args,
            project_root=project_root,
            feature=feature,
            planning_dir=planning_dir,
            prd_path=prd_path,
            slices=slices,
        )

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


def _run_work_all_multi_slice(
    *,
    args: argparse.Namespace,
    project_root: Path,
    feature: str,
    planning_dir: Path,
    prd_path: str,
    slices: list[dict[str, Any]],
) -> int:
    """Run plan + work for each PRD slice in sequence, then a single final
    review + release at the parent planning_dir level.

    Resume semantics: each slice's completion is recorded in
    ``.multi_slice_state.json``. On re-invocation, completed slices are
    skipped unless ``--force-rerun`` was passed. A failure on slice N
    halts the loop and writes the failure into the multi-slice state so
    the user can inspect / fix / resume.
    """
    state = _load_multi_slice_state(planning_dir)
    force_rerun = bool(getattr(args, "force_rerun", False))
    completed: list[int] = [] if force_rerun else list(state.get("completed_positions") or [])

    slice_records: list[dict[str, Any]] = []
    final_rc = 0
    final_status = "PASS"
    halted_slice: int | None = None

    for slice_info in slices:
        position = int(slice_info.get("position", 0))
        title = str(slice_info.get("title") or f"slice {position + 1}")
        # Per-slice feature name. The autopilot derives its planning_dir from
        # feature alone (autopilot_runtime_flow.resolve_planning_paths), so
        # using a unique feature suffix is the only way to give each slice an
        # independent planning_dir — overriding --planning-dir alone is NOT
        # enough because the work step's autopilot ignores it. Discovered
        # during multi-slice实战 verification on greenfield URL-shortener.
        slice_feature = f"{feature}_slice_{position:02d}"
        slice_dir = (project_root / "planning" / slice_feature).resolve()

        if position in completed and not force_rerun:
            slice_records.append({
                "position": position,
                "title": title,
                "slice_feature": slice_feature,
                "status": "SKIPPED",
                "skipped": True,
                "reason": "resume_skip_prior_pass",
            })
            continue

        _save_multi_slice_state(
            planning_dir,
            feature=feature,
            slices=slices,
            completed_positions=completed,
            current_position=position,
            status="running",
        )

        slice_prd_path = _write_slice_prd(
            slice_dir=slice_dir,
            feature=feature,
            slice_info=slice_info,
            total=len(slices),
        )

        # Plan step (slice-scoped: uses slice_feature so the autopilot work
        # step that follows can derive a matching planning_dir).
        plan_args = argparse.Namespace(
            project_root=str(project_root),
            feature=slice_feature,
            planning_dir=str(slice_dir),
            task=f"Slice {position + 1}/{len(slices)}: {title}",
            prd=str(slice_prd_path),
            requirements_file=None,
            planner_route=str(getattr(args, "planner_route", "auto") or "auto"),
            replan=bool(getattr(args, "replan", False)),
        )
        plan_rc, plan_payload = invoke_cli_handler(run_plan_command, plan_args)
        plan_record = _step_record("plan", plan_rc, plan_payload)

        if plan_rc != 0 or _should_stop("plan", plan_payload, plan_rc):
            slice_records.append({
                "position": position,
                "title": title,
                "slice_feature": slice_feature,
                "status": "FAIL",
                "step": "plan",
                "rc": int(plan_rc),
                "details": plan_record,
            })
            final_rc = int(plan_rc) or 1
            final_status = "FAIL"
            halted_slice = position
            break

        # Work step (slice-scoped) — reuse the full args but override
        # feature so autopilot derives its planning_dir to slice_dir.
        work_args = argparse.Namespace(**vars(args))
        setattr(work_args, "feature", slice_feature)
        setattr(work_args, "planning_dir", str(slice_dir))
        setattr(work_args, "prd", str(slice_prd_path))
        setattr(work_args, "task", f"Slice {position + 1}/{len(slices)}: {title}")
        setattr(work_args, "replan", False)
        work_rc, work_payload = invoke_cli_handler(run_autopilot_command, work_args)
        work_record = _step_record("work", work_rc, work_payload)

        slice_records.append({
            "position": position,
            "title": title,
            "slice_feature": slice_feature,
            "status": work_record["status"],
            "step": "work",
            "rc": int(work_rc),
            "plan": plan_record,
            "work": work_record,
        })

        if _should_stop("work", work_payload, work_rc):
            final_rc = int(work_rc) or 1
            final_status = work_record["status"]
            halted_slice = position
            break

        completed.append(position)

    if halted_slice is None:
        # All slices done — record final state then run parent-level
        # review + release once across the whole feature.
        _save_multi_slice_state(
            planning_dir,
            feature=feature,
            slices=slices,
            completed_positions=completed,
            current_position=None,
            status="all_slices_complete",
        )
        review_args = argparse.Namespace(
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
        )
        review_rc, review_payload = invoke_cli_handler(run_review_command, review_args)
        release_args = argparse.Namespace(
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
        )
        release_rc, release_payload = invoke_cli_handler(run_release_command, release_args)
        final_rc = int(release_rc) or int(review_rc) or 0
        final_status = _step_status(release_payload, release_rc, step_name="release")
        slice_records.append({"position": -1, "title": "review", "step": "review", "rc": int(review_rc), "status": _step_status(review_payload, review_rc, step_name="review")})
        slice_records.append({"position": -1, "title": "release", "step": "release", "rc": int(release_rc), "status": final_status})
    else:
        _save_multi_slice_state(
            planning_dir,
            feature=feature,
            slices=slices,
            completed_positions=completed,
            current_position=halted_slice,
            status="halted",
        )

    payload = {
        "_rc": int(final_rc),
        "status": final_status,
        "entrypoint": "kodawari work all",
        "feature": feature,
        "project_root": str(project_root),
        "planning_dir": str(planning_dir),
        "manifest_path": str(_manifest_path(planning_dir)),
        "multi_slice_state_path": str(_multi_slice_state_path(planning_dir)),
        "resume_supported": True,
        "multi_slice": True,
        "total_slices": len(slices),
        "completed_slices": sorted(set(completed)),
        "halted_slice": halted_slice,
        "slices": slice_records,
        "summary": (
            "work all completed all slices"
            if halted_slice is None
            else f"work all halted at slice {halted_slice}"
        ),
        "provenance": _build_cli_provenance(
            command="work all",
            project_root=project_root,
            planning_dir=planning_dir,
        ),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return final_rc


__all__ = ["WORK_ALL_MANIFEST_FILENAME", "WORK_ALL_MANIFEST_VERSION", "run_work_all_command"]


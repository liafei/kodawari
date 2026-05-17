"""Telemetry command implementation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.planning.effort_scoring import score_effort_profile
from kodawari.cli.artifact_versions import ArtifactSchemaVersionError, load_versioned_artifact
from kodawari.cli.contract.command_contract import normalize_mutating_payload
from kodawari.cli.io_atomic import CorruptArtifactError
from kodawari.cli.contract.planning_conversation_compat import load_prd_intake_compatible
from kodawari.cli.evidence.observability_store import (
    SNAPSHOT_SCHEMA_VERSION,
    SchemaValidationError,
    _append_jsonl,
    _build_provenance,
    _count_recent_history_events,
    _error_payload,
    _int_or_none,
    _int_or_zero,
    _load_json_dict_optional,
    _load_jsonl_dict_rows,
    _now_iso,
    _print_schema_error,
    _resolve_planning_dir,
    _validate_observability_payload,
    _write_json,
    _write_optional_json_output,
)
from kodawari.cli.runtime.runtime_metrics import count_peer_review_rounds


def _error_events_count(state: dict[str, Any]) -> int:
    events = state.get("error_events")
    if isinstance(events, list):
        return len(events)
    return 0


def _workflow_chain_verify_status(workflow_chain: dict[str, Any]) -> str:
    upstream = dict(workflow_chain.get("upstream") or {})
    verify = dict(upstream.get("verify") or {})
    status = str(verify.get("status") or "").strip()
    if status:
        return status
    final_outcome = dict(workflow_chain.get("final_outcome") or {})
    return str(final_outcome.get("status") or "UNKNOWN")


def _latest_round_outcome(rounds: list[dict[str, Any]]) -> str:
    for row in reversed(rounds):
        stage_status = str(row.get("stage_status") or "").strip()
        if stage_status:
            return stage_status
    return ""


def _effort_profile_from_runtime(
    *,
    planning_dir: Path,
    state: dict[str, Any],
    changed_files: list[str],
) -> dict[str, Any]:
    task_card = _load_json_dict_optional(planning_dir / "TASK_CARD_ACTIVE.json") or {}
    repo_inventory = _load_json_dict_optional(planning_dir / "REPO_INVENTORY.json") or {}
    planning_context = load_prd_intake_compatible(planning_dir) or {}
    requirements = "\n".join(
        part
        for part in (
            str(planning_context.get("business_outcome") or "").strip(),
            " ".join(str(item) for item in list(planning_context.get("out_of_scope") or []) if str(item).strip()),
            " ".join(str(item) for item in list(task_card.get("invariants") or []) if str(item).strip()),
        )
        if part
    )
    return score_effort_profile(
        task_label=str(state.get("active_task") or task_card.get("task_name") or "").strip(),
        task_scope=str(task_card.get("task_name") or "").strip(),
        requirements=requirements,
        task_card=task_card,
        changed_files=changed_files,
        prior_failures=len(list(state.get("error_events") or [])),
        project_model={
            "surface": str(task_card.get("layer") or "").strip(),
            "capabilities": [str(item) for item in list(repo_inventory.get("capabilities") or []) if str(item).strip()],
        },
    )


def run_telemetry_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    try:
        planning_dir, feature = _resolve_planning_dir(
            project_root=project_root,
            feature=getattr(args, "feature", None),
            planning_dir=getattr(args, "planning_dir", None),
        )
        state = load_versioned_artifact(planning_dir / ".autopilot_state.json")
        rounds = _load_jsonl_dict_rows(planning_dir / ".autopilot_rounds.jsonl")
        workflow_chain = _load_json_dict_optional(planning_dir / ".workflow_chain.json") or {}
        gate_result = _load_json_dict_optional(planning_dir / ".gate_result.json") or {}
        max_history_days = _int_or_none(getattr(args, "max_history_days", None))
        changed_files_raw = state.get("changed_files")
        changed_files = changed_files_raw if isinstance(changed_files_raw, list) else changed_files_raw
        changed_files_list = [str(item) for item in list(changed_files_raw or []) if str(item).strip()] if isinstance(changed_files_raw, list) else []
        effort_profile = _effort_profile_from_runtime(
            planning_dir=planning_dir,
            state=state,
            changed_files=changed_files_list,
        )
        metrics = {
            "tokens_used": _int_or_zero(state.get("tokens_used")),
            "cycle": _int_or_zero(state.get("cycle")),
            "changed_files_count": len(changed_files_raw) if isinstance(changed_files_raw, list) else 0,
            "error_events_count": _error_events_count(state),
            "review_rounds_used": count_peer_review_rounds(rounds),
            "history_events_considered": _count_recent_history_events(
                planning_dir / ".telemetry_events.jsonl",
                max_history_days=max_history_days,
            ),
        }
        status = state.get("final_status") or state.get("stop_reason") or "UNKNOWN"
        snapshot = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "captured_at": _now_iso(),
            "feature": feature,
            "run_id": planning_dir.name,
            "status": status,
            "metrics": metrics,
            "signals": {
                "stop_reason": state.get("stop_reason") or "",
                "gate_status": str(gate_result.get("total_status") or "UNKNOWN"),
                "verify_status": _workflow_chain_verify_status(workflow_chain),
                "round_outcome": _latest_round_outcome(rounds),
                "reasoning_tier": str(effort_profile.get("tier") or "economy"),
                "effort_score": int(effort_profile.get("score") or 0),
                "effort_reasons": [str(item) for item in list(effort_profile.get("reasons") or []) if str(item).strip()],
            },
            "source_artifacts": {
                "autopilot_state": str((planning_dir / ".autopilot_state.json").resolve()),
                "autopilot_rounds": str((planning_dir / ".autopilot_rounds.jsonl").resolve()),
                "workflow_chain": str((planning_dir / ".workflow_chain.json").resolve()),
                "gate_result": str((planning_dir / ".gate_result.json").resolve()),
            },
            "changed_files": changed_files,
        }
        _validate_observability_payload("telemetry_snapshot", snapshot)
        snapshot_path = planning_dir / ".telemetry_snapshot.json"
        _write_json(snapshot_path, snapshot)
        if bool(getattr(args, "append_history", True)):
            history_row = {
                "captured_at": snapshot["captured_at"],
                "run_id": snapshot["run_id"],
                "feature": snapshot["feature"],
                "status": str(snapshot["status"]),
                "metrics": dict(snapshot["metrics"]),
                "snapshot_path": str(snapshot_path.resolve()),
            }
            _append_jsonl(planning_dir / ".telemetry_events.jsonl", history_row)
        payload = {
            "status": str(snapshot.get("status") or "UNKNOWN").upper(),
            "feature": feature,
            "planning_dir": str(planning_dir),
            "snapshot_path": str(snapshot_path.resolve()),
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "metrics": metrics,
            "reasoning_tier": str(effort_profile.get("tier") or "economy"),
            "effort_score": int(effort_profile.get("score") or 0),
            "effort_reasons": [str(item) for item in list(effort_profile.get("reasons") or []) if str(item).strip()],
            "provenance": _build_provenance(
                command="telemetry",
                project_root=project_root,
                planning_dir=planning_dir,
            ),
        }
        normalized_payload = normalize_mutating_payload(payload)
        _write_optional_json_output(normalized_payload, getattr(args, "output", None), project_root=project_root)
        print(json.dumps(normalized_payload, ensure_ascii=False, indent=2))
        return 0
    except SchemaValidationError as exc:
        return _print_schema_error(
            command="telemetry",
            project_root=project_root,
            planning_dir=None,
            schema_name=exc.schema_name,
            errors=exc.errors,
        )
    except ArtifactSchemaVersionError as exc:
        payload = _error_payload(
            command="telemetry",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="artifact_schema_version_invalid",
            remediation=[
                "Run `kodawari migrate-artifacts --project-root <root> --feature <feature>` before rerunning telemetry."
            ],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except CorruptArtifactError as exc:
        remediation = ["Inspect or regenerate the quarantined artifact before rerunning telemetry."]
        if exc.quarantine_path is not None:
            remediation.append(f"Quarantined copy: {exc.quarantine_path}")
        payload = _error_payload(
            command="telemetry",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="artifact_corrupt",
            remediation=remediation,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except ValueError as exc:
        payload = _error_payload(
            command="telemetry",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="telemetry_failed",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


__all__ = ["run_telemetry_command"]



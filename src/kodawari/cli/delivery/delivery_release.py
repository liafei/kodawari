"""QA and ship-readiness workflow helpers."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.execution.execution_artifacts import (
    EXECUTION_RESULT_FILENAME,
    ExecutionArtifactError,
    load_execution_result,
)
from kodawari.cli.artifact_versions import ArtifactSchemaVersionError
from kodawari.cli.delivery.delivery_common import (
    DEFAULT_CANARY_GATE_RESULT,
    DEFAULT_REPLAY_GATE_RESULT,
    VERIFY_REPORT_FILENAME,
    _attach_payload_digest,
    _ensure_design_artifact,
    _ensure_placeholder_markdown,
    _load_contract_task_graph,
    _load_json_dict,
    _load_verify_report,
    _planning_artifact_mode,
    _required_planning_artifacts_status,
    _task_run_payload,
    _utc_now_iso,
    _write_json,
)
from kodawari.cli.delivery.delivery_evidence import (
    _must_fix_items,
    _resolve_gate_check,
    _review_evidence,
    _review_evidence_check,
    _verify_status,
    _workflow_chain_review_status,
)
from kodawari.cli.io_atomic import atomic_write_text
from kodawari.cli.main_support import _build_cli_provenance
from kodawari.cli.delivery.release_surface_consistency import build_surface_consistency_checks
from kodawari.cli.evidence.verify_report import VerifyReportSchemaValidationError
from kodawari.cli.delivery.workflow_chain import load_workflow_chain_snapshot

RISK_PROFILE_RULES: dict[str, dict[str, bool]] = {
    "low": {
        "require_explicit_review_evidence": False,
        "block_auto_eval": False,
        "block_eval_warnings": False,
        "block_fallback_verify_inputs": False,
    },
    "medium": {
        "require_explicit_review_evidence": True,
        "block_auto_eval": False,
        "block_eval_warnings": False,
        "block_fallback_verify_inputs": True,
    },
    "high": {
        "require_explicit_review_evidence": True,
        "block_auto_eval": True,
        "block_eval_warnings": True,
        "block_fallback_verify_inputs": True,
    },
}


def _contract_layer_boundary_debt(planning_dir: Path) -> dict[str, Any]:
    task_graph = _load_contract_task_graph(planning_dir) or {}
    payload = dict(task_graph.get("boundary_debt") or {})
    if payload:
        return {
            "status": str(payload.get("status") or "PASS").upper(),
            "details": str(payload.get("details") or ""),
            "items": list(payload.get("items") or []),
        }
    return {
        "status": "PASS",
        "details": "No physical layer-boundary debt detected.",
        "items": [],
    }


def _resolve_verify_check(
    *,
    planning_dir: Path,
    workflow_chain: dict[str, Any],
    semantic_compact: dict[str, Any] | None,
    state_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    verify_report_path = planning_dir / VERIFY_REPORT_FILENAME
    if verify_report_path.exists():
        try:
            verify_report = _load_verify_report(planning_dir) or {}
        except (ArtifactSchemaVersionError, ValueError, VerifyReportSchemaValidationError) as exc:
            return {
                "status": "FAIL",
                "verify_status": "INVALID",
                "source": VERIFY_REPORT_FILENAME,
                "reason": f"invalid verify artifact: {exc}",
            }
        verify_payload = dict(verify_report.get("verify_check") or {})
        verify_status = str(verify_payload.get("status") or verify_report.get("status") or "UNKNOWN").upper()
        return {
            "status": "PASS" if verify_status == "PASS" else "FAIL",
            "verify_status": verify_status,
            "source": VERIFY_REPORT_FILENAME,
            "reason": (
                ""
                if verify_status == "PASS"
                else str(verify_payload.get("blocking_reason") or verify_payload.get("summary") or verify_status)
            ),
            "input_confidence": str(verify_report.get("input_confidence") or ""),
            "requested_command_kind": str(verify_report.get("requested_command_kind") or ""),
            "changed_files": [
                str(item)
                for item in list(dict(verify_report.get("changed_files") or {}).get("items") or [])
                if str(item).strip()
            ],
            "surface_results": [dict(item) for item in list(verify_report.get("surface_results") or []) if isinstance(item, dict)],
            "surface_summary": dict(verify_report.get("surface_summary") or {}),
            "verify_scope_mode": str(verify_report.get("verify_scope_mode") or ""),
        }
    task_run = _task_run_payload(planning_dir) or {}
    verify_payload = dict(task_run.get("verify_check") or {})
    if verify_payload:
        verify_status = str(verify_payload.get("status") or "UNKNOWN").upper()
        return {
            "status": "PASS" if verify_status == "PASS" else "FAIL",
            "verify_status": verify_status,
            "source": ".task_run_result.json.verify_check",
            "reason": "" if verify_status == "PASS" else str(verify_payload.get("details") or verify_status),
            "input_confidence": "curated",
            "requested_command_kind": "default" if str(verify_payload.get("verify_cmd") or "") == "pytest -q" else "inline",
            "changed_files": [
                str(item)
                for item in list(task_run.get("task_delta_changed_files") or task_run.get("changed_files") or [])
                if str(item).strip()
            ],
            "surface_results": [],
            "surface_summary": {},
            "verify_scope_mode": "",
        }
    verify_status = _verify_status(
        workflow_chain=workflow_chain,
        semantic_compact=semantic_compact,
        state_payload=state_payload,
    )
    if verify_status != "UNKNOWN":
        return {
            "status": "PASS" if verify_status == "PASS" else "FAIL",
            "verify_status": verify_status,
            "source": "workflow_chain_or_semantic_compact",
            "reason": "" if verify_status == "PASS" else verify_status,
            "input_confidence": "fallback",
            "requested_command_kind": "default",
            "changed_files": [],
            "surface_results": [],
            "surface_summary": {},
            "verify_scope_mode": "",
        }
    if _planning_artifact_mode(planning_dir) == "contract_first":
        return {
            "status": "FAIL",
            "verify_status": "MISSING",
            "source": VERIFY_REPORT_FILENAME,
            "reason": "canonical verify artifact not generated for contract-first flow",
            "input_confidence": "fallback",
            "requested_command_kind": "default",
            "changed_files": [],
            "surface_results": [],
            "surface_summary": {},
            "verify_scope_mode": "",
        }
    return {
        "status": "FAIL",
        "verify_status": "UNKNOWN",
        "source": "",
        "reason": "verify result unavailable",
        "input_confidence": "fallback",
        "requested_command_kind": "default",
        "changed_files": [],
        "surface_results": [],
        "surface_summary": {},
        "verify_scope_mode": "",
    }


def _resolve_execution_check(planning_dir: Path) -> dict[str, Any]:
    execution_path = planning_dir / EXECUTION_RESULT_FILENAME
    if execution_path.exists():
        try:
            payload = load_execution_result(execution_path)
        except (ArtifactSchemaVersionError, ExecutionArtifactError, ValueError) as exc:
            return {
                "status": "FAIL",
                "execution_status": "INVALID",
                "source": EXECUTION_RESULT_FILENAME,
            "backend": "",
            "backend_capabilities": {},
            "backend_capability_truth": {},
            "host_probe": {},
            "execution_guard": {},
            "changed_files": [],
            "reason": f"invalid execution artifact: {exc}",
        }
        execution_status = str(payload.get("status") or "UNKNOWN").upper()
        return {
            "status": "PASS" if execution_status in {"PASS", "DONE", "SUCCESS"} else "FAIL",
            "execution_status": execution_status,
            "source": EXECUTION_RESULT_FILENAME,
            "backend": str(payload.get("backend") or "").strip(),
            "backend_capabilities": dict(payload.get("backend_capabilities") or {}),
            "backend_capability_truth": dict(payload.get("backend_capability_truth") or {}),
            "host_probe": dict(payload.get("host_probe") or {}),
            "execution_guard": {
                "action": str(payload.get("guard_action") or "").strip(),
                "policy": str(payload.get("guard_policy") or "").strip(),
                "pattern": str(payload.get("guard_pattern") or "").strip(),
                "command": str(payload.get("guard_command") or "").strip(),
                "decision": dict(payload.get("guard_decision") or {}),
            },
            "changed_files": [
                str(item) for item in list(payload.get("changed_files") or []) if str(item).strip()
            ],
            "reason": (
                ""
                if execution_status in {"PASS", "DONE", "SUCCESS"}
                else str(payload.get("blocking_reason") or payload.get("summary") or execution_status)
            ),
        }
    task_run = _task_run_payload(planning_dir) or {}
    execution_payload = dict(task_run.get("execution_result") or {})
    if execution_payload:
        execution_status = str(execution_payload.get("status") or "UNKNOWN").upper()
        return {
            "status": "PASS" if execution_status in {"PASS", "DONE", "SUCCESS"} else "FAIL",
            "execution_status": execution_status,
            "source": ".task_run_result.json.execution_result",
            "backend": str(execution_payload.get("backend") or "").strip(),
            "backend_capabilities": dict(execution_payload.get("backend_capabilities") or {}),
            "backend_capability_truth": dict(execution_payload.get("backend_capability_truth") or {}),
            "host_probe": dict(execution_payload.get("host_probe") or {}),
            "execution_guard": {
                "action": str(execution_payload.get("guard_action") or "").strip(),
                "policy": str(execution_payload.get("guard_policy") or "").strip(),
                "pattern": str(execution_payload.get("guard_pattern") or "").strip(),
                "command": str(execution_payload.get("guard_command") or "").strip(),
                "decision": dict(execution_payload.get("guard_decision") or {}),
            },
            "changed_files": [
                str(item)
                for item in list(execution_payload.get("changed_files") or [])
                if str(item).strip()
            ],
            "reason": (
                ""
                if execution_status in {"PASS", "DONE", "SUCCESS"}
                else str(execution_payload.get("blocking_reason") or execution_payload.get("summary") or execution_status)
            ),
        }
    if _planning_artifact_mode(planning_dir) == "contract_first":
        return {
            "status": "FAIL",
            "execution_status": "MISSING",
            "source": EXECUTION_RESULT_FILENAME,
            "backend": "",
            "backend_capabilities": {},
            "backend_capability_truth": {},
            "host_probe": {},
            "execution_guard": {},
            "changed_files": [],
            "reason": "canonical execution artifact not generated for contract-first flow",
        }
    return {
        "status": "FAIL",
        "execution_status": "UNKNOWN",
        "source": "",
        "backend": "",
        "backend_capabilities": {},
        "backend_capability_truth": {},
        "host_probe": {},
        "execution_guard": {},
        "changed_files": [],
        "reason": "execution result unavailable",
    }


def _qa_summary(*, status: str, checks: dict[str, dict[str, Any]]) -> str:
    failing = [name for name, value in checks.items() if str(value.get("status")) == "FAIL"]
    if status == "PASS":
        return "qa=PASS; all checks succeeded"
    return f"qa=BLOCKED; failing checks: {', '.join(failing)}"


def _resolve_risk_policy(risk_profile: str) -> dict[str, bool]:
    normalized = str(risk_profile or "medium").strip().lower()
    return dict(RISK_PROFILE_RULES.get(normalized) or RISK_PROFILE_RULES["medium"])


def _check_item(check: str, passed: bool, details: str) -> dict[str, Any]:
    return {"check": check, "status": "PASS" if passed else "FAIL", "details": details}


def _check_item_with_status(check: str, status: str, details: str) -> dict[str, Any]:
    return {
        "check": check,
        "status": str(status or "PASS").strip().upper() or "PASS",
        "details": details,
    }


def _normalize_changed_files(values: list[Any] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in list(values or []):
        text = str(raw or "").strip().replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    return normalized


def _review_changed_files(review_payload: dict[str, Any] | None) -> list[str]:
    payload = dict((review_payload or {}).get("changed_files") or {})
    return _normalize_changed_files(list(payload.get("items") or []))


def _changed_files_consistency_check(
    *,
    check: str,
    left_source: str,
    left_files: list[str],
    right_source: str,
    right_files: list[str],
) -> dict[str, Any]:
    left = _normalize_changed_files(left_files)
    right = _normalize_changed_files(right_files)
    if not left or not right:
        return {
            "status": "PASS",
            "reason": "",
            "details": f"comparison skipped; {left_source}={left or []}; {right_source}={right or []}",
            "left_source": left_source,
            "left_files": left,
            "right_source": right_source,
            "right_files": right,
        }
    if {item.lower() for item in left} == {item.lower() for item in right}:
        return {
            "status": "PASS",
            "reason": "",
            "details": f"changed files consistent between {left_source} and {right_source}",
            "left_source": left_source,
            "left_files": left,
            "right_source": right_source,
            "right_files": right,
        }
    reason = f"{left_source} changed files {left} do not match {right_source} changed files {right}"
    return {
        "status": "FAIL",
        "reason": reason,
        "details": reason,
        "left_source": left_source,
        "left_files": left,
        "right_source": right_source,
        "right_files": right,
    }


def _risk_profile_checks(
    *,
    risk_profile: str,
    risk_policy: dict[str, bool],
    review_evidence: dict[str, Any],
    verify_check: dict[str, Any],
    eval_check: dict[str, Any],
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []
    explicit = bool(review_evidence.get("explicit_review_evidence"))
    review_source = str(review_evidence.get("review_evidence_source") or "unknown").strip() or "unknown"
    checks.append(
        _check_item_with_status(
            "risk_review_evidence_source",
            (
                "PASS"
                if explicit
                else "FAIL"
                if risk_policy.get("require_explicit_review_evidence")
                else "WARN"
            ),
            (
                f"risk_profile={risk_profile}; review_source={review_source}"
                if explicit
                else f"risk_profile={risk_profile}; explicit review evidence missing; review_source={review_source}"
            ),
        )
    )
    input_confidence = str(verify_check.get("input_confidence") or "").strip().lower() or "fallback"
    checks.append(
        _check_item_with_status(
            "risk_verify_input_confidence",
            (
                "FAIL"
                if input_confidence == "fallback" and risk_policy.get("block_fallback_verify_inputs")
                else "WARN"
                if input_confidence == "fallback"
                else "PASS"
            ),
            f"risk_profile={risk_profile}; verify_input_confidence={input_confidence}",
        )
    )
    if risk_policy.get("block_auto_eval"):
        auto_eval_used = bool(eval_check.get("auto_eval_result"))
        checks.append(
            _check_item(
                "risk_auto_eval",
                not auto_eval_used,
                f"risk_profile={risk_profile}; auto_eval_used={auto_eval_used}",
            )
        )
    if risk_policy.get("block_eval_warnings"):
        warnings = [str(item) for item in list(eval_check.get("warnings") or []) if str(item).strip()]
        checks.append(
            _check_item(
                "risk_eval_warnings",
                not warnings,
                f"risk_profile={risk_profile}; warnings={warnings}",
            )
        )
    return checks


def _resolve_eval_check(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    eval_report_path: str | None,
    auto_eval: bool = False,
) -> dict[str, Any]:
    candidate = (
        Path(eval_report_path).resolve()
        if str(eval_report_path or "").strip()
        else (project_root / "AUTOMATION_EVAL_REPORT.json").resolve()
    )
    payload = _load_json_dict(candidate)
    auto_eval_result: dict[str, Any] = {}
    if payload is None and auto_eval:
        auto_eval_result = _attempt_auto_eval_report(project_root=project_root, planning_dir=planning_dir)
        payload = _load_json_dict(candidate)
    if payload is None:
        remediation = [
            f"kodawari telemetry --project-root {project_root} --feature {feature}",
            (
                "kodawari eval-report "
                f"--project-root {project_root} --run-id {planning_dir.name} "
                f"--output {project_root / 'AUTOMATION_EVAL_REPORT.md'} "
                f"--json-output {project_root / 'AUTOMATION_EVAL_REPORT.json'}"
            ),
        ]
        if not auto_eval:
            remediation.append(
                f"kodawari ship-readiness --project-root {project_root} --feature {feature} --auto-eval"
            )
        details = f"eval_report=missing ({candidate})"
        if auto_eval_result:
            details = f"{details}; auto_eval_attempt={auto_eval_result.get('status', 'UNKNOWN')}"
        return {
            "status": "FAIL",
            "details": details,
            "path": str(candidate),
            "auto_eval_result": auto_eval_result,
            "warnings": [],
            "remediation": remediation,
        }
    status = str(payload.get("status") or "UNKNOWN").upper()
    details = f"eval={status}"
    if auto_eval_result:
        details = f"{details} (auto_eval={auto_eval_result.get('status', 'UNKNOWN')})"
    return {
        "status": status,
        "details": details,
        "path": str(candidate),
        "auto_eval_result": auto_eval_result,
        "warnings": [str(item) for item in list(payload.get("warnings") or []) if str(item).strip()],
        "remediation": [],
    }


def _resolve_release_gate_check(*, project_root: Path, gate_filename: str, gate_type: str) -> dict[str, Any]:
    candidate = (project_root / gate_filename).resolve()
    payload = _load_json_dict(candidate)
    if payload is None:
        return {
            "status": "SKIP",
            "details": f"{gate_type}_gate=missing ({candidate})",
            "path": str(candidate),
        }
    status = str(payload.get("status") or "UNKNOWN").upper()
    return {
        "status": status,
        "details": f"{gate_type}_gate={status}",
        "path": str(candidate),
        "summary": dict(payload.get("summary") or {}),
    }


def _attempt_auto_telemetry(*, project_root: Path, planning_dir: Path) -> dict[str, Any]:
    """C9: best-effort telemetry snapshot so eval-report has input to read."""
    try:
        from kodawari.cli.gate.telemetry_cmd import run_telemetry_command
    except Exception as exc:
        return {"status": "ERROR", "message": f"telemetry_import_failed: {exc}"}
    args = argparse.Namespace(
        project_root=str(project_root),
        feature=planning_dir.name,
        planning_dir=None,
        append_history=True,
        max_history_days=None,
        output=None,
    )
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            rc = int(run_telemetry_command(args))
    except Exception as exc:  # pragma: no cover — best effort
        return {"status": "ERROR", "message": f"telemetry_call_failed: {exc}"}
    return {"status": "PASS" if rc == 0 else "ERROR", "rc": rc}


def _attempt_auto_eval_report(*, project_root: Path, planning_dir: Path) -> dict[str, Any]:
    try:
        from kodawari.cli.gate.telemetry_field_eval_cmd import run_eval_report_command
    except Exception as exc:
        return {"status": "ERROR", "message": f"eval_import_failed: {exc}"}
    # C9: ensure telemetry exists first (best-effort; idempotent via append_history)
    telemetry_result = _attempt_auto_telemetry(project_root=project_root, planning_dir=planning_dir)
    report_json = (project_root / "AUTOMATION_EVAL_REPORT.json").resolve()
    report_md = (project_root / "AUTOMATION_EVAL_REPORT.md").resolve()
    args = argparse.Namespace(
        project_root=str(project_root),
        run_id=[planning_dir.name],
        planning_dir=[],
        all_runs=False,
        max_history_days=None,
        min_pass_rate=0.8,
        max_blocked_rate=0.2,
        max_critical_field_reports=0,
        emit_input_lock=None,
        input_lock=None,
        output=str(report_md),
        json_output=str(report_json),
        fail_on_block=False,
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = int(run_eval_report_command(args))
    raw = buffer.getvalue().strip()
    payload: dict[str, Any] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {"raw_output": raw}
    status = "PASS" if rc == 0 else "ERROR"
    return {
        "status": status,
        "rc": rc,
        "json_output": str(report_json),
        "markdown_output": str(report_md),
        "payload": payload,
        "telemetry_result": telemetry_result,
    }


def _ship_summary(*, status: str, blocking_reason: str) -> str:
    if status == "PASS":
        return "ship_readiness=PASS; ready for release"
    return f"ship_readiness=BLOCKED; {blocking_reason}"


def _write_qa_markdown(path: Path, payload: dict[str, Any]) -> None:
    checks = dict(payload.get("checks") or {})
    lines = [
        f"# QA_REPORT ({payload.get('feature', '')})",
        "",
        f"- overall_status: {payload.get('status', '')}",
        f"- summary: {payload.get('summary', '')}",
        "",
        "## Checks",
    ]
    for name, item in checks.items():
        data = dict(item) if isinstance(item, dict) else {}
        lines.append(f"- {name}: {data.get('status', '')} ({data})")
    atomic_write_text(path, "\n".join(lines) + "\n")


def _write_release_markdown(path: Path, payload: dict[str, Any]) -> None:
    checklist = list(payload.get("checklist") or [])
    remediation = [str(item).strip() for item in list(payload.get("remediation") or []) if str(item).strip()]
    execution_guard = dict(payload.get("execution_guard") or {})
    lines = [
        f"# RELEASE ({payload.get('feature', '')})",
        "",
        f"- overall_status: {payload.get('status', '')}",
        f"- blocking_reason: {payload.get('blocking_reason', '')}",
        f"- summary: {payload.get('summary', '')}",
        f"- execution_guard_action: {execution_guard.get('action', '')}",
        f"- execution_guard_policy: {execution_guard.get('policy', '')}",
        f"- execution_guard_pattern: {execution_guard.get('pattern', '')}",
        f"- execution_guard_command: {execution_guard.get('command', '')}",
        "",
        "## Ship Checklist",
    ]
    if checklist:
        for item in checklist:
            record = dict(item) if isinstance(item, dict) else {}
            status = str(record.get("status") or "").upper()
            marker = "x" if status == "PASS" else "!"
            lines.append(f"- [{marker}] {record.get('check', '')} ({status}): {record.get('details', '')}")
    else:
        lines.append("- [ ] (none)")
    lines.extend(["", "## Remediation"])
    if remediation:
        lines.extend(f"- {item}" for item in remediation)
    else:
        lines.append("- (none)")
    provenance = dict(payload.get("provenance") or {})
    lines.extend(
        [
            "",
            "## Mirror Provenance",
            "- source_json: .ship_readiness.json",
            f"- digest_algorithm: {payload.get('digest_algorithm', '')}",
            f"- payload_digest: {payload.get('payload_digest', '')}",
            f"- generated_at: {payload.get('generated_at', '')}",
            f"- provenance.command: {provenance.get('command', '')}",
            f"- provenance.planning_dir: {provenance.get('planning_dir', '')}",
        ]
    )
    atomic_write_text(path, "\n".join(lines) + "\n")


def build_qa_report(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    policy: Any | None = None,
) -> dict[str, Any]:
    del project_root
    planning_dir.mkdir(parents=True, exist_ok=True)
    planning_mode = _planning_artifact_mode(planning_dir)
    planning_artifacts = _required_planning_artifacts_status(planning_dir, include_delivery_artifacts=False)
    state = _load_json_dict(planning_dir / ".autopilot_state.json")
    workflow_chain = load_workflow_chain_snapshot(planning_dir) or {}
    gate_payload = _load_json_dict(planning_dir / ".gate_result.json")
    review_payload = _load_json_dict(planning_dir / ".review_result.json")
    semantic_compact = _load_json_dict(planning_dir / "semantic_compact.json")
    execution_check = _resolve_execution_check(planning_dir)
    verify_check = _resolve_verify_check(
        planning_dir=planning_dir,
        workflow_chain=workflow_chain,
        semantic_compact=semantic_compact,
        state_payload=state,
    )
    gate_check = _resolve_gate_check(planning_dir=planning_dir, gate_payload=gate_payload)
    review_status = str((review_payload or {}).get("status") or _workflow_chain_review_status(workflow_chain) or "UNKNOWN").upper()
    review_changed_files = _review_changed_files(review_payload)
    review_evidence = _review_evidence(
        planning_dir=planning_dir,
        workflow_chain=workflow_chain,
        semantic_compact=semantic_compact,
        gate_payload=gate_payload,
        review_payload=review_payload,
    )
    must_fix_items = _must_fix_items(semantic_compact)
    checks: dict[str, dict[str, Any]] = {}
    if planning_mode == "contract_first":
        checks["planning_artifacts"] = {
            "status": "PASS" if planning_artifacts["all_present"] else "FAIL",
            "planning_status": "READY" if planning_artifacts["all_present"] else "INVALID",
            "missing": list(planning_artifacts.get("missing") or []),
            "invalid": list(planning_artifacts.get("invalid") or []),
            "reason": (
                "contract-first planning artifacts ready"
                if planning_artifacts["all_present"]
                else f"contract-first planning artifacts missing/invalid: missing={planning_artifacts.get('missing', [])}, invalid={planning_artifacts.get('invalid', [])}"
            ),
        }
    checks.update({
        "execution": execution_check,
        "verify": verify_check,
        "gate": gate_check,
        "review": {
            "status": "PASS" if review_status == "PASS" else "FAIL",
            "review_status": review_status,
        },
        "execution_vs_review_changed_files": _changed_files_consistency_check(
            check="execution_vs_review_changed_files",
            left_source=str(execution_check.get("source") or "execution"),
            left_files=list(execution_check.get("changed_files") or []),
            right_source=".review_result.json",
            right_files=review_changed_files,
        ),
        "execution_vs_verify_changed_files": _changed_files_consistency_check(
            check="execution_vs_verify_changed_files",
            left_source=str(execution_check.get("source") or "execution"),
            left_files=list(execution_check.get("changed_files") or []),
            right_source=str(verify_check.get("source") or "verify"),
            right_files=list(verify_check.get("changed_files") or []),
        ),
        "review_vs_verify_changed_files": _changed_files_consistency_check(
            check="review_vs_verify_changed_files",
            left_source=".review_result.json",
            left_files=review_changed_files,
            right_source=str(verify_check.get("source") or "verify"),
            right_files=list(verify_check.get("changed_files") or []),
        ),
        "review_evidence_presence_for_release": {
            "status": "PASS" if bool(review_evidence.get("explicit_review_evidence")) else "WARN",
            "review_evidence_status": str(review_evidence.get("review_evidence_status") or "UNKNOWN"),
            "review_evidence_source": str(review_evidence.get("review_evidence_source") or ""),
            "reason": (
                "explicit review evidence present"
                if bool(review_evidence.get("explicit_review_evidence"))
                else "explicit review evidence missing; ship-readiness medium/high will still block"
            ),
        },
        "must_fix": {
            "status": "PASS" if not must_fix_items else "FAIL",
            "open_items": must_fix_items,
        },
    })
    checks.update(
        build_surface_consistency_checks(
            planning_dir=planning_dir,
            execution_files=list(execution_check.get("changed_files") or []),
            review_files=review_changed_files,
            verify_files=list(verify_check.get("changed_files") or []),
            verify_report=verify_check,
        )
    )
    failed_checks = [(name, item) for name, item in checks.items() if str(item.get("status")) == "FAIL"]
    blocked = bool(failed_checks)
    status = "BLOCKED" if blocked else "PASS"
    blocking_reason = ""
    remediation: list[str] = []
    if blocked:
        first_name, first_check = failed_checks[0]
        blocking_reason = (
            f"{first_name}: "
            f"{first_check.get('reason') or first_check.get('execution_status') or first_check.get('verify_status') or first_check.get('gate_status') or first_check.get('review_status') or first_check.get('open_items') or first_check.get('status')}"
        )
        if any(name == "execution" for name, _ in failed_checks):
            execution_status = str(execution_check.get("execution_status") or "").upper()
            if execution_status == "MISSING":
                remediation.append("Run `kodawari task-run` with a real executor backend or materialize a valid `.execution_result.json` artifact before rerunning qa.")
            elif execution_status == "INVALID":
                remediation.append("Regenerate `.execution_result.json` from a real executor path before rerunning qa.")
            else:
                remediation.append("Fix the execution backend failure and regenerate `.execution_result.json` before rerunning qa.")
        if any(name == "verify" for name, _ in failed_checks):
            verify_status = str(verify_check.get("verify_status") or "").upper()
            if verify_status == "MISSING":
                remediation.append("Run `kodawari verify --project-root <root> --feature <feature>` to materialize the canonical verify artifact before rerunning qa.")
            elif verify_status == "INVALID":
                remediation.append("Regenerate `.verify_report.json` with `kodawari verify` or fix the invalid verify artifact before rerunning qa.")
            else:
                remediation.append("Rerun the scoped verify command and fix verification failures before rerunning qa.")
        if any(name == "gate" for name, _ in failed_checks):
            if str(gate_check.get("gate_status") or "").upper() == "MISSING":
                remediation.append("Run `kodawari task-run` or `kodawari compliance-check` to materialize gate evidence before rerunning qa.")
            else:
                remediation.append("Resolve the blocking gate findings, then rerun `kodawari qa`.")
        if any(name == "review" for name, _ in failed_checks):
            remediation.append("Fix review blockers and rerun `kodawari review` before qa.")
        if any(name in {"execution_vs_review_changed_files", "execution_vs_verify_changed_files", "review_vs_verify_changed_files", "surface_coverage_consistency"} for name, _ in failed_checks):
            remediation.append(
                "Regenerate execution/review/verify artifacts so they all point at the same changed files truth before rerunning qa."
            )
        if any(name == "must_fix" for name, _ in failed_checks):
            remediation.append("Close all must_fix items in semantic_compact before rerunning qa.")
        if any(name == "planning_artifacts" for name, _ in failed_checks):
            remediation.append("Regenerate or migrate PRD_INTAKE.json, TASK_GRAPH.json, and TASK_CARD_ACTIVE.json before rerunning qa.")
    payload = {
        "status": status,
        "entrypoint": "kodawari qa",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "planning_artifact_mode": planning_mode,
        "planning_artifacts": planning_artifacts,
        "execution_status": str(execution_check.get("execution_status") or "").upper(),
        "execution_source": str(execution_check.get("source") or ""),
        "execution_backend": str(execution_check.get("backend") or ""),
        "execution_backend_capabilities": dict(execution_check.get("backend_capabilities") or {}),
        "execution_guard": dict(execution_check.get("execution_guard") or {}),
        "checks": checks,
        "summary": _qa_summary(status=status, checks=checks),
        "blocking_reason": blocking_reason,
        "remediation": remediation,
        "next_action": "" if not blocked else remediation[0],
    }
    _ensure_design_artifact(planning_dir=planning_dir, feature=feature, state_payload=state, policy=policy)
    from kodawari.autopilot.engine.workflow_policy import should_emit_artifact
    if should_emit_artifact(".qa_report.json", policy):
        _write_json(planning_dir / ".qa_report.json", payload)
    if should_emit_artifact("QA_REPORT.md", policy):
        _write_qa_markdown(planning_dir / "QA_REPORT.md", payload)
    return payload


def build_ship_readiness_report(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    eval_report_path: str | None = None,
    auto_eval: bool = False,
    risk_profile: str = "medium",
    policy: Any | None = None,
) -> dict[str, Any]:
    planning_dir.mkdir(parents=True, exist_ok=True)
    planning_mode = _planning_artifact_mode(planning_dir)
    state = _load_json_dict(planning_dir / ".autopilot_state.json")
    workflow_chain = load_workflow_chain_snapshot(planning_dir) or {}
    gate_payload = _load_json_dict(planning_dir / ".gate_result.json")
    semantic_compact = _load_json_dict(planning_dir / "semantic_compact.json")
    review_payload = _load_json_dict(planning_dir / ".review_result.json")
    qa_payload = _load_json_dict(planning_dir / ".qa_report.json")
    _ensure_design_artifact(planning_dir=planning_dir, feature=feature, state_payload=state, policy=policy)
    _ensure_placeholder_markdown(planning_dir / "REVIEW.md", title=f"REVIEW ({feature})", policy=policy)
    _ensure_placeholder_markdown(planning_dir / "QA_REPORT.md", title=f"QA_REPORT ({feature})", policy=policy)
    must_fix = _must_fix_items(semantic_compact)
    execution_check = _resolve_execution_check(planning_dir)
    verify_check = _resolve_verify_check(
        planning_dir=planning_dir,
        workflow_chain=workflow_chain,
        semantic_compact=semantic_compact,
        state_payload=state,
    )
    gate_check = _resolve_gate_check(planning_dir=planning_dir, gate_payload=gate_payload)
    review_status = str((review_payload or {}).get("status") or _workflow_chain_review_status(workflow_chain) or "UNKNOWN").upper()
    qa_status = str((qa_payload or {}).get("status") or "UNKNOWN").upper()
    boundary_debt = _contract_layer_boundary_debt(planning_dir) if planning_mode == "contract_first" else {
        "status": "PASS",
        "details": "Layer-boundary debt only applies to contract-first task graph.",
        "items": [],
    }
    review_evidence = _review_evidence(
        planning_dir=planning_dir,
        workflow_chain=workflow_chain,
        semantic_compact=semantic_compact,
        gate_payload=gate_payload,
        review_payload=review_payload,
    )
    eval_check = _resolve_eval_check(
        project_root=project_root,
        planning_dir=planning_dir,
        feature=feature,
        eval_report_path=eval_report_path,
        auto_eval=auto_eval,
    )
    replay_check = _resolve_release_gate_check(project_root=project_root, gate_filename=DEFAULT_REPLAY_GATE_RESULT, gate_type="replay")
    canary_check = _resolve_release_gate_check(project_root=project_root, gate_filename=DEFAULT_CANARY_GATE_RESULT, gate_type="canary")
    docs = _required_planning_artifacts_status(planning_dir)
    risk_policy = _resolve_risk_policy(risk_profile)
    review_evidence_check = _review_evidence_check(review_evidence["review_evidence_payload"])
    checklist = [
        _check_item("required_docs", docs["all_present"], f"missing={docs['missing']}; invalid={docs.get('invalid', [])}"),
        _check_item("gate_status", str(gate_check.get("gate_status") or "").upper() == "PASS", f"gate={gate_check.get('gate_status', 'UNKNOWN')}"),
        _check_item("must_fix_closed", not must_fix, f"open_items={len(must_fix)}"),
        _check_item("review_status", review_status == "PASS", f"review={review_status}"),
        _check_item("verify_status", str(verify_check.get("verify_status") or "").upper() == "PASS", f"verify={verify_check.get('verify_status', 'UNKNOWN')}"),
        _check_item("qa_status", qa_status == "PASS", f"qa={qa_status}"),
        _check_item("eval_status", eval_check["status"] == "PASS", eval_check["details"]),
        _check_item("replay_gate", replay_check["status"] != "BLOCKED", replay_check["details"]),
        _check_item("canary_gate", canary_check["status"] != "BLOCKED", canary_check["details"]),
        _check_item("review_evidence", review_evidence_check["status"] == "PASS", review_evidence_check["details"]),
        _check_item_with_status("boundary_debt", str(boundary_debt.get("status") or "PASS").upper(), str(boundary_debt.get("details") or "")),
    ]
    checklist.extend(_risk_profile_checks(
        risk_profile=risk_profile,
        risk_policy=risk_policy,
        review_evidence=review_evidence,
        verify_check=verify_check,
        eval_check=eval_check,
    ))
    blocked_item = next((item for item in checklist if item["status"] == "FAIL"), None)
    status = "BLOCKED" if blocked_item else "PASS"
    blocking_reason = ""
    if blocked_item is not None:
        blocking_reason = f"{blocked_item['check']}: {blocked_item['details']}"
    remediation = list(eval_check.get("remediation") or [])
    if review_evidence_check["status"] != "PASS":
        remediation.append("kodawari review --fail-on-block")
    if any(
        str(item.get("check") or "") == "risk_review_evidence_source" and str(item.get("status") or "").upper() in {"FAIL", "WARN"}
        for item in checklist
    ):
        remediation.append("kodawari review-evidence --project-root <root> --feature <feature> --input REVIEW_EVIDENCE_INPUT.json")
    if any(
        str(item.get("check") or "") == "risk_verify_input_confidence" and str(item.get("status") or "").upper() in {"FAIL", "WARN"}
        for item in checklist
    ):
        remediation.append("kodawari verify --project-root <root> --feature <feature> --changed-file <path>")
    if replay_check["status"] == "BLOCKED":
        remediation.append("kodawari replay-gate --project-root <root> --fail-on-block")
    if canary_check["status"] == "BLOCKED":
        remediation.append("kodawari canary-gate --project-root <root> --fail-on-block")
    payload = {
        "status": status,
        "entrypoint": "kodawari ship-readiness",
        "feature": feature,
        "planning_dir": str(planning_dir),
        "planning_artifact_mode": planning_mode,
        "risk_profile": risk_profile,
        "checklist": checklist,
        "required_docs": docs,
        "eval_report_path": eval_check["path"],
        "replay_gate_path": replay_check["path"],
        "canary_gate_path": canary_check["path"],
        "auto_eval": bool(auto_eval),
        "auto_eval_result": dict(eval_check.get("auto_eval_result") or {}),
        "execution_status": str(execution_check.get("execution_status") or "").upper(),
        "execution_source": str(execution_check.get("source") or ""),
        "execution_backend": str(execution_check.get("backend") or ""),
        "execution_backend_capabilities": dict(execution_check.get("backend_capabilities") or {}),
        "execution_guard": dict(execution_check.get("execution_guard") or {}),
        "execution_check": execution_check,
        "boundary_debt": boundary_debt,
        "risk_policy": risk_policy,
        "remediation": remediation,
        "blocking_reason": blocking_reason,
        "release_gates": {
            "replay": replay_check,
            "canary": canary_check,
        },
        "review_evidence_status": review_evidence["review_evidence_status"],
        "review_evidence_source": review_evidence["review_evidence_source"],
        "explicit_review_evidence": review_evidence["explicit_review_evidence"],
        "review_evidence": review_evidence["review_evidence_payload"],
        "review_evidence_check": review_evidence_check,
        "verify_check": verify_check,
        "summary": _ship_summary(status=status, blocking_reason=blocking_reason),
        "generated_at": _utc_now_iso(),
        "provenance": _build_cli_provenance(
            command="ship-readiness",
            project_root=project_root,
            planning_dir=planning_dir,
        ),
    }
    _attach_payload_digest(payload)
    from kodawari.autopilot.engine.workflow_policy import should_emit_artifact
    if should_emit_artifact(".ship_readiness.json", policy):
        _write_json(planning_dir / ".ship_readiness.json", payload)
    release_markdown_path = planning_dir / "RELEASE.md"
    ship_markdown_path = planning_dir / "Ship.md"
    if should_emit_artifact("RELEASE.md", policy):
        _write_release_markdown(release_markdown_path, payload)
        _write_release_markdown(ship_markdown_path, payload)
    return payload


__all__ = [
    "RISK_PROFILE_RULES",
    "_contract_layer_boundary_debt",
    "_resolve_execution_check",
    "_review_evidence",
    "build_qa_report",
    "build_ship_readiness_report",
]



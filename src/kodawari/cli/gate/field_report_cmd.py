"""Field report command implementations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from kodawari.cli.contract.command_contract import normalize_mutating_payload
from kodawari.cli.evidence.observability_store import (
    DEFAULT_TEXT_FIELD_MAX_LENGTH,
    FIELD_REPORT_SCHEMA_VERSION,
    REPORT_STATUS_IN_PROGRESS,
    REPORT_STATUS_OPEN,
    REPORT_STATUS_RESOLVED,
    SchemaValidationError,
    _ALLOWED_REPORT_STATUSES,
    _ALLOWED_TRANSITIONS,
    _append_jsonl,
    _build_provenance,
    _error_payload,
    _load_json_dict_optional,
    _load_jsonl_dict_rows,
    _load_jsonl_dict_rows_with_errors,
    _now_iso,
    _print_schema_error,
    _resolve_planning_dir,
    _validate_observability_payload,
    _write_json,
    _write_optional_json_output,
)


def _build_field_report_payload(
    *,
    feature: str,
    run_id: str,
    report_id: str,
    severity: str,
    status: str,
    title: Any,
    summary: Any,
    component: Any,
    impact: Any,
    owner: Any,
    tags: list[Any],
    evidence_files: list[Any],
    updated_at: str,
    reported_at: str,
) -> dict[str, Any]:
    normalized_tags = sorted({str(item).strip() for item in tags if str(item).strip()})
    return {
        "schema_version": FIELD_REPORT_SCHEMA_VERSION,
        "reported_at": reported_at,
        "updated_at": updated_at,
        "report_id": str(report_id).strip(),
        "feature": str(feature).strip(),
        "run_id": str(run_id).strip(),
        "severity": str(severity).strip().lower(),
        "status": str(status).strip().lower(),
        "title": _sanitize_text(title),
        "summary": _sanitize_text(summary),
        "component": _sanitize_text(component),
        "impact": _sanitize_text(impact),
        "owner": _sanitize_text(owner),
        "tags": normalized_tags,
        "evidence_files": _sanitize_evidence_files(evidence_files),
    }


def _sanitize_text(value: Any, *, max_length: int = DEFAULT_TEXT_FIELD_MAX_LENGTH) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[:max_length]


def _sanitize_evidence_files(values: list[Any]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        redacted = _redact_path(text)
        key = redacted.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(redacted)
    return cleaned


def _redact_path(raw: str) -> str:
    normalized = str(raw).replace("\\", "/").strip()
    normalized = re.sub(r"^[A-Za-z]:", "", normalized)
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    tail = "/".join(parts[-2:])
    if len(parts) > 2:
        return f".../{tail}"
    return tail


def _is_valid_status_transition(current: str, target: str, *, allow_reopen: bool) -> bool:
    if target not in _ALLOWED_REPORT_STATUSES:
        return False
    if current not in _ALLOWED_REPORT_STATUSES:
        return False
    if current == target:
        return True
    if target in _ALLOWED_TRANSITIONS.get(current, set()):
        return True
    if allow_reopen and current == REPORT_STATUS_RESOLVED and target in {REPORT_STATUS_OPEN, REPORT_STATUS_IN_PROGRESS}:
        return True
    return False


def _load_field_reports(planning_dir: Path) -> list[dict[str, Any]]:
    rows, _ = _load_jsonl_dict_rows_with_errors(planning_dir / ".field_reports.jsonl")
    return rows


def _load_field_reports_with_errors(planning_dir: Path) -> tuple[list[dict[str, Any]], int]:
    return _load_jsonl_dict_rows_with_errors(planning_dir / ".field_reports.jsonl")


def _report_exists(reports: list[dict[str, Any]], report_id: str) -> bool:
    target = str(report_id or "").strip()
    if not target:
        return False
    for report in reports:
        if str(report.get("report_id") or "").strip() == target:
            return True
    return False


def _latest_report_by_id(reports: list[dict[str, Any]], report_id: str) -> dict[str, Any] | None:
    target = str(report_id or "").strip()
    if not target:
        return None
    for report in reversed(reports):
        if str(report.get("report_id") or "").strip() == target:
            return report
    return None


def _latest_field_reports_by_id(reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for report in reports:
        report_id = str(report.get("report_id") or "").strip()
        if report_id:
            latest[report_id] = report
    return latest


def _write_field_report_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# FIELD_REPORT",
        "",
        f"- report_id: {report.get('report_id', '')}",
        f"- status: {report.get('status', '')}",
        f"- severity: {report.get('severity', '')}",
        f"- feature: {report.get('feature', '')}",
        f"- run_id: {report.get('run_id', '')}",
        f"- title: {report.get('title', '')}",
        f"- summary: {report.get('summary', '')}",
        f"- component: {report.get('component', '')}",
        f"- impact: {report.get('impact', '')}",
        f"- owner: {report.get('owner', '')}",
        f"- tags: {', '.join(list(report.get('tags') or []))}",
        "- evidence_files:",
    ]
    evidence = list(report.get("evidence_files") or [])
    if evidence:
        for item in evidence:
            lines.append(f"  - {item}")
    else:
        lines.append("  - (none)")
    lines.extend(
        [
            "",
            f"- reported_at: {report.get('reported_at', '')}",
            f"- updated_at: {report.get('updated_at', '')}",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _generate_report_id() -> str:
    return f"FR-{uuid4().hex[:10]}"


def run_field_report_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    try:
        planning_dir, feature = _resolve_planning_dir(
            project_root=project_root,
            feature=getattr(args, "feature", None),
            planning_dir=getattr(args, "planning_dir", None),
        )
        report_id = str(getattr(args, "report_id", "") or "").strip() or _generate_report_id()
        report_status = str(getattr(args, "report_status", REPORT_STATUS_OPEN) or REPORT_STATUS_OPEN).strip().lower()
        existing_reports = _load_field_reports(planning_dir)
        if _report_exists(existing_reports, report_id):
            payload = _error_payload(
                command="field-report",
                project_root=project_root,
                planning_dir=planning_dir,
                error=f"duplicate report_id detected: {report_id}",
                error_code="duplicate_report_id",
            )
            payload["report_id"] = report_id
            payload["hint"] = (
                "Use 'kodawari field-report-update --project-root ... --feature ... --report-id "
                f"{report_id} --status <in_progress|resolved>' to update existing report."
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
        report = _build_field_report_payload(
            feature=feature,
            run_id=planning_dir.name,
            report_id=report_id,
            severity=str(getattr(args, "severity", "medium") or "medium"),
            status=report_status,
            title=getattr(args, "title", ""),
            summary=getattr(args, "summary", ""),
            component=getattr(args, "component", ""),
            impact=getattr(args, "impact", ""),
            owner=getattr(args, "owner", ""),
            tags=list(getattr(args, "tag", []) or []),
            evidence_files=list(getattr(args, "evidence", []) or []),
            updated_at=_now_iso(),
            reported_at=_now_iso(),
        )
        _validate_observability_payload("field_report", report)
        _append_jsonl(planning_dir / ".field_reports.jsonl", report)
        _write_json(planning_dir / ".field_report.json", report)
        _write_field_report_markdown(planning_dir / "FIELD_REPORT.md", report)
        payload = {
            "status": "RECORDED",
            "feature": feature,
            "planning_dir": str(planning_dir),
            "report_id": report_id,
            "severity": report["severity"],
            "report_status": report["status"],
            "schema_version": FIELD_REPORT_SCHEMA_VERSION,
            "field_report_path": str((planning_dir / ".field_report.json").resolve()),
            "provenance": _build_provenance(
                command="field-report",
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
            command="field-report",
            project_root=project_root,
            planning_dir=None,
            schema_name=exc.schema_name,
            errors=exc.errors,
        )
    except ValueError as exc:
        payload = _error_payload(
            command="field-report",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="field_report_failed",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


def run_field_report_update_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    try:
        planning_dir, _feature = _resolve_planning_dir(
            project_root=project_root,
            feature=getattr(args, "feature", None),
            planning_dir=getattr(args, "planning_dir", None),
        )
        report_id = str(getattr(args, "report_id", "") or "").strip()
        target_status = str(getattr(args, "status", "") or "").strip().lower()
        if not report_id:
            raise ValueError("field-report-update requires --report-id")
        if not target_status:
            raise ValueError("field-report-update requires --status")
        reports, parse_errors = _load_field_reports_with_errors(planning_dir)
        resolution_source = "history_jsonl"
        latest = _latest_report_by_id(reports, report_id)
        if latest is None:
            latest_single = _load_json_dict_optional(planning_dir / ".field_report.json") or {}
            latest_single_id = str(latest_single.get("report_id") or "").strip()
            if latest_single_id == report_id:
                latest = latest_single
                resolution_source = "latest_snapshot"
        if latest is None:
            details = f"report_id not found: {report_id}"
            if parse_errors:
                details += f" (detected {parse_errors} malformed history rows in .field_reports.jsonl)"
            raise ValueError(details)
        current_status = str(latest.get("status") or "").strip().lower()
        allow_reopen = bool(getattr(args, "allow_reopen", False))
        if not _is_valid_status_transition(current_status, target_status, allow_reopen=allow_reopen):
            payload = _error_payload(
                command="field-report-update",
                project_root=project_root,
                planning_dir=planning_dir,
                error=f"status transition not allowed: {current_status} -> {target_status}",
                error_code="invalid_status_transition",
            )
            payload["report_id"] = report_id
            payload["current_status"] = current_status
            payload["target_status"] = target_status
            payload["allow_reopen"] = allow_reopen
            payload["resolution_source"] = resolution_source
            if parse_errors:
                payload["history_parse_errors"] = parse_errors
                payload["warning"] = (
                    f"Ignored {parse_errors} malformed row(s) in .field_reports.jsonl while resolving report history."
                )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
        updated = dict(latest)
        updated["status"] = target_status
        updated["updated_at"] = _now_iso()
        for key in ("title", "summary", "component", "impact", "owner"):
            updated[key] = _sanitize_text(updated.get(key, ""))
        updated["evidence_files"] = _sanitize_evidence_files(list(updated.get("evidence_files") or []))
        _validate_observability_payload("field_report", updated)
        _append_jsonl(planning_dir / ".field_reports.jsonl", updated)
        _write_json(planning_dir / ".field_report.json", updated)
        _write_field_report_markdown(planning_dir / "FIELD_REPORT.md", updated)
        payload = {
            "status": "UPDATED",
            "report_id": report_id,
            "from_status": current_status,
            "to_status": target_status,
            "allow_reopen": allow_reopen,
            "resolution_source": resolution_source,
            "field_report_path": str((planning_dir / ".field_report.json").resolve()),
            "provenance": _build_provenance(
                command="field-report-update",
                project_root=project_root,
                planning_dir=planning_dir,
            ),
        }
        if parse_errors:
            payload["history_parse_errors"] = parse_errors
            payload["warning"] = (
                f"Ignored {parse_errors} malformed row(s) in .field_reports.jsonl while resolving report history."
            )
        normalized_payload = normalize_mutating_payload(payload)
        _write_optional_json_output(normalized_payload, getattr(args, "output", None), project_root=project_root)
        print(json.dumps(normalized_payload, ensure_ascii=False, indent=2))
        return 0
    except SchemaValidationError as exc:
        return _print_schema_error(
            command="field-report-update",
            project_root=project_root,
            planning_dir=None,
            schema_name=exc.schema_name,
            errors=exc.errors,
        )
    except ValueError as exc:
        payload = _error_payload(
            command="field-report-update",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="field_report_update_failed",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


__all__ = [
    "run_field_report_command",
    "run_field_report_update_command",
    "_append_jsonl",
    "_latest_field_reports_by_id",
    "_load_field_reports",
    "_load_field_reports_with_errors",
]


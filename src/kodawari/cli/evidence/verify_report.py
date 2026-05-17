"""Canonical verify report artifact helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from kodawari.cli.artifact_versions import load_versioned_artifact, validate_schema_version
from kodawari.cli.io_atomic import atomic_write_json


VERIFY_REPORT_SCHEMA_VERSION = "verify.report.v1"
VERIFY_REPORT_FILENAME = ".verify_report.json"

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


class VerifyReportSchemaValidationError(ValueError):
    """Raised when a verify report does not satisfy its schema."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("verify report schema validation failed")
        self.errors = errors


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[2] / "schemas" / "observability" / "verify_report.schema.json"


def _load_schema() -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get("verify_report")
    if cached is not None:
        return cached
    payload = json.loads(_schema_path().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid verify report schema: {_schema_path()}")
    _SCHEMA_CACHE["verify_report"] = payload
    return payload


def validate_verify_report_payload(payload: dict[str, Any]) -> dict[str, Any]:
    schema = _load_schema()
    validator = Draft202012Validator(schema)
    errors: list[dict[str, str]] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.path)):
        field = ".".join(str(part) for part in error.path) or "<root>"
        errors.append({"field": field, "message": error.message})
    if errors:
        raise VerifyReportSchemaValidationError(errors=errors)
    return payload


def _resolve_verify_cmd(payload: dict[str, Any], requested_command: str) -> str:
    return str(payload.get("verify_cmd") or requested_command or "pytest -q").strip() or "pytest -q"


def _resolve_passed(payload: dict[str, Any], status: str) -> bool:
    if "passed" in payload:
        return bool(payload.get("passed"))
    return status == "PASS"


def _resolve_mode(payload: dict[str, Any]) -> str:
    default = "command" if payload.get("command_executed") else "compat_post_execution_qa"
    return str(payload.get("mode") or default)


def _resolve_source(payload: dict[str, Any]) -> str:
    default = "verify_command" if payload.get("command_executed") else "verify_check"
    return str(payload.get("source") or default)


def _resolve_target_source(payload: dict[str, Any], verify_cmd: str) -> str:
    default = "explicit_command" if verify_cmd != "pytest -q" else "default"
    return str(payload.get("verify_target_source") or default)


def _resolve_blocking_reason(payload: dict[str, Any], status: str) -> str:
    if payload.get("blocking_reason"):
        return str(payload["blocking_reason"])
    return "" if status == "PASS" else str(payload.get("summary") or status)


def _resolve_command_executed(payload: dict[str, Any], verify_cmd: str) -> bool:
    if "command_executed" in payload:
        return bool(payload.get("command_executed"))
    return verify_cmd != "pytest -q"


def _filter_strings(items: list[Any] | None) -> list[str]:
    return [s for s in (str(item).strip() for item in list(items or [])) if s]


def _normalize_verify_check(verify_check: dict[str, Any], *, requested_command: str) -> dict[str, Any]:
    payload = dict(verify_check or {})
    status = str(payload.get("status") or "UNKNOWN").upper()
    verify_cmd = _resolve_verify_cmd(payload, requested_command)
    payload["status"] = status
    payload["passed"] = _resolve_passed(payload, status)
    payload["mode"] = _resolve_mode(payload)
    payload["source"] = _resolve_source(payload)
    payload["verify_cmd"] = verify_cmd
    payload["verify_cmd_resolved"] = str(payload.get("verify_cmd_resolved") or verify_cmd)
    payload["verify_target_source"] = _resolve_target_source(payload, verify_cmd)
    payload["verify_targets"] = _filter_strings(payload.get("verify_targets"))
    payload["summary"] = str(payload.get("summary") or payload.get("details") or "")
    payload["blocking_reason"] = _resolve_blocking_reason(payload, status)
    payload["command_executed"] = _resolve_command_executed(payload, verify_cmd)
    payload["artifacts"] = _filter_strings(payload.get("artifacts"))
    payload["stdout_excerpt"] = str(payload.get("stdout_excerpt") or "")
    payload["stderr_excerpt"] = str(payload.get("stderr_excerpt") or "")
    payload["returncode"] = payload.get("returncode", 0 if status == "PASS" else None)
    return payload


def _infer_requested_command_kind(requested_command: str, requested_command_kind: str | None) -> str:
    normalized_kind = str(requested_command_kind or "").strip().lower()
    if normalized_kind in {"file", "inline", "default"}:
        return normalized_kind
    return "default" if str(requested_command or "").strip() == "pytest -q" else "inline"


def _build_changed_files_section(changed_files: list[str], changed_files_source: str) -> dict[str, Any]:
    items = _filter_strings(changed_files)
    return {"source": str(changed_files_source).strip(), "items": items, "count": len(items)}


def _apply_optional_fields(
    payload: dict[str, Any],
    surface_results: list[dict[str, Any]] | None,
    surface_summary: dict[str, Any] | None,
    verify_scope_mode: str | None,
) -> None:
    if surface_results:
        payload["surface_results"] = [dict(item) for item in surface_results if isinstance(item, dict)]
    if isinstance(surface_summary, dict) and surface_summary:
        payload["surface_summary"] = dict(surface_summary)
    if str(verify_scope_mode or "").strip():
        payload["verify_scope_mode"] = str(verify_scope_mode).strip()


def build_verify_report_payload(
    *,
    feature: str,
    planning_dir: Path,
    verify_check: dict[str, Any],
    changed_files: list[str],
    changed_files_source: str,
    input_confidence: str,
    requested_command: str,
    requested_command_kind: str | None = None,
    entrypoint: str,
    surface_results: list[dict[str, Any]] | None = None,
    surface_summary: dict[str, Any] | None = None,
    verify_scope_mode: str | None = None,
) -> dict[str, Any]:
    normalized_verify_check = _normalize_verify_check(verify_check, requested_command=requested_command)
    verify_status = str(normalized_verify_check.get("status") or "UNKNOWN").upper()
    payload: dict[str, Any] = {
        "schema_version": VERIFY_REPORT_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "feature": str(feature).strip(),
        "planning_dir": str(planning_dir.resolve()),
        "entrypoint": str(entrypoint).strip() or "kodawari verify",
        "requested_command": str(requested_command).strip(),
        "requested_command_kind": _infer_requested_command_kind(requested_command, requested_command_kind),
        "changed_files": _build_changed_files_section(changed_files, changed_files_source),
        "input_confidence": str(input_confidence).strip().lower() or "fallback",
        "status": verify_status,
        "verify_check": normalized_verify_check,
    }
    _apply_optional_fields(payload, surface_results, surface_summary, verify_scope_mode)
    return payload


def load_verify_report_artifact(path: Path) -> dict[str, Any]:
    payload = load_versioned_artifact(path)
    validate_verify_report_payload(payload)
    return payload


def write_verify_report_artifact(path: Path, payload: dict[str, Any]) -> None:
    validate_schema_version(path, payload)
    validate_verify_report_payload(payload)
    atomic_write_json(path, payload)

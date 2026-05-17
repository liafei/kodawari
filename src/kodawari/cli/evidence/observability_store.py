"""Shared observability helpers for telemetry/field/eval commands."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.io_atomic import (
    CorruptArtifactError,
    append_jsonl_atomic,
    atomic_write_json,
    load_json_dict,
    quarantine_corrupt_jsonl_lines,
)
from kodawari.cli.provenance import build_cli_provenance

SNAPSHOT_SCHEMA_VERSION = "telemetry.snapshot.v1"
FIELD_REPORT_SCHEMA_VERSION = "field.report.v1"
EVAL_REPORT_SCHEMA_VERSION = "eval.report.v1"
EVAL_INPUT_LOCK_SCHEMA_VERSION = "eval.input_lock.v1"

DEFAULT_TEXT_FIELD_MAX_LENGTH = 512
DEFAULT_EVAL_REPORT_JSON = "AUTOMATION_EVAL_REPORT.json"
DEFAULT_EVAL_REPORT_MD = "AUTOMATION_EVAL_REPORT.md"
DEFAULT_EVAL_INPUT_LOCK = "AUTOMATION_EVAL_INPUT_LOCK.json"

REPORT_STATUS_OPEN = "open"
REPORT_STATUS_IN_PROGRESS = "in_progress"
REPORT_STATUS_RESOLVED = "resolved"

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    REPORT_STATUS_OPEN: {REPORT_STATUS_IN_PROGRESS, REPORT_STATUS_RESOLVED},
    REPORT_STATUS_IN_PROGRESS: {REPORT_STATUS_RESOLVED},
    REPORT_STATUS_RESOLVED: set(),
}
_ALLOWED_REPORT_STATUSES = frozenset(_ALLOWED_TRANSITIONS.keys())
_TERMINAL_PASS_STATUSES = {"PASS"}
_TERMINAL_BLOCKED_STATUSES = {"BLOCKED", "FAIL", "HARD_ERROR"}
_TERMINAL_STATUSES = _TERMINAL_PASS_STATUSES | _TERMINAL_BLOCKED_STATUSES

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


class SchemaValidationError(ValueError):
    """Raised when a payload does not satisfy an observability schema."""

    def __init__(self, schema_name: str, errors: list[dict[str, str]]) -> None:
        super().__init__(f"schema validation failed: {schema_name}")
        self.schema_name = schema_name
        self.errors = errors


def _resolve_planning_dir(*, project_root: Path, feature: str | None, planning_dir: str | None) -> tuple[Path, str]:
    if planning_dir:
        resolved = Path(planning_dir).resolve()
        if not resolved.exists():
            raise ValueError(f"planning_dir does not exist: {resolved}")
        inferred_feature = str(feature or resolved.name).strip() or resolved.name
        return resolved, inferred_feature
    feature_text = str(feature or "").strip()
    if not feature_text:
        raise ValueError("feature is required when planning_dir is not provided")
    resolved = (project_root / "planning" / feature_text).resolve()
    if not resolved.exists():
        raise ValueError(f"planning_dir does not exist: {resolved}")
    return resolved, feature_text


def _load_schema(schema_name: str) -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get(schema_name)
    if cached is not None:
        return cached
    base = Path(__file__).resolve().parents[2] / "schemas" / "observability"
    schema_path = base / f"{schema_name}.schema.json"
    schema = _load_json_dict_required(schema_path)
    _SCHEMA_CACHE[schema_name] = schema
    return schema


def _validate_observability_payload(schema_name: str, payload: dict[str, Any]) -> None:
    schema = _load_schema(schema_name)
    validator = Draft202012Validator(schema)
    errors: list[dict[str, str]] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.path)):
        path = ".".join(str(part) for part in error.path) or "<root>"
        errors.append({"field": path, "message": error.message})
    if errors:
        raise SchemaValidationError(schema_name=schema_name, errors=errors)


def _load_json_dict_required(path: Path) -> dict[str, Any]:
    payload = load_json_dict(path, required=True, quarantine_on_error=True)
    if payload is None:
        raise ValueError(f"required file not found: {path}")
    return payload


def _load_json_dict_optional(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return _load_json_dict_required(path)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError, CorruptArtifactError):
        return None


def _load_jsonl_dict_rows(path: Path) -> list[dict[str, Any]]:
    rows, _ = _load_jsonl_dict_rows_with_errors(path)
    return rows


def _load_jsonl_dict_rows_with_errors(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    parse_errors = 0
    bad_lines: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                payload = _recover_jsonl_line(line)
                if payload is None:
                    bad_lines.append(line)
                    parse_errors += 1
                    continue
            if isinstance(payload, dict):
                rows.append(payload)
            else:
                bad_lines.append(line)
                parse_errors += 1
    quarantine_corrupt_jsonl_lines(path, bad_lines)
    return rows, parse_errors


def _recover_jsonl_line(line: str) -> dict[str, Any] | None:
    start = line.find("{")
    end = line.rfind("}")
    if start < 0 or end <= start:
        return None
    fragment = line[start : end + 1]
    try:
        payload = json.loads(fragment)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _count_recent_history_events(path: Path, *, max_history_days: int | None) -> int:
    rows = _load_jsonl_dict_rows(path)
    if max_history_days is None:
        return len(rows)
    now = datetime.now(timezone.utc)
    return sum(
        1
        for row in rows
        if _within_history_window(row.get("captured_at"), max_history_days=max_history_days, now=now)
    )


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    append_jsonl_atomic(path, payload)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _write_optional_json_output(payload: dict[str, Any], output: str | None, *, project_root: Path) -> None:
    text = str(output or "").strip()
    if not text:
        return
    destination = _resolve_optional_path(project_root, text)
    if destination is None:
        return
    _write_json(destination, payload)


def _resolve_optional_path(project_root: Path, raw_path: str | None) -> Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = int(text)
    if parsed < 0:
        raise ValueError("max-history-days must be >= 0")
    return parsed


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if float(denominator) <= 0.0:
        return 0.0
    return float(numerator) / float(denominator)


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _within_history_window(captured_at: Any, *, max_history_days: int | None, now: datetime) -> bool:
    if max_history_days is None:
        return True
    parsed = _parse_iso_datetime(captured_at)
    if parsed is None:
        return False
    return parsed >= now - timedelta(days=max_history_days)


def _build_provenance(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    resolved_planning_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    return build_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        resolved_planning_dirs=resolved_planning_dirs,
        module_file=Path(__file__),
    )


def _print_schema_error(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    schema_name: str,
    errors: list[dict[str, str]],
    resolved_planning_dirs: list[Path] | None = None,
    remediation: list[str] | None = None,
) -> int:
    payload = _error_payload(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        error=f"schema validation failed: {schema_name}",
        error_code="schema_validation_failed",
        resolved_planning_dirs=resolved_planning_dirs,
        remediation=remediation,
    )
    payload["schema"] = schema_name
    payload["field_errors"] = errors
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 2


def _eval_error_remediation(*, error: str, args: argparse.Namespace) -> list[str]:
    del args
    normalized = str(error or "").strip()
    lower = normalized.lower()
    suggestions: list[str] = []
    if "planning run not found" in lower or "planning_dir does not exist" in lower:
        suggestions.append("Verify the requested --run-id/--planning-dir exists under the planning/ directory.")
    if "max_history_days" in lower:
        suggestions.append("Increase --max-history-days or cleanse older snapshots before rerunning eval-report.")
    if "requires --run-id" in lower:
        suggestions.append("Provide --run-id or --planning-dir (or use --all-runs) so eval-report can locate existing runs.")
    if "schema validation failed: eval_input_lock" in lower:
        suggestions.append("Regenerate the input lock with `kodawari eval-report --run-id <run> --emit-input-lock <path>` before replaying.")
    suggestions.append("Run `kodawari telemetry --project-root <root> --feature <feature>` before eval-report to produce required artifacts.")
    return suggestions


def _error_payload(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    error: str,
    error_code: str,
    resolved_planning_dirs: list[Path] | None = None,
    remediation: list[str] | None = None,
) -> dict[str, Any]:
    payload = build_error_payload(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=Path(__file__),
        error=str(error),
        error_code=error_code,
        resolved_planning_dirs=resolved_planning_dirs,
        remediation=list(remediation or []),
    )
    return normalize_mutating_payload(payload)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DEFAULT_EVAL_INPUT_LOCK",
    "DEFAULT_EVAL_REPORT_JSON",
    "DEFAULT_EVAL_REPORT_MD",
    "DEFAULT_TEXT_FIELD_MAX_LENGTH",
    "EVAL_INPUT_LOCK_SCHEMA_VERSION",
    "EVAL_REPORT_SCHEMA_VERSION",
    "FIELD_REPORT_SCHEMA_VERSION",
    "REPORT_STATUS_IN_PROGRESS",
    "REPORT_STATUS_OPEN",
    "REPORT_STATUS_RESOLVED",
    "SNAPSHOT_SCHEMA_VERSION",
    "SchemaValidationError",
    "_ALLOWED_REPORT_STATUSES",
    "_ALLOWED_TRANSITIONS",
    "_TERMINAL_BLOCKED_STATUSES",
    "_TERMINAL_PASS_STATUSES",
    "_TERMINAL_STATUSES",
    "_append_jsonl",
    "_build_provenance",
    "_count_recent_history_events",
    "_error_payload",
    "_eval_error_remediation",
    "_int_or_none",
    "_int_or_zero",
    "_load_json_dict_optional",
    "_load_json_dict_required",
    "_load_jsonl_dict_rows",
    "_load_jsonl_dict_rows_with_errors",
    "_now_iso",
    "_parse_iso_datetime",
    "_print_schema_error",
    "_ratio",
    "_resolve_optional_path",
    "_resolve_planning_dir",
    "_validate_observability_payload",
    "_within_history_window",
    "_write_json",
    "_write_optional_json_output",
]


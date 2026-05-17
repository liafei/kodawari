"""Compatibility facade for telemetry, field-report, and eval commands."""

from __future__ import annotations

from kodawari.cli.evidence.eval_report_cmd import run_eval_report_command
from kodawari.cli.gate.field_report_cmd import (
    run_field_report_command,
    run_field_report_update_command,
)
from kodawari.cli.evidence.observability_store import (
    EVAL_REPORT_SCHEMA_VERSION,
    FIELD_REPORT_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    SchemaValidationError,
    _append_jsonl,
    _load_jsonl_dict_rows,
)
from kodawari.cli.gate.telemetry_cmd import run_telemetry_command

__all__ = [
    "EVAL_REPORT_SCHEMA_VERSION",
    "FIELD_REPORT_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "SchemaValidationError",
    "_append_jsonl",
    "_load_jsonl_dict_rows",
    "run_eval_report_command",
    "run_field_report_command",
    "run_field_report_update_command",
    "run_telemetry_command",
]


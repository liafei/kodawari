"""Incident ingest command that feeds the field-report state machine."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from typing import Any

from kodawari.cli.contract.command_contract import normalize_mutating_payload
from kodawari.cli.gate.telemetry_field_eval_cmd import run_field_report_command


def run_incident_ingest_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    forwarded_args = argparse.Namespace(
        project_root=str(project_root),
        feature=getattr(args, "feature", None),
        planning_dir=getattr(args, "planning_dir", None),
        report_id=getattr(args, "incident_id", None),
        severity=getattr(args, "severity", "high"),
        title=getattr(args, "title", ""),
        summary=getattr(args, "summary", ""),
        component=getattr(args, "component", ""),
        impact=getattr(args, "impact", ""),
        owner=getattr(args, "owner", ""),
        report_status="open",
        tag=list(getattr(args, "tag", []) or []),
        evidence=list(getattr(args, "evidence", []) or []),
        output=None,
    )
    raw_payload = ""
    with contextlib.redirect_stdout(io.StringIO()) as buffer:
        rc = int(run_field_report_command(forwarded_args))
        raw_payload = buffer.getvalue().strip()
    try:
        nested_payload = json.loads(raw_payload) if raw_payload else {}
    except json.JSONDecodeError:
        nested_payload = {"raw_output": raw_payload}

    if rc != 0:
        payload = nested_payload if isinstance(nested_payload, dict) else {}
        payload["entrypoint"] = "kodawari incident-ingest"
        payload["incident_source"] = str(getattr(args, "source", "production"))
        print(json.dumps(normalize_mutating_payload(payload), ensure_ascii=False, indent=2))
        return rc

    payload = normalize_mutating_payload(
        {
            "status": "RECORDED",
            "entrypoint": "kodawari incident-ingest",
            "incident_source": str(getattr(args, "source", "production")),
            "feature": getattr(args, "feature", None),
            "planning_dir": nested_payload.get("planning_dir") if isinstance(nested_payload, dict) else None,
            "incident_id": getattr(args, "incident_id", None) or nested_payload.get("report_id"),
            "field_report_result": nested_payload,
            "remediation": [],
            "next_action": "",
        }
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


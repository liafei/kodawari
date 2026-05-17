"""Replay/canary gate commands for frozen release-gate inputs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.io_atomic import atomic_write_json, load_json_dict
from kodawari.cli.provenance import build_cli_provenance


REPLAY_INPUT_SCHEMA_VERSION = "release.replay.input.v1"
REPLAY_RESULT_SCHEMA_VERSION = "release.replay.result.v1"
CANARY_INPUT_SCHEMA_VERSION = "release.canary.input.v1"
CANARY_RESULT_SCHEMA_VERSION = "release.canary.result.v1"


def _provenance(command: str, project_root: Path) -> dict[str, Any]:
    return build_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=None,
        module_file=Path(__file__),
    )


def _load_gate_input(path: Path, *, required_schema: str) -> dict[str, Any]:
    payload = load_json_dict(path, required=True)
    if payload is None:
        raise ValueError(f"required file not found: {path}")
    actual = str(payload.get("schema_version") or "").strip()
    if actual != required_schema:
        raise ValueError(f"input schema_version mismatch for {path}: expected {required_schema}, got {actual or '<missing>'}")
    samples = payload.get("samples")
    if not isinstance(samples, list):
        raise ValueError(f"release gate input must include samples[]: {path}")
    return payload


def _sample_status(sample: dict[str, Any]) -> tuple[bool, str]:
    explicit_status = str(sample.get("status") or "").strip().upper()
    expected_status = str(sample.get("expected_status") or "").strip().upper()
    actual_status = str(sample.get("actual_status") or "").strip().upper()
    if explicit_status:
        failed = explicit_status in {"FAIL", "BLOCKED", "ERROR"}
        return failed, explicit_status
    if expected_status or actual_status:
        failed = bool(expected_status) and bool(actual_status) and expected_status != actual_status
        return failed, f"expected={expected_status or '?'} actual={actual_status or '?'}"
    return False, "PASS"


def _evaluate_replay_samples(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    evaluated: list[dict[str, Any]] = []
    failures = 0
    for item in samples:
        sample = dict(item) if isinstance(item, dict) else {}
        failed, status_label = _sample_status(sample)
        if failed:
            failures += 1
        evaluated.append(
            {
                "name": str(sample.get("name") or sample.get("sample_id") or f"sample-{len(evaluated) + 1}"),
                "status": "FAIL" if failed else "PASS",
                "details": str(sample.get("details") or status_label),
                "evidence": [str(value) for value in list(sample.get("evidence") or []) if str(value).strip()],
            }
        )
    return evaluated, failures


def _evaluate_canary_samples(samples: list[dict[str, Any]], *, max_failed: int) -> tuple[list[dict[str, Any]], int]:
    evaluated: list[dict[str, Any]] = []
    failures = 0
    for item in samples:
        sample = dict(item) if isinstance(item, dict) else {}
        status = str(sample.get("status") or "").strip().upper()
        failed = status in {"FAIL", "BLOCKED", "ERROR"}
        if failed:
            failures += 1
        evaluated.append(
            {
                "name": str(sample.get("name") or sample.get("sample_id") or f"sample-{len(evaluated) + 1}"),
                "status": "FAIL" if failed else "PASS",
                "details": str(sample.get("details") or f"status={status or 'UNKNOWN'}"),
                "evidence": [str(value) for value in list(sample.get("evidence") or []) if str(value).strip()],
            }
        )
    return evaluated, failures


def run_replay_gate_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    input_path = Path(getattr(args, "input", "") or (project_root / "REPLAY_GATE_INPUT.json")).resolve()
    output_path = Path(getattr(args, "output", "") or (project_root / "REPLAY_GATE_RESULT.json")).resolve()
    try:
        payload = _load_gate_input(input_path, required_schema=REPLAY_INPUT_SCHEMA_VERSION)
        evaluated, failures = _evaluate_replay_samples(list(payload.get("samples") or []))
        result = {
            "schema_version": REPLAY_RESULT_SCHEMA_VERSION,
            "gate_type": "replay",
            "status": "BLOCKED" if failures else "PASS",
            "input_path": str(input_path),
            "summary": {
                "samples_total": len(evaluated),
                "samples_failed": failures,
            },
            "samples": evaluated,
            "blocking_reason": "" if failures == 0 else f"replay samples failed: {failures}",
            "provenance": _provenance("replay-gate", project_root),
        }
        atomic_write_json(output_path, result)
        cli_payload = normalize_mutating_payload(
            {
                **result,
                "entrypoint": "kodawari replay-gate",
                "output_path": str(output_path),
                "remediation": [] if failures == 0 else ["Fix replay regressions before proceeding to ship-readiness."],
                "next_action": "" if failures == 0 else "Review the failed replay samples and rerun replay-gate.",
            }
        )
        print(json.dumps(cli_payload, ensure_ascii=False, indent=2))
        if bool(getattr(args, "fail_on_block", False)) and failures > 0:
            return 2
        return 0
    except ValueError as exc:
        payload = normalize_mutating_payload(
            build_error_payload(
                command="replay-gate",
                project_root=project_root,
                planning_dir=None,
                module_file=Path(__file__),
                error=str(exc),
                error_code="replay_gate_failed",
                remediation=["Provide a frozen replay input artifact and rerun replay-gate."],
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


def run_canary_gate_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    input_path = Path(getattr(args, "input", "") or (project_root / "CANARY_GATE_INPUT.json")).resolve()
    output_path = Path(getattr(args, "output", "") or (project_root / "CANARY_GATE_RESULT.json")).resolve()
    try:
        payload = _load_gate_input(input_path, required_schema=CANARY_INPUT_SCHEMA_VERSION)
        max_failed = int(getattr(args, "max_failed", payload.get("max_failed_samples", 0)) or 0)
        evaluated, failures = _evaluate_canary_samples(list(payload.get("samples") or []), max_failed=max_failed)
        result = {
            "schema_version": CANARY_RESULT_SCHEMA_VERSION,
            "gate_type": "canary",
            "status": "BLOCKED" if failures > max_failed else "PASS",
            "input_path": str(input_path),
            "summary": {
                "samples_total": len(evaluated),
                "samples_failed": failures,
                "max_failed_samples": max_failed,
            },
            "samples": evaluated,
            "blocking_reason": "" if failures <= max_failed else f"canary failed samples={failures} > allowed={max_failed}",
            "provenance": _provenance("canary-gate", project_root),
        }
        atomic_write_json(output_path, result)
        cli_payload = normalize_mutating_payload(
            {
                **result,
                "entrypoint": "kodawari canary-gate",
                "output_path": str(output_path),
                "remediation": [] if failures <= max_failed else ["Investigate canary failures before proceeding to ship-readiness."],
                "next_action": "" if failures <= max_failed else "Review canary failures and rerun canary-gate.",
            }
        )
        print(json.dumps(cli_payload, ensure_ascii=False, indent=2))
        if bool(getattr(args, "fail_on_block", False)) and failures > max_failed:
            return 2
        return 0
    except ValueError as exc:
        payload = normalize_mutating_payload(
            build_error_payload(
                command="canary-gate",
                project_root=project_root,
                planning_dir=None,
                module_file=Path(__file__),
                error=str(exc),
                error_code="canary_gate_failed",
                remediation=["Provide a frozen canary input artifact and rerun canary-gate."],
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


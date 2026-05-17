"""Eval report command implementation."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from kodawari.cli.artifact_versions import ArtifactSchemaVersionError, load_versioned_artifact
from kodawari.cli.contract.command_contract import normalize_mutating_payload
from kodawari.cli.gate.field_report_cmd import _latest_field_reports_by_id, _load_field_reports
from kodawari.cli.io_atomic import CorruptArtifactError
from kodawari.cli.evidence.observability_store import (
    DEFAULT_EVAL_INPUT_LOCK,
    DEFAULT_EVAL_REPORT_JSON,
    DEFAULT_EVAL_REPORT_MD,
    EVAL_INPUT_LOCK_SCHEMA_VERSION,
    EVAL_REPORT_SCHEMA_VERSION,
    REPORT_STATUS_RESOLVED,
    SchemaValidationError,
    _TERMINAL_BLOCKED_STATUSES,
    _TERMINAL_PASS_STATUSES,
    _TERMINAL_STATUSES,
    _build_provenance,
    _error_payload,
    _eval_error_remediation,
    _int_or_none,
    _int_or_zero,
    _load_json_dict_optional,
    _now_iso,
    _print_schema_error,
    _ratio,
    _resolve_optional_path,
    _validate_observability_payload,
    _within_history_window,
    _write_json,
)


def _resolve_thresholds_from_lock(lock_payload: dict[str, Any]) -> dict[str, Any]:
    thresholds = dict(lock_payload.get("thresholds") or {})
    if not thresholds:
        raise ValueError("input lock missing thresholds")
    return {
        "min_pass_rate": float(thresholds.get("min_pass_rate", 0.8)),
        "max_blocked_rate": float(thresholds.get("max_blocked_rate", 0.2)),
        "max_critical_field_reports": int(thresholds.get("max_critical_field_reports", 0) or 0),
    }


def _resolve_emit_input_lock_path(project_root: Path, raw: Any) -> Path | None:
    if raw is None or raw is False:
        return None
    text = str(raw).strip()
    if not text:
        return (project_root / DEFAULT_EVAL_INPUT_LOCK).resolve()
    return _resolve_optional_path(project_root, text)


def _build_eval_input_lock_payload(
    *,
    planning_dirs: list[Path],
    thresholds: dict[str, Any],
    max_history_days: int | None,
) -> dict[str, Any]:
    return {
        "schema_version": EVAL_INPUT_LOCK_SCHEMA_VERSION,
        "created_at": _now_iso(),
        "run_ids": [path.name for path in planning_dirs],
        "planning_dirs": [str(path.resolve()) for path in planning_dirs],
        "thresholds": {
            "min_pass_rate": float(thresholds["min_pass_rate"]),
            "max_blocked_rate": float(thresholds["max_blocked_rate"]),
            "max_critical_field_reports": int(thresholds["max_critical_field_reports"]),
        },
        "max_history_days": max_history_days,
    }


def _resolve_eval_planning_dirs(
    *,
    project_root: Path,
    run_ids: list[str],
    explicit_planning_dirs: list[str],
    all_runs: bool,
) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        resolved.append(path.resolve())

    for run_id in run_ids:
        candidate = (project_root / "planning" / run_id).resolve()
        if not candidate.exists():
            raise ValueError(f"planning run not found for run_id: {run_id}")
        add(candidate)
    for raw in explicit_planning_dirs:
        candidate = Path(raw).resolve()
        if not candidate.exists():
            raise ValueError(f"planning_dir does not exist: {candidate}")
        add(candidate)
    if all_runs:
        planning_root = (project_root / "planning").resolve()
        if planning_root.exists():
            for candidate in sorted(path for path in planning_root.iterdir() if path.is_dir()):
                add(candidate)
    return resolved


def _resolve_eval_inputs(
    *,
    args: argparse.Namespace,
    project_root: Path,
    lock_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if lock_payload is not None:
        _validate_observability_payload("eval_input_lock", lock_payload)
        lock_run_ids = [str(item).strip() for item in list(lock_payload.get("run_ids") or []) if str(item).strip()]
        lock_planning_dirs = [str(item).strip() for item in list(lock_payload.get("planning_dirs") or []) if str(item).strip()]
        thresholds = _resolve_thresholds_from_lock(lock_payload)
        max_history_days = _int_or_none(lock_payload.get("max_history_days"))
        planning_dirs = _resolve_eval_planning_dirs(
            project_root=project_root,
            run_ids=lock_run_ids,
            explicit_planning_dirs=lock_planning_dirs,
            all_runs=False,
        )
        return {
            "planning_dirs": planning_dirs,
            "thresholds": thresholds,
            "max_history_days": max_history_days,
            "selection_source": "input_lock",
        }
    run_ids = [str(item).strip() for item in list(getattr(args, "run_id", []) or []) if str(item).strip()]
    explicit_planning_dirs = [str(item).strip() for item in list(getattr(args, "planning_dir", []) or []) if str(item).strip()]
    planning_dirs = _resolve_eval_planning_dirs(
        project_root=project_root,
        run_ids=run_ids,
        explicit_planning_dirs=explicit_planning_dirs,
        all_runs=bool(getattr(args, "all_runs", False)),
    )
    thresholds = {
        "min_pass_rate": float(getattr(args, "min_pass_rate", 0.8)),
        "max_blocked_rate": float(getattr(args, "max_blocked_rate", 0.2)),
        "max_critical_field_reports": int(getattr(args, "max_critical_field_reports", 0) or 0),
    }
    max_history_days = _int_or_none(getattr(args, "max_history_days", None))
    return {
        "planning_dirs": planning_dirs,
        "thresholds": thresholds,
        "max_history_days": max_history_days,
        "selection_source": "cli",
    }


def _collect_eval_runs(
    planning_dirs: list[Path],
    *,
    max_history_days: int | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    now = datetime.now(timezone.utc)
    for planning_dir in planning_dirs:
        snapshot_path = planning_dir / ".telemetry_snapshot.json"
        if not snapshot_path.exists():
            warnings.append(f"{planning_dir.name}: missing telemetry snapshot")
            continue
        try:
            snapshot = load_versioned_artifact(snapshot_path)
            _validate_observability_payload("telemetry_snapshot", snapshot)
        except (ValueError, SchemaValidationError, ArtifactSchemaVersionError, CorruptArtifactError):
            warnings.append(f"{planning_dir.name}: invalid telemetry snapshot")
            continue
        if not _within_history_window(snapshot.get("captured_at"), max_history_days=max_history_days, now=now):
            warnings.append(f"{planning_dir.name}: snapshot out of max-history-days window")
            continue
        latest_reports = _latest_field_reports_by_id(_load_field_reports(planning_dir))
        critical_open_reports = [
            report
            for report in latest_reports.values()
            if str(report.get("severity") or "").strip().lower() == "critical"
            and str(report.get("status") or "").strip().lower() != REPORT_STATUS_RESOLVED
        ]
        rows.append(
            {
                "run_id": planning_dir.name,
                "feature": str(snapshot.get("feature") or planning_dir.name),
                "status": str(snapshot.get("status") or "UNKNOWN").upper(),
                "captured_at": str(snapshot.get("captured_at") or ""),
                "metrics": dict(snapshot.get("metrics") or {}),
                "signals": dict(snapshot.get("signals") or {}),
                "critical_field_reports": len(critical_open_reports),
                "critical_report_ids": [str(report.get("report_id") or "") for report in critical_open_reports],
                "snapshot_path": str(snapshot_path.resolve()),
            }
        )
    return rows, warnings


def _build_eval_summary(*, runs: list[dict[str, Any]], max_critical_field_reports: int) -> dict[str, Any]:
    total_runs = len(runs)
    terminal_runs = [run for run in runs if str(run.get("status") or "").upper() in _TERMINAL_STATUSES]
    terminal_total = len(terminal_runs)
    pass_runs = sum(1 for run in terminal_runs if str(run.get("status") or "").upper() in _TERMINAL_PASS_STATUSES)
    blocked_runs = sum(1 for run in terminal_runs if str(run.get("status") or "").upper() in _TERMINAL_BLOCKED_STATUSES)
    pending_runs = max(0, total_runs - terminal_total)
    critical_reports = sum(int(run.get("critical_field_reports") or 0) for run in runs)
    review_rounds_total = sum(_int_or_zero(dict(run.get("metrics") or {}).get("review_rounds_used")) for run in runs)
    return {
        "runs_total": total_runs,
        "runs_terminal": terminal_total,
        "runs_pending": pending_runs,
        "runs_pass": pass_runs,
        "runs_blocked": blocked_runs,
        "pass_rate": _ratio(pass_runs, terminal_total),
        "blocked_rate": _ratio(blocked_runs, terminal_total),
        "critical_field_reports": critical_reports,
        "critical_field_reports_threshold": int(max_critical_field_reports),
        "review_rounds_used_total": review_rounds_total,
        "review_rounds_used_avg": _ratio(review_rounds_total, total_runs),
    }


def _eval_violations(*, summary: dict[str, Any], thresholds: dict[str, Any]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    terminal_total = int(summary.get("runs_terminal") or 0)
    pass_rate = float(summary.get("pass_rate") or 0.0)
    blocked_rate = float(summary.get("blocked_rate") or 0.0)
    critical_reports = int(summary.get("critical_field_reports") or 0)
    if terminal_total > 0:
        if pass_rate < float(thresholds["min_pass_rate"]):
            violations.append({"metric": "pass_rate", "expected": f">={thresholds['min_pass_rate']}", "actual": pass_rate})
        if blocked_rate > float(thresholds["max_blocked_rate"]):
            violations.append({"metric": "blocked_rate", "expected": f"<={thresholds['max_blocked_rate']}", "actual": blocked_rate})
    if critical_reports > int(thresholds["max_critical_field_reports"]):
        violations.append(
            {
                "metric": "critical_field_reports",
                "expected": f"<={thresholds['max_critical_field_reports']}",
                "actual": critical_reports,
            }
        )
    return violations


def _eval_warnings(summary: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    terminal_total = int(summary.get("runs_terminal") or 0)
    pending_runs = int(summary.get("runs_pending") or 0)
    if terminal_total <= 0:
        warnings.append("No terminal runs in input set; pass_rate/blocked_rate thresholds were skipped.")
    elif pending_runs > 0:
        warnings.append(f"{pending_runs} pending runs excluded from pass_rate/blocked_rate denominator.")
    return warnings


def _build_rule_candidates(
    *,
    summary: dict[str, Any],
    violations: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    blocked_rate = float(summary.get("blocked_rate") or 0.0)
    critical_reports = int(summary.get("critical_field_reports") or 0)
    review_rounds_avg = float(summary.get("review_rounds_used_avg") or 0.0)
    blocked_runs = [
        str(run.get("run_id") or "")
        for run in runs
        if str(run.get("status") or "").upper() in _TERMINAL_BLOCKED_STATUSES
    ]
    if blocked_rate > 0.0 and blocked_runs:
        candidates.append(
            {
                "candidate_id": "rule.blocked_rate.followup",
                "reason": "Blocked runs were detected in the locked eval input set.",
                "suggested_action": "Review repeated blocked causes and consider a targeted gate rule only after replay/canary validation.",
                "evidence": blocked_runs[:5],
            }
        )
    if critical_reports > 0:
        candidates.append(
            {
                "candidate_id": "rule.critical_field_reports.followup",
                "reason": "Critical field reports remain open across evaluated runs.",
                "suggested_action": "Convert the recurring incident into a reviewed gate candidate after triage confirms a stable invariant.",
                "evidence": [str(summary.get("critical_field_reports") or 0)],
            }
        )
    if review_rounds_avg > 1.0:
        candidates.append(
            {
                "candidate_id": "rule.review_round_budget.followup",
                "reason": "Average review rounds are elevated across the evaluated runs.",
                "suggested_action": "Audit recurring review churn and consider adding tighter task-card invariants before enabling stricter automation.",
                "evidence": [f"review_rounds_used_avg={review_rounds_avg:.2f}"],
            }
        )
    for violation in violations:
        metric = str(violation.get("metric") or "").strip()
        if not metric:
            continue
        candidates.append(
            {
                "candidate_id": f"rule.{metric}.threshold",
                "reason": f"Eval violation detected for {metric}.",
                "suggested_action": "Keep this as a candidate only; validate on replay/canary before hardening thresholds.",
                "evidence": [json.dumps(violation, ensure_ascii=False)],
            }
        )
    return candidates


def _write_eval_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = dict(report.get("summary") or {})
    thresholds = dict(report.get("thresholds") or {})
    violations = list(report.get("violations") or [])
    input_payload = dict(report.get("input") or {})
    warnings = [str(item) for item in list(input_payload.get("warnings") or []) if str(item).strip()]
    lines = [
        "# AUTOMATION_EVAL_REPORT",
        "",
        f"- status: {report.get('status', 'UNKNOWN')}",
        f"- evaluated_at: {report.get('evaluated_at', '')}",
        "",
        "## Summary",
        "",
        f"- runs_total: {summary.get('runs_total', 0)}",
        f"- runs_terminal: {summary.get('runs_terminal', 0)}",
        f"- runs_pending: {summary.get('runs_pending', 0)}",
        f"- runs_pass: {summary.get('runs_pass', 0)}",
        f"- runs_blocked: {summary.get('runs_blocked', 0)}",
        f"- pass_rate: {summary.get('pass_rate', 0.0):.4f}",
        f"- blocked_rate: {summary.get('blocked_rate', 0.0):.4f}",
        f"- critical_field_reports: {summary.get('critical_field_reports', 0)}",
        f"- review_rounds_used_total: {summary.get('review_rounds_used_total', 0)}",
        f"- review_rounds_used_avg: {summary.get('review_rounds_used_avg', 0.0):.4f}",
        "",
        "## Thresholds",
        "",
        f"- min_pass_rate: {thresholds.get('min_pass_rate', 0.0)}",
        f"- max_blocked_rate: {thresholds.get('max_blocked_rate', 0.0)}",
        f"- max_critical_field_reports: {thresholds.get('max_critical_field_reports', 0)}",
        "",
        "## Violations",
        "",
    ]
    if violations:
        for item in violations:
            lines.append(f"- {item.get('metric', '')}: expected {item.get('expected', '')}, actual {item.get('actual', '')}")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Warnings", ""])
    if warnings:
        lines.extend([f"- {item}" for item in warnings])
    else:
        lines.append("- (none)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_eval_report_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dirs_for_provenance: list[Path] = []
    input_lock_path: Path | None = None
    try:
        input_lock_path = _resolve_optional_path(project_root, getattr(args, "input_lock", None))
        lock_payload = load_versioned_artifact(input_lock_path) if input_lock_path is not None else None
        eval_config = _resolve_eval_inputs(args=args, project_root=project_root, lock_payload=lock_payload)
        planning_dirs = eval_config["planning_dirs"]
        planning_dirs_for_provenance = planning_dirs
        if not planning_dirs:
            raise ValueError("eval-report requires --run-id/--planning-dir, --all-runs, or --input-lock")
        runs, warnings = _collect_eval_runs(
            planning_dirs,
            max_history_days=eval_config["max_history_days"],
        )
        if not runs:
            raise ValueError("no telemetry snapshots available for eval input set")
        summary = _build_eval_summary(
            runs=runs,
            max_critical_field_reports=eval_config["thresholds"]["max_critical_field_reports"],
        )
        violations = _eval_violations(summary=summary, thresholds=eval_config["thresholds"])
        rule_candidates = _build_rule_candidates(summary=summary, violations=violations, runs=runs)
        warnings.extend(_eval_warnings(summary))
        status = "BLOCKED" if violations else "PASS"
        eval_report = {
            "schema_version": EVAL_REPORT_SCHEMA_VERSION,
            "evaluated_at": _now_iso(),
            "status": status,
            "thresholds": dict(eval_config["thresholds"]),
            "summary": summary,
            "violations": violations,
            "rule_candidates": rule_candidates,
            "runs": runs,
            "input": {
                "run_ids": [path.name for path in planning_dirs],
                "planning_dirs": [str(path.resolve()) for path in planning_dirs],
                "input_lock": str(input_lock_path.resolve()) if input_lock_path is not None else None,
                "max_history_days": eval_config["max_history_days"],
                "warnings": warnings,
            },
        }
        _validate_observability_payload("eval_report", eval_report)
        json_output = _resolve_optional_path(project_root, getattr(args, "json_output", None)) or (project_root / DEFAULT_EVAL_REPORT_JSON)
        markdown_output = _resolve_optional_path(project_root, getattr(args, "output", None)) or (project_root / DEFAULT_EVAL_REPORT_MD)
        _write_json(json_output, eval_report)
        _write_eval_markdown(markdown_output, eval_report)
        emit_input_lock_path = _resolve_emit_input_lock_path(project_root, getattr(args, "emit_input_lock", None))
        if emit_input_lock_path is not None:
            input_lock_payload = _build_eval_input_lock_payload(
                planning_dirs=planning_dirs,
                thresholds=eval_config["thresholds"],
                max_history_days=eval_config["max_history_days"],
            )
            _validate_observability_payload("eval_input_lock", input_lock_payload)
            _write_json(emit_input_lock_path, input_lock_payload)
        payload = {
            "status": status,
            "summary": summary,
            "thresholds": dict(eval_config["thresholds"]),
            "violations": violations,
            "rule_candidates": rule_candidates,
            "warnings": warnings,
            "json_output": str(json_output.resolve()),
            "markdown_output": str(markdown_output.resolve()),
            "input_lock": str(input_lock_path.resolve()) if input_lock_path is not None else None,
            "emitted_input_lock": str(emit_input_lock_path.resolve()) if emit_input_lock_path is not None else None,
            "provenance": _build_provenance(
                command="eval-report",
                project_root=project_root,
                planning_dir=None,
                resolved_planning_dirs=planning_dirs,
            ),
        }
        print(
            json.dumps(
                normalize_mutating_payload(
                    payload,
                    default_next_action="" if status == "PASS" else "Review violations and rerun eval-report after remediation.",
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        if bool(getattr(args, "fail_on_block", False)) and status == "BLOCKED":
            return 2
        return 0
    except SchemaValidationError as exc:
        return _print_schema_error(
            command="eval-report",
            project_root=project_root,
            planning_dir=None,
            schema_name=exc.schema_name,
            errors=exc.errors,
            resolved_planning_dirs=planning_dirs_for_provenance,
            remediation=_eval_error_remediation(error=f"schema validation failed: {exc.schema_name}", args=args),
        )
    except ArtifactSchemaVersionError as exc:
        payload = _error_payload(
            command="eval-report",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="artifact_schema_version_invalid",
            resolved_planning_dirs=planning_dirs_for_provenance,
            remediation=[
                "Run `kodawari migrate-artifacts --project-root <root> --run-id <run>` before rerunning eval-report."
            ],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except CorruptArtifactError as exc:
        remediation = ["Inspect or regenerate the quarantined artifact before rerunning eval-report."]
        if exc.quarantine_path is not None:
            remediation.append(f"Quarantined copy: {exc.quarantine_path}")
        payload = _error_payload(
            command="eval-report",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="artifact_corrupt",
            resolved_planning_dirs=planning_dirs_for_provenance,
            remediation=remediation,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except ValueError as exc:
        payload = _error_payload(
            command="eval-report",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="eval_report_failed",
            resolved_planning_dirs=planning_dirs_for_provenance,
            remediation=_eval_error_remediation(error=str(exc), args=args),
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


__all__ = ["run_eval_report_command"]


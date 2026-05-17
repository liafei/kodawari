"""Aggregate repeated lane stability/triage artifacts into a weekly trend report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from kodawari.cli.contract.command_contract import normalize_mutating_payload
from kodawari.cli.evidence.observability_store import (
    _build_provenance,
    _error_payload,
    _int_or_none,
    _parse_iso_datetime,
    _resolve_optional_path,
    _within_history_window,
    _write_json,
)
from kodawari.cli.gate.root_cause_buckets import classify_root_cause_bucket, ranked_root_causes, root_cause_bucket_label
from kodawari.cli.status.stability_report_parser import parse_datetime_filter, serialize_datetime


DEFAULT_LANE_TREND_JSON = "AUTOMATION_LANE_TREND_REPORT.json"
DEFAULT_LANE_TREND_MD = "AUTOMATION_LANE_TREND_REPORT.md"
_DEFAULT_MAX_HISTORY_DAYS = 7


def _load_json_dict(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object in: {path}")
    return payload


def _resolve_artifact_inputs(args: argparse.Namespace, *, project_root: Path) -> tuple[list[Path], list[Path]]:
    dirs_raw = [str(item).strip() for item in list(getattr(args, "artifacts_dir", []) or []) if str(item).strip()]
    files_raw = [str(item).strip() for item in list(getattr(args, "summary_path", []) or []) if str(item).strip()]

    if not dirs_raw and not files_raw:
        dirs_raw = ["planning"]

    resolved_dirs: list[Path] = []
    seen_dirs: set[str] = set()
    for raw in dirs_raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate = candidate.resolve()
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"artifact directory does not exist: {candidate}")
        key = str(candidate).lower()
        if key not in seen_dirs:
            seen_dirs.add(key)
            resolved_dirs.append(candidate)

    resolved_files: list[Path] = []
    seen_files: set[str] = set()
    for raw in files_raw:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        candidate = candidate.resolve()
        if not candidate.exists() or not candidate.is_file():
            raise ValueError(f"summary artifact does not exist: {candidate}")
        key = str(candidate).lower()
        if key not in seen_files:
            seen_files.add(key)
            resolved_files.append(candidate)

    return resolved_dirs, resolved_files


def _discover_summary_paths(*, artifact_dirs: list[Path], summary_paths: list[Path]) -> list[Path]:
    discovered: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path.resolve()).lower()
        if key in seen:
            return
        seen.add(key)
        discovered.append(path.resolve())

    for path in summary_paths:
        add(path)
    for root in artifact_dirs:
        for path in sorted(root.rglob("lane_stability_*.json")):
            add(path)
    return discovered


def _summary_lane(summary_payload: dict[str, Any], path: Path) -> str:
    lane = str(summary_payload.get("lane") or "").strip()
    if not lane:
        raise ValueError(f"lane field is required in: {path}")
    return lane


def _resolve_local_triage_path(summary_path: Path, summary_payload: dict[str, Any]) -> Path | None:
    lane = _summary_lane(summary_payload, summary_path)
    candidates: list[Path] = [summary_path.parent / f"lane_triage_{lane}.json"]
    triage_meta = dict(summary_payload.get("triage_artifacts") or {})
    raw = str(triage_meta.get("json") or "").strip()
    if raw:
        raw_path = Path(raw)
        if raw_path.exists():
            candidates.insert(0, raw_path.resolve())
        else:
            candidates.append(summary_path.parent / raw_path.name)
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate.resolve()
    return None


def _summary_failure_signatures(summary_payload: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for row in list(summary_payload.get("runs") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").upper() == "PASS":
            continue
        message = str(row.get("message") or row.get("status") or "").strip() or "unknown"
        counts[message] = counts.get(message, 0) + 1
    return [
        {"signature": key, "count": value}
        for key, value in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _failure_messages_from_signatures(rows: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("signature") or "").strip() for item in rows if str(item.get("signature") or "").strip()]


def _all_non_pass_runs_match(summary_payload: dict[str, Any], needle: str) -> bool:
    non_pass = [
        row
        for row in list(summary_payload.get("runs") or [])
        if isinstance(row, dict) and str(row.get("status") or "").upper() != "PASS"
    ]
    if not non_pass:
        return False
    lowered = needle.lower()
    return all(lowered in str(row.get("message") or "").lower() for row in non_pass)


def _fallback_triage(summary_payload: dict[str, Any]) -> dict[str, Any]:
    lane = str(summary_payload.get("lane") or "").strip()
    status = str(summary_payload.get("status") or "UNKNOWN").upper()
    fail_if_skipped = bool(summary_payload.get("fail_if_skipped", False))
    passed_runs = int(summary_payload.get("passed_runs") or 0)
    failed_runs = int(summary_payload.get("failed_runs") or 0)
    skipped_runs = int(summary_payload.get("skipped_runs") or 0)
    missing_env = sorted(
        {
            str(name).strip()
            for row in list(summary_payload.get("runs") or [])
            if isinstance(row, dict)
            for name in list(row.get("missing_env") or [])
            if str(name).strip()
        }
    )
    classification_id = "lane.unclassified"
    classification_label = "Unclassified lane outcome"
    alert_level = "warning"
    headline = "Lane outcome requires manual interpretation."
    if status == "PASS":
        classification_id = "lane.stable_pass"
        classification_label = "Stable pass"
        alert_level = "info"
        headline = "Lane repeated cleanly across all requested runs."
    elif status == "SKIP" and _all_non_pass_runs_match(summary_payload, "required integration environment is incomplete"):
        classification_id = "lane.integration_env_missing"
        classification_label = "Integration environment missing"
        alert_level = "warning"
        headline = "Lane skipped because required integration environment was missing."
    elif status == "FAIL" and fail_if_skipped and _all_non_pass_runs_match(summary_payload, "required integration environment is incomplete"):
        classification_id = "lane.integration_env_missing_fail_closed"
        classification_label = "Integration environment missing (fail-closed)"
        alert_level = "error"
        headline = "Lane failed closed because required integration environment was missing."
    elif failed_runs > 0 and passed_runs > 0:
        classification_id = "lane.flaky_failure"
        classification_label = "Flaky lane"
        alert_level = "warning"
        headline = "Lane mixed pass and fail outcomes inside the selected window."
    elif failed_runs > 0:
        classification_id = "lane.consistent_failure"
        classification_label = "Consistent lane failure"
        alert_level = "error"
        headline = "Lane failed consistently across the selected repeats."
    failure_signatures = _summary_failure_signatures(summary_payload)
    root_cause_bucket = classify_root_cause_bucket(
        classification_id=classification_id,
        status=status,
        missing_env=missing_env,
        failure_messages=_failure_messages_from_signatures(failure_signatures),
        headline=headline,
    )
    return {
        "schema_version": "lane.triage.v1",
        "triage_version": "lane.triage.v1",
        "lane": lane,
        "status": status,
        "alert_level": alert_level,
        "classification_id": classification_id,
        "classification_label": classification_label,
        "headline": headline,
        "root_cause_bucket": root_cause_bucket,
        "root_cause_label": root_cause_bucket_label(root_cause_bucket),
        "missing_env": missing_env,
        "failure_signatures": failure_signatures,
        "generated_at_utc": str(summary_payload.get("finished_at_utc") or summary_payload.get("started_at_utc") or ""),
    }


def _load_artifact_record(summary_path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    try:
        summary_payload = _load_json_dict(summary_path)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"{summary_path}: skipped invalid lane summary ({type(exc).__name__})"]
    summary_schema_version = str(summary_payload.get("schema_version") or summary_payload.get("summary_version") or "").strip()
    if summary_schema_version != "lane.stability.v1":
        return None, [f"{summary_path}: skipped unsupported lane summary version"]

    lane = _summary_lane(summary_payload, summary_path)
    triage_path = _resolve_local_triage_path(summary_path, summary_payload)
    triage_payload: dict[str, Any]
    if triage_path is None:
        triage_payload = _fallback_triage(summary_payload)
        warnings.append(f"{summary_path}: triage artifact missing; used fallback classification")
    else:
        try:
            triage_payload = _load_json_dict(triage_path)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            triage_payload = _fallback_triage(summary_payload)
            warnings.append(f"{triage_path}: invalid triage artifact ({type(exc).__name__}); used fallback classification")
        else:
            triage_schema_version = str(triage_payload.get("schema_version") or triage_payload.get("triage_version") or "").strip()
            if triage_schema_version != "lane.triage.v1":
                triage_payload = _fallback_triage(summary_payload)
                warnings.append(f"{triage_path}: unsupported triage version; used fallback classification")

    timestamp_text = (
        str(summary_payload.get("finished_at_utc") or "").strip()
        or str(summary_payload.get("started_at_utc") or "").strip()
        or str(triage_payload.get("generated_at_utc") or "").strip()
    )
    captured_at = _parse_iso_datetime(timestamp_text)
    if captured_at is None:
        return None, [f"{summary_path}: skipped missing/invalid finished_at_utc timestamp"]

    failure_signatures = list(triage_payload.get("failure_signatures") or [])
    if not failure_signatures:
        failure_signatures = _summary_failure_signatures(summary_payload)
    missing_env = [str(item).strip() for item in list(triage_payload.get("missing_env") or []) if str(item).strip()]
    root_cause_bucket = str(triage_payload.get("root_cause_bucket") or "").strip()
    if not root_cause_bucket:
        root_cause_bucket = classify_root_cause_bucket(
            classification_id=str(triage_payload.get("classification_id") or "lane.unclassified"),
            status=str(triage_payload.get("status") or summary_payload.get("status") or "UNKNOWN"),
            stop_reason=str(triage_payload.get("stop_reason") or summary_payload.get("stop_reason") or ""),
            gate_status=str(triage_payload.get("gate_status") or summary_payload.get("gate_status") or ""),
            verify_status=str(triage_payload.get("verify_status") or summary_payload.get("verify_status") or ""),
            round_outcome=str(triage_payload.get("round_outcome") or summary_payload.get("round_outcome") or ""),
            run_outcome=str(triage_payload.get("run_outcome") or summary_payload.get("run_outcome") or ""),
            error_categories=list(triage_payload.get("error_categories") or []),
            missing_env=missing_env,
            failure_messages=_failure_messages_from_signatures(failure_signatures),
            blocking_reason=str(triage_payload.get("blocking_reason") or ""),
            headline=str(triage_payload.get("headline") or ""),
        )
    root_cause_label = str(triage_payload.get("root_cause_label") or "").strip() or root_cause_bucket_label(root_cause_bucket)

    record = {
        "lane": lane,
        "captured_at": captured_at.isoformat(),
        "status": str(summary_payload.get("status") or "UNKNOWN").upper(),
        "alert_level": str(triage_payload.get("alert_level") or "warning").strip().lower() or "warning",
        "classification_id": str(triage_payload.get("classification_id") or "lane.unclassified").strip() or "lane.unclassified",
        "classification_label": str(triage_payload.get("classification_label") or "").strip(),
        "root_cause_bucket": root_cause_bucket,
        "root_cause_label": root_cause_label,
        "headline": str(triage_payload.get("headline") or "").strip(),
        "artifact_dir": str(summary_path.parent.resolve()),
        "summary_path": str(summary_path.resolve()),
        "triage_path": str(triage_path.resolve()) if triage_path is not None else "",
        "repeat_completed": int(summary_payload.get("repeat_completed") or 0),
        "passed_runs": int(summary_payload.get("passed_runs") or 0),
        "failed_runs": int(summary_payload.get("failed_runs") or 0),
        "skipped_runs": int(summary_payload.get("skipped_runs") or 0),
        "fail_if_skipped": bool(summary_payload.get("fail_if_skipped", False)),
        "missing_env": missing_env,
        "failure_signatures": [
            {
                "signature": str(item.get("signature") or "").strip(),
                "count": int(item.get("count") or 0),
            }
            for item in failure_signatures
            if isinstance(item, dict) and str(item.get("signature") or "").strip()
        ],
    }
    return record, warnings


def _filter_records(
    records: list[dict[str, Any]],
    *,
    lanes: list[str],
    updated_since: datetime | None,
    updated_until: datetime | None,
    max_history_days: int | None,
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    selected: list[dict[str, Any]] = []
    normalized_lanes = {item.strip().lower() for item in lanes if item.strip()}
    for record in records:
        lane = str(record.get("lane") or "").strip().lower()
        if normalized_lanes and lane not in normalized_lanes:
            continue
        captured_at = _parse_iso_datetime(record.get("captured_at"))
        if captured_at is None:
            continue
        if updated_since is not None and captured_at < updated_since:
            continue
        if updated_until is not None and captured_at > updated_until:
            continue
        if max_history_days is not None and not _within_history_window(record.get("captured_at"), max_history_days=max_history_days, now=now):
            continue
        selected.append(record)
    return sorted(selected, key=lambda item: str(item.get("captured_at") or ""), reverse=True)


def _count_by(records: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        value = str(record.get(key) or "").strip()
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def _consecutive_stable_passes(records: list[dict[str, Any]]) -> int:
    total = 0
    for record in records:
        if str(record.get("classification_id") or "") != "lane.stable_pass":
            break
        total += 1
    return total


def _latest_record_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "captured_at": str(record.get("captured_at") or ""),
        "status": str(record.get("status") or ""),
        "alert_level": str(record.get("alert_level") or ""),
        "classification_id": str(record.get("classification_id") or ""),
        "root_cause_bucket": str(record.get("root_cause_bucket") or ""),
        "root_cause_label": str(record.get("root_cause_label") or ""),
        "headline": str(record.get("headline") or ""),
        "summary_path": str(record.get("summary_path") or ""),
        "triage_path": str(record.get("triage_path") or ""),
    }


def _aggregate_lane_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {}
    latest = records[0]
    status_counts = _count_by(records, "status")
    alert_level_counts = _count_by(records, "alert_level")
    classification_counts = _count_by(records, "classification_id")
    root_cause_bucket_counts = _count_by(records, "root_cause_bucket")
    records_total = len(records)
    pass_records = sum(1 for record in records if str(record.get("status") or "").upper() == "PASS")
    return {
        "records_total": records_total,
        "status_counts": status_counts,
        "alert_level_counts": alert_level_counts,
        "classification_counts": classification_counts,
        "root_cause_bucket_counts": root_cause_bucket_counts,
        "top_root_causes": ranked_root_causes(root_cause_bucket_counts),
        "pass_rate": round(pass_records / float(records_total), 4) if records_total else 0.0,
        "error_alert_rate": round(alert_level_counts.get("error", 0) / float(records_total), 4) if records_total else 0.0,
        "consecutive_stable_passes_from_latest": _consecutive_stable_passes(records),
        "latest_record": _latest_record_payload(latest),
    }


def _aggregate_failure_signatures(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for record in records:
        lane = str(record.get("lane") or "")
        classification = str(record.get("classification_id") or "")
        for item in list(record.get("failure_signatures") or []):
            if not isinstance(item, dict):
                continue
            signature = str(item.get("signature") or "").strip()
            if not signature:
                continue
            entry = buckets.setdefault(
                signature,
                {
                    "signature": signature,
                    "count": 0,
                    "lanes": set(),
                    "classifications": set(),
                },
            )
            entry["count"] += int(item.get("count") or 0)
            if lane:
                entry["lanes"].add(lane)
            if classification:
                entry["classifications"].add(classification)
    ranked = sorted(buckets.values(), key=lambda item: (-int(item["count"]), str(item["signature"])))
    return [
        {
            "signature": str(item["signature"]),
            "count": int(item["count"]),
            "lanes": sorted(str(value) for value in item["lanes"]),
            "classifications": sorted(str(value) for value in item["classifications"]),
        }
        for item in ranked[:10]
    ]


def _build_trend_suggestions(*, records: list[dict[str, Any]], lane_summaries: dict[str, dict[str, Any]]) -> list[str]:
    classification_counts = _count_by(records, "classification_id")
    suggestions: list[str] = []
    if classification_counts.get("lane.integration_env_missing_fail_closed", 0) > 0:
        suggestions.append("Treat repeated `lane.integration_env_missing_fail_closed` results as environment incidents; audit secrets scope and gateway availability before rerunning integration standing proof.")
    if classification_counts.get("lane.integration_env_missing", 0) > 0:
        suggestions.append("Do not count `lane.integration_env_missing` runs as standing proof; restore the real integration environment and rerun fail-closed.")
    if classification_counts.get("lane.flaky_failure", 0) > 0:
        suggestions.append("Investigate flaky lanes with a single fail-fast rerun before changing recipes or thresholds.")
    if classification_counts.get("lane.consistent_failure", 0) > 0:
        suggestions.append("Keep the recipe fixed and debug the consistent failing signature before widening coverage.")
    if not suggestions and lane_summaries:
        suggestions.append("Current weekly sample is stable; keep collecting nightly artifacts and compare the latest stable-pass streak per lane.")
    return suggestions


def _build_report_payload(
    *,
    project_root: Path,
    artifact_dirs: list[Path],
    summary_paths: list[Path],
    records: list[dict[str, Any]],
    warnings: list[str],
    lanes_filter: list[str],
    updated_since: datetime | None,
    updated_until: datetime | None,
    max_history_days: int | None,
) -> dict[str, Any]:
    lane_names = sorted({str(record.get("lane") or "") for record in records if str(record.get("lane") or "").strip()})
    lane_summaries = {
        lane: _aggregate_lane_summary([record for record in records if str(record.get("lane") or "") == lane])
        for lane in lane_names
    }
    latest_lane_states = {
        lane: dict(payload.get("latest_record") or {})
        for lane, payload in lane_summaries.items()
        if payload.get("latest_record")
    }
    blocked_lanes = sorted(
        lane
        for lane, payload in lane_summaries.items()
        if str(dict(payload.get("latest_record") or {}).get("alert_level") or "").lower() == "error"
    )
    status = "BLOCKED" if blocked_lanes else "PASS"
    timestamps = [record["captured_at"] for record in records]
    root_cause_bucket_counts = _count_by(records, "root_cause_bucket")
    payload = {
        "schema_version": "lane.trend.report.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "selection": {
            "artifact_dirs": [str(path.resolve()) for path in artifact_dirs],
            "summary_paths": [str(path.resolve()) for path in summary_paths],
            "lanes": [str(item) for item in lanes_filter],
            "updated_since": serialize_datetime(updated_since),
            "updated_until": serialize_datetime(updated_until),
            "max_history_days": max_history_days,
        },
        "summary": {
            "records_total": len(records),
            "lanes_observed": lane_names,
            "status_counts": _count_by(records, "status"),
            "alert_level_counts": _count_by(records, "alert_level"),
            "classification_counts": _count_by(records, "classification_id"),
            "root_cause_bucket_counts": root_cause_bucket_counts,
            "top_root_causes": ranked_root_causes(root_cause_bucket_counts),
            "window_start_utc": min(timestamps) if timestamps else None,
            "window_end_utc": max(timestamps) if timestamps else None,
            "blocked_lanes": blocked_lanes,
            "latest_lane_states": latest_lane_states,
        },
        "lanes": lane_summaries,
        "top_failure_signatures": _aggregate_failure_signatures(records),
        "suggestions": _build_trend_suggestions(records=records, lane_summaries=lane_summaries),
        "records": records,
        "warnings": warnings,
        "provenance": _build_provenance(
            command="lane-trend-report",
            project_root=project_root,
            planning_dir=None,
            resolved_planning_dirs=summary_paths,
        ),
    }
    return payload


def _render_markdown(report: dict[str, Any]) -> str:
    summary = dict(report.get("summary") or {})
    lines = [
        "# AUTOMATION_LANE_TREND_REPORT",
        "",
        f"- status: {report.get('status', 'UNKNOWN')}",
        f"- generated_at: {report.get('generated_at', '')}",
        "",
        "## Summary",
        "",
        f"- records_total: {summary.get('records_total', 0)}",
        f"- lanes_observed: {', '.join(summary.get('lanes_observed', [])) or '(none)'}",
        f"- window_start_utc: {summary.get('window_start_utc', '')}",
        f"- window_end_utc: {summary.get('window_end_utc', '')}",
        f"- blocked_lanes: {', '.join(summary.get('blocked_lanes', [])) or '(none)'}",
        "",
        "## Lane Table",
        "",
        "| lane | records | pass_rate | error_alert_rate | stable_pass_streak | latest_classification | latest_root_cause_bucket | latest_status |",
        "|---|---:|---:|---:|---:|---|---|---|",
    ]
    lanes = dict(report.get("lanes") or {})
    if lanes:
        for lane in sorted(lanes):
            payload = dict(lanes[lane] or {})
            latest = dict(payload.get("latest_record") or {})
            lines.append(
                f"| {lane} | {payload.get('records_total', 0)} | {float(payload.get('pass_rate', 0.0)):.4f} | {float(payload.get('error_alert_rate', 0.0)):.4f} | {payload.get('consecutive_stable_passes_from_latest', 0)} | {latest.get('classification_id', '')} | {latest.get('root_cause_bucket', '')} | {latest.get('status', '')} |"
            )
    else:
        lines.append("| (none) | 0 | 0.0000 | 0.0000 | 0 | - | - | - |")

    top_root_causes = list(summary.get("top_root_causes") or [])
    if top_root_causes:
        lines.extend(["", "## Top Root Causes", ""])
        for item in top_root_causes:
            lines.append(f"- {item.get('bucket', '')} ({item.get('label', '')}): {item.get('count', 0)}")

    lines.extend(["", "## Top Failure Signatures", "", "| signature | count | lanes | classifications |", "|---|---:|---|---|"])
    signatures = list(report.get("top_failure_signatures") or [])
    if signatures:
        for item in signatures:
            lines.append(
                f"| {item.get('signature', '')} | {item.get('count', 0)} | {', '.join(item.get('lanes', []))} | {', '.join(item.get('classifications', []))} |"
            )
    else:
        lines.append("| (none) | 0 | - | - |")

    lines.extend(["", "## Suggestions", ""])
    for item in list(report.get("suggestions") or []):
        lines.append(f"- {item}")

    warnings = [str(item) for item in list(report.get("warnings") or []) if str(item).strip()]
    lines.extend(["", "## Warnings", ""])
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("- (none)")
    return "\n".join(lines) + "\n"


def run_lane_trend_report_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    artifact_dirs: list[Path] = []
    explicit_summary_paths: list[Path] = []
    resolved_summary_paths: list[Path] = []
    try:
        artifact_dirs, explicit_summary_paths = _resolve_artifact_inputs(args, project_root=project_root)
        resolved_summary_paths = _discover_summary_paths(artifact_dirs=artifact_dirs, summary_paths=explicit_summary_paths)
        if not resolved_summary_paths:
            raise ValueError("no lane stability artifacts found under the selected inputs")

        warnings: list[str] = []
        loaded_records: list[dict[str, Any]] = []
        for summary_path in resolved_summary_paths:
            record, record_warnings = _load_artifact_record(summary_path)
            warnings.extend(record_warnings)
            if record is not None:
                loaded_records.append(record)

        lanes_filter = [str(item).strip() for item in list(getattr(args, "lane", []) or []) if str(item).strip()]
        updated_since = parse_datetime_filter(getattr(args, "updated_since", None), end_of_day=False)
        updated_until = parse_datetime_filter(getattr(args, "updated_until", None), end_of_day=True)
        max_history_days = _int_or_none(getattr(args, "max_history_days", _DEFAULT_MAX_HISTORY_DAYS))

        records = _filter_records(
            loaded_records,
            lanes=lanes_filter,
            updated_since=updated_since,
            updated_until=updated_until,
            max_history_days=max_history_days,
        )
        if not records:
            raise ValueError("no lane trend records matched the selected filters")

        report = _build_report_payload(
            project_root=project_root,
            artifact_dirs=artifact_dirs,
            summary_paths=resolved_summary_paths,
            records=records,
            warnings=warnings,
            lanes_filter=lanes_filter,
            updated_since=updated_since,
            updated_until=updated_until,
            max_history_days=max_history_days,
        )

        json_output = _resolve_optional_path(project_root, getattr(args, "json_output", None)) or (project_root / DEFAULT_LANE_TREND_JSON).resolve()
        markdown_output = _resolve_optional_path(project_root, getattr(args, "output", None)) or (project_root / DEFAULT_LANE_TREND_MD).resolve()
        _write_json(json_output, report)
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(_render_markdown(report), encoding="utf-8")

        payload = normalize_mutating_payload(
            {
                "status": str(report.get("status") or "PASS"),
                "schema_version": str(report.get("schema_version") or ""),
                "summary": dict(report.get("summary") or {}),
                "warnings": list(report.get("warnings") or []),
                "json_output": str(json_output.resolve()),
                "markdown_output": str(markdown_output.resolve()),
                "selection": dict(report.get("selection") or {}),
                "top_failure_signatures": list(report.get("top_failure_signatures") or []),
                "suggestions": list(report.get("suggestions") or []),
                "provenance": dict(report.get("provenance") or {}),
            },
            default_next_action="" if str(report.get("status") or "PASS") == "PASS" else "Inspect the latest blocked lane states and rerun the affected lane after remediation.",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if bool(getattr(args, "fail_on_block", False)) and str(report.get("status") or "PASS") == "BLOCKED":
            return 2
        return 0
    except ValueError as exc:
        payload = _error_payload(
            command="lane-trend-report",
            project_root=project_root,
            planning_dir=None,
            error=str(exc),
            error_code="lane_trend_report_failed",
            resolved_planning_dirs=resolved_summary_paths,
            remediation=[
                "Provide one or more --artifacts-dir / --summary-path inputs that contain lane_stability_*.json artifacts.",
                "Adjust --lane, --updated-since, --updated-until, or --max-history-days if the current filters exclude every record.",
            ],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


__all__ = ["DEFAULT_LANE_TREND_JSON", "DEFAULT_LANE_TREND_MD", "run_lane_trend_report_command"]


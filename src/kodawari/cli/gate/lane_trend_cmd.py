"""Aggregate lane triage artifacts into weekly standing-proof trend reports."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Any

from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.io_atomic import atomic_write_json, atomic_write_text, load_json_dict
from kodawari.cli.provenance import build_cli_provenance
from kodawari.cli.gate.root_cause_buckets import (
    classify_root_cause_bucket,
    ranked_root_causes,
    root_cause_bucket_label,
)


TRIAGE_SCHEMA_VERSION = "lane.triage.v1"
TREND_SCHEMA_VERSION = "lane.trend.v1"
DEFAULT_TREND_JSON = "lane_weekly_trend.json"
DEFAULT_TREND_MD = "lane_weekly_trend.md"
_INCIDENT_SCOPE_HINT = "Choose a repo-local `--planning-dir` or `--feature` before running `kodawari incident-ingest`."
_CRITICAL_INCIDENT_BUCKETS = frozenset(
    {
        "gate_blocked",
        "verify_setup",
        "verify_failure",
        "task_blocked",
        "max_cycles",
        "no_progress",
        "stuck",
        "implementation_error",
        "runtime_error",
        "unknown",
    }
)
_HIGH_INCIDENT_BUCKETS = frozenset({"env_missing", "rate_limit", "timeout", "external_gateway", "flaky_failure"})


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_or_empty(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value is not None else ""


def _discover_triage_paths(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(path.resolve() for path in root.rglob("lane_triage_*.json") if path.is_file())


def _load_triage_record(path: Path) -> dict[str, Any]:
    payload = load_json_dict(path, required=True)
    assert payload is not None
    lane = str(payload.get("lane") or "").strip() or "unknown"
    classification = str(payload.get("classification_id") or payload.get("classification") or "").strip() or "unknown"
    generated_at = _parse_iso_datetime(payload.get("generated_at_utc")) or datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    evidence = dict(payload.get("evidence") or {})
    raw_signatures = list(payload.get("failure_signatures") or evidence.get("top_failure_signatures") or [])
    failure_signatures: list[dict[str, Any]] = []
    for item in raw_signatures:
        row = dict(item) if isinstance(item, dict) else {"signature": str(item)}
        signature = str(row.get("signature") or row.get("sample") or "").strip()
        if not signature:
            continue
        failure_signatures.append(
            {
                "signature": signature,
                "count": int(row.get("count") or 0),
            }
        )
    missing_env = [str(item).strip() for item in list(payload.get("missing_env") or evidence.get("missing_env") or []) if str(item).strip()]
    operator_actions = [str(item).strip() for item in list(payload.get("operator_actions") or payload.get("remediation") or []) if str(item).strip()]
    root_cause_bucket = str(payload.get("root_cause_bucket") or "").strip()
    if not root_cause_bucket:
        failure_messages = [str(item.get("signature") or "") for item in failure_signatures]
        failure_messages.extend(str(item).strip() for item in list(evidence.get("top_messages") or []) if str(item).strip())
        root_cause_bucket = classify_root_cause_bucket(
            classification_id=classification,
            status=str(payload.get("status") or ""),
            stop_reason=str(payload.get("stop_reason") or ""),
            gate_status=str(payload.get("gate_status") or ""),
            verify_status=str(payload.get("verify_status") or ""),
            round_outcome=str(payload.get("round_outcome") or ""),
            run_outcome=str(payload.get("run_outcome") or ""),
            error_categories=list(payload.get("error_categories") or []),
            missing_env=missing_env,
            failure_messages=failure_messages,
            blocking_reason=str(payload.get("blocking_reason") or ""),
            headline=str(payload.get("headline") or payload.get("summary") or ""),
        )
    record = {
        "path": str(path),
        "summary_path": str(payload.get("summary_path") or "").strip(),
        "lane": lane,
        "classification": classification,
        "status": str(payload.get("status") or "").strip().upper() or "UNKNOWN",
        "alert_level": str(payload.get("alert_level") or payload.get("severity") or "").strip().lower() or "unknown",
        "headline": str(payload.get("headline") or payload.get("summary") or "").strip(),
        "generated_at": generated_at,
        "generated_at_utc": _iso_or_empty(generated_at),
        "passed_runs": int(payload.get("passed_runs") or 0),
        "failed_runs": int(payload.get("failed_runs") or 0),
        "skipped_runs": int(payload.get("skipped_runs") or 0),
        "missing_env": missing_env,
        "failure_signatures": failure_signatures,
        "operator_actions": operator_actions,
        "root_cause_bucket": root_cause_bucket,
        "root_cause_label": root_cause_bucket_label(root_cause_bucket),
        "triage_version": str(payload.get("triage_version") or payload.get("schema_version") or "").strip(),
    }
    return record


def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, int, int, int]] = set()
    deduped: list[dict[str, Any]] = []
    for item in sorted(records, key=lambda row: (row["lane"], row["generated_at_utc"], row["path"])):
        key = (
            str(item["lane"]),
            str(item["generated_at_utc"]),
            str(item["classification"]),
            int(item["passed_runs"]),
            int(item["failed_runs"]),
            int(item["skipped_runs"]),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _within_history_window(record: dict[str, Any], *, max_history_days: int | None, now: datetime) -> bool:
    if max_history_days is None:
        return True
    generated_at = record.get("generated_at")
    if not isinstance(generated_at, datetime):
        return False
    return generated_at >= now - timedelta(days=max_history_days)


def _stable_classification(record: dict[str, Any]) -> bool:
    return str(record.get("classification") or "") == "lane.stable_pass"


def _current_pass_streak(records: list[dict[str, Any]]) -> int:
    streak = 0
    for item in reversed(records):
        if not _stable_classification(item):
            break
        streak += 1
    return streak


def _longest_pass_streak(records: list[dict[str, Any]]) -> int:
    best = 0
    current = 0
    for item in records:
        if _stable_classification(item):
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _current_non_pass_streak(records: list[dict[str, Any]]) -> int:
    streak = 0
    for item in reversed(records):
        if _stable_classification(item):
            break
        streak += 1
    return streak


def _lane_standing_state(*, latest_classification: str, current_pass_streak: int, required_pass_streak: int) -> str:
    if not latest_classification:
        return "no_data"
    if latest_classification == "lane.stable_pass":
        return "stable" if current_pass_streak >= required_pass_streak else "recovering"
    if latest_classification in {"lane.integration_env_missing", "lane.integration_env_missing_fail_closed"}:
        return "env_blocked"
    if latest_classification == "lane.flaky_failure":
        return "degraded"
    return "blocked"


def _top_signature_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in records:
        if _stable_classification(item):
            continue
        for signature_row in list(item.get("failure_signatures") or []):
            signature = str(dict(signature_row).get("signature") or "").strip()
            if not signature:
                continue
            counts[signature] = counts.get(signature, 0) + max(1, int(dict(signature_row).get("count") or 0))
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return [{"signature": signature, "count": count} for signature, count in ranked[:5]]


def _missing_env_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in records:
        for name in list(item.get("missing_env") or []):
            counts[str(name)] = counts.get(str(name), 0) + 1
    return dict(sorted(counts.items()))


def _root_cause_bucket_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in records:
        bucket = str(item.get("root_cause_bucket") or "").strip()
        if not bucket:
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
    return dict(sorted(counts.items()))


def _slug_token(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or "unknown"


def _quote_powershell_arg(value: str) -> str:
    return "'" + str(value or "").replace("'", "''") + "'"


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(text)
    return deduped


def _incident_recommended(summary: dict[str, Any]) -> bool:
    state = str(summary.get("standing_proof_state") or "")
    if state in {"stable", "recovering", "no_data"}:
        return False
    return True


def _incident_severity(summary: dict[str, Any]) -> str:
    state = str(summary.get("standing_proof_state") or "")
    bucket = str(summary.get("latest_root_cause_bucket") or "")
    streak = int(summary.get("current_non_pass_streak") or 0)
    if state == "recovering":
        return "medium"
    if state == "env_blocked":
        return "high"
    if state == "degraded":
        return "critical" if streak >= 2 else "high"
    if bucket in _CRITICAL_INCIDENT_BUCKETS:
        return "critical" if streak >= 2 else "high"
    if bucket in _HIGH_INCIDENT_BUCKETS:
        return "high"
    return "critical" if state == "blocked" else "high"


def _incident_reason(summary: dict[str, Any]) -> str:
    state = str(summary.get("standing_proof_state") or "")
    if state == "recovering":
        return "Latest lane runs are passing again, but the required consecutive pass streak is not met yet."
    if state == "env_blocked":
        return "Environment configuration is incomplete and the weekly standing proof is still blocked; hand this off to operator/incident workflow."
    if state == "no_data":
        return "No lane triage artifacts matched the selected history window."
    return "Standing proof remains non-stable in the weekly trend and should be handed off to operator/incident workflow."


def _incident_summary_text(summary: dict[str, Any]) -> str:
    lane = str(summary.get("lane") or "")
    state = str(summary.get("standing_proof_state") or "")
    bucket = str(summary.get("latest_root_cause_bucket") or "")
    label = str(summary.get("latest_root_cause_label") or "")
    classification = str(summary.get("latest_classification") or "")
    headline = str(summary.get("latest_headline") or "")
    pass_streak = int(summary.get("current_pass_streak") or 0)
    required_pass_streak = int(summary.get("required_pass_streak") or 0)
    non_pass_streak = int(summary.get("current_non_pass_streak") or 0)
    bucket_text = f"{bucket} ({label})" if label else bucket
    if state == "recovering":
        return (
            f"{lane} lane is recovering but has only restored {pass_streak}/{required_pass_streak} required consecutive stable passes. "
            f"Latest classification: {classification or 'unknown'}."
        )
    if state == "env_blocked":
        return (
            f"{lane} lane is blocked by missing integration environment in weekly standing proof. "
            f"Latest root cause: {bucket_text or 'unknown'}."
        )
    return (
        f"{lane} lane standing proof is {state or 'non_stable'} with latest root cause {bucket_text or 'unknown'} "
        f"and current non-pass streak {non_pass_streak}. Latest classification: {classification or 'unknown'}."
        + (f" {headline}" if headline else "")
    )


def _incident_impact_text(summary: dict[str, Any]) -> str:
    state = str(summary.get("standing_proof_state") or "")
    bucket = str(summary.get("latest_root_cause_bucket") or "")
    missing_env = list(dict(summary.get("missing_env_counts") or {}).keys())
    if state == "env_blocked" and missing_env:
        return "Standing proof cannot count as green coverage until missing env is restored: " + ", ".join(missing_env)
    if state == "recovering":
        return "Standing proof is not yet eligible for weekly signoff because the pass streak is still below threshold."
    return f"Standing proof for this lane is not yet stable; latest root cause bucket is {bucket or 'unknown'}."


def _incident_tags(summary: dict[str, Any]) -> list[str]:
    return _dedupe_strings(
        [
            "lane-trend",
            "standing-proof",
            f"lane-{str(summary.get('lane') or '')}",
            f"state-{str(summary.get('standing_proof_state') or '')}",
            f"root-cause-{str(summary.get('latest_root_cause_bucket') or '')}",
        ]
    )


def _incident_evidence_files(summary: dict[str, Any]) -> list[str]:
    return _dedupe_strings(
        [
            str(summary.get("latest_triage_path") or ""),
            str(summary.get("latest_summary_path") or ""),
        ]
    )


def _incident_suggested_command(candidate: dict[str, Any]) -> str:
    tags = [str(item) for item in list(candidate.get("tags") or []) if str(item).strip()]
    evidence_files = [str(item) for item in list(candidate.get("evidence_files") or []) if str(item).strip()]
    parts = [
        "kodawari incident-ingest",
        "--project-root <root>",
        "--planning-dir <planning-dir>",
        f"--incident-id {_quote_powershell_arg(str(candidate.get('incident_id') or ''))}",
        f"--source {_quote_powershell_arg(str(candidate.get('source') or 'standing-proof'))}",
        f"--severity {_quote_powershell_arg(str(candidate.get('severity') or 'high'))}",
        f"--title {_quote_powershell_arg(str(candidate.get('title') or ''))}",
        f"--summary {_quote_powershell_arg(str(candidate.get('summary') or ''))}",
    ]
    component = str(candidate.get("component") or "").strip()
    impact = str(candidate.get("impact") or "").strip()
    owner = str(candidate.get("owner") or "").strip()
    if component:
        parts.append(f"--component {_quote_powershell_arg(component)}")
    if impact:
        parts.append(f"--impact {_quote_powershell_arg(impact)}")
    if owner:
        parts.append(f"--owner {_quote_powershell_arg(owner)}")
    parts.extend(f"--tag {_quote_powershell_arg(item)}" for item in tags)
    parts.extend(f"--evidence {_quote_powershell_arg(item)}" for item in evidence_files)
    return " ".join(parts)


def _build_incident_candidate(summary: dict[str, Any]) -> dict[str, Any]:
    state = str(summary.get("standing_proof_state") or "")
    if state in {"stable", "no_data"}:
        return {}
    lane = str(summary.get("lane") or "unknown")
    bucket = str(summary.get("latest_root_cause_bucket") or "unknown")
    severity = _incident_severity(summary)
    summary_text = _incident_summary_text(summary)
    impact = _incident_impact_text(summary)
    candidate = {
        "lane": lane,
        "standing_proof_state": state,
        "latest_root_cause_bucket": bucket,
        "recommended": _incident_recommended(summary),
        "reason": _incident_reason(summary),
        "planning_scope_hint": _INCIDENT_SCOPE_HINT,
        "incident_id": f"lane-{_slug_token(lane)}-{_slug_token(bucket)}",
        "source": "standing-proof",
        "severity": severity,
        "title": f"{lane} lane standing proof {state}",
        "summary": summary_text,
        "component": f"ci:{lane}_lane",
        "impact": impact,
        "owner": "workflow-operator",
        "tags": _incident_tags(summary),
        "evidence_files": _incident_evidence_files(summary),
    }
    candidate["suggested_command"] = _incident_suggested_command(candidate)
    return candidate


def _build_lane_summary(
    lane: str,
    records: list[dict[str, Any]],
    *,
    required_pass_streak: int,
) -> dict[str, Any]:
    ordered = sorted(records, key=lambda row: row["generated_at"])
    if not ordered:
        summary = {
            "lane": lane,
            "reports_total": 0,
            "standing_proof_state": "no_data",
            "latest_generated_at_utc": "",
            "latest_classification": "",
            "latest_status": "",
            "latest_triage_path": "",
            "latest_summary_path": "",
            "current_pass_streak": 0,
            "current_non_pass_streak": 0,
            "longest_pass_streak": 0,
            "required_pass_streak": required_pass_streak,
            "pass_rate": 0.0,
            "classification_counts": {},
            "root_cause_bucket_counts": {},
            "top_root_causes": [],
            "latest_root_cause_bucket": "",
            "latest_root_cause_label": "",
            "alert_level_counts": {},
            "top_failure_signatures": [],
            "missing_env_counts": {},
            "latest_operator_actions": [],
            "latest_headline": "",
            "incident_candidate": {},
        }
        summary["incident_candidate"] = _build_incident_candidate(summary)
        return summary

    latest = ordered[-1]
    classifications = Counter(str(item.get("classification") or "") for item in ordered)
    alert_levels = Counter(str(item.get("alert_level") or "") for item in ordered)
    current_pass_streak = _current_pass_streak(ordered)
    current_non_pass_streak = _current_non_pass_streak(ordered)
    pass_reports = sum(1 for item in ordered if _stable_classification(item))
    standing_state = _lane_standing_state(
        latest_classification=str(latest.get("classification") or ""),
        current_pass_streak=current_pass_streak,
        required_pass_streak=required_pass_streak,
    )
    summary = {
        "lane": lane,
        "reports_total": len(ordered),
        "standing_proof_state": standing_state,
        "latest_generated_at_utc": str(latest.get("generated_at_utc") or ""),
        "latest_classification": str(latest.get("classification") or ""),
        "latest_status": str(latest.get("status") or ""),
        "latest_triage_path": str(latest.get("path") or ""),
        "latest_summary_path": str(latest.get("summary_path") or ""),
        "current_pass_streak": current_pass_streak,
        "current_non_pass_streak": current_non_pass_streak,
        "longest_pass_streak": _longest_pass_streak(ordered),
        "required_pass_streak": required_pass_streak,
        "pass_rate": round(pass_reports / len(ordered), 3),
        "classification_counts": dict(sorted(classifications.items())),
        "root_cause_bucket_counts": _root_cause_bucket_counts(ordered),
        "top_root_causes": ranked_root_causes(_root_cause_bucket_counts(ordered)),
        "latest_root_cause_bucket": str(latest.get("root_cause_bucket") or ""),
        "latest_root_cause_label": str(latest.get("root_cause_label") or ""),
        "alert_level_counts": dict(sorted(alert_levels.items())),
        "top_failure_signatures": _top_signature_rows(ordered),
        "missing_env_counts": _missing_env_counts(ordered),
        "latest_operator_actions": list(latest.get("operator_actions") or [])[:3],
        "latest_headline": str(latest.get("headline") or ""),
        "incident_candidate": {},
    }
    summary["incident_candidate"] = _build_incident_candidate(summary)
    return summary


def _collect_incident_candidates(lane_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in lane_summaries:
        candidate = dict(item.get("incident_candidate") or {})
        if candidate:
            candidates.append(candidate)
    return candidates


def _build_overview(
    lane_summaries: list[dict[str, Any]],
    *,
    required_pass_streak: int,
) -> tuple[str, dict[str, Any], list[str], str]:
    stable_lanes = [item["lane"] for item in lane_summaries if item["standing_proof_state"] == "stable"]
    non_stable = [item["lane"] for item in lane_summaries if item["standing_proof_state"] != "stable"]
    recommended_incidents = [
        str(dict(item.get("incident_candidate") or {}).get("lane") or item["lane"])
        for item in lane_summaries
        if bool(dict(item.get("incident_candidate") or {}).get("recommended"))
    ]
    overview = {
        "required_pass_streak": required_pass_streak,
        "lanes_total": len(lane_summaries),
        "lanes_stable": len(stable_lanes),
        "lanes_non_stable": len(non_stable),
        "stable_lanes": stable_lanes,
        "non_stable_lanes": non_stable,
        "lanes_incident_recommended": len(recommended_incidents),
        "incident_recommended_lanes": recommended_incidents,
    }
    if lane_summaries and not non_stable:
        status = "PASS"
        remediation: list[str] = []
        next_action = "Keep the scheduled lanes running and review the weekly trend after the next artifact download."
    else:
        status = "BLOCKED"
        remediation = [
            "Download the latest lane artifacts and rerun `kodawari lane-trend` after the next nightly window.",
            "Prioritize lanes whose `standing_proof_state` is not `stable`, then follow the latest operator actions for that lane.",
        ]
        next_action = "Recover each non-stable lane until the required pass streak is restored."
    return status, overview, remediation, next_action


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# LANE_WEEKLY_TREND",
        "",
        f"- status: {payload.get('status', '')}",
        f"- trend_version: {payload.get('trend_version', '')}",
        f"- artifacts_root: {payload.get('artifacts_root', '')}",
        f"- generated_at_utc: {payload.get('generated_at_utc', '')}",
        f"- max_history_days: {payload.get('max_history_days', '')}",
        f"- required_pass_streak: {payload.get('required_pass_streak', '')}",
        "",
        "## Overview",
        "",
        f"- selected_lanes: {', '.join(payload.get('selected_lanes', [])) or '-'}",
        f"- lanes_stable: {payload.get('overview', {}).get('lanes_stable', 0)}/{payload.get('overview', {}).get('lanes_total', 0)}",
        f"- lanes_incident_recommended: {payload.get('overview', {}).get('lanes_incident_recommended', 0)}",
        f"- next_action: {payload.get('next_action', '')}",
    ]

    top_root_causes = list(payload.get("top_root_causes") or [])
    if top_root_causes:
        lines.extend(["", "## Top Root Causes", ""])
        for item in top_root_causes:
            lines.append(f"- {item.get('bucket', '')} ({item.get('label', '')}): {item.get('count', 0)}")

    incident_candidates = list(payload.get("incident_candidates") or [])
    if incident_candidates:
        lines.extend(["", "## Incident Candidates", ""])
        for item in incident_candidates:
            lines.append(
                f"- lane={item.get('lane', '')}, recommended={item.get('recommended', False)}, severity={item.get('severity', '')}, incident_id={item.get('incident_id', '')}"
            )
    recommended_incidents = list(payload.get("recommended_incidents") or [])
    if recommended_incidents:
        lines.extend(["", "## Recommended Incidents", ""])
        for item in recommended_incidents:
            lines.append(f"- {item.get('lane', '')}: {item.get('severity', '')} | {item.get('title', '')}")
            lines.append(f"- suggested_command: {item.get('suggested_command', '')}")

    lines.extend(
        [
            "",
            "## Lane Summary",
            "",
            "| lane | standing_proof_state | latest_classification | latest_root_cause_bucket | current_pass_streak | required_pass_streak | reports_total | pass_rate |",
            "|---|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for item in list(payload.get("lanes") or []):
        latest_root_cause_bucket = str(item.get("latest_root_cause_bucket") or "")
        latest_root_cause_label = str(item.get("latest_root_cause_label") or "")
        latest_root_cause_text = latest_root_cause_bucket
        if latest_root_cause_label:
            latest_root_cause_text = f"{latest_root_cause_bucket} ({latest_root_cause_label})"
        lines.append(
            f"| {item.get('lane', '')} | {item.get('standing_proof_state', '')} | {item.get('latest_classification', '')} | "
            f"{latest_root_cause_text} | {item.get('current_pass_streak', 0)} | {item.get('required_pass_streak', 0)} | {item.get('reports_total', 0)} | {item.get('pass_rate', 0.0):.3f} |"
        )

    warnings = [str(item) for item in list(payload.get("warnings") or []) if str(item).strip()]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend([f"- {item}" for item in warnings])

    for item in list(payload.get("lanes") or []):
        lines.extend(
            [
                "",
                f"## Lane: {item.get('lane', '')}",
                "",
                f"- latest_generated_at_utc: {item.get('latest_generated_at_utc', '')}",
                f"- latest_status: {item.get('latest_status', '')}",
                f"- latest_headline: {item.get('latest_headline', '')}",
                f"- latest_root_cause_bucket: {item.get('latest_root_cause_bucket', '')}",
                f"- latest_root_cause_label: {item.get('latest_root_cause_label', '')}",
                f"- current_non_pass_streak: {item.get('current_non_pass_streak', 0)}",
                f"- longest_pass_streak: {item.get('longest_pass_streak', 0)}",
                "",
                "### Classification Counts",
                "",
            ]
        )
        classification_counts = dict(item.get("classification_counts") or {})
        if classification_counts:
            for key, value in classification_counts.items():
                lines.append(f"- {key}: {value}")
        else:
            lines.append("- (none)")
        lines.extend(["", "### Top Failure Signatures", ""])
        signatures = list(item.get("top_failure_signatures") or [])
        if signatures:
            for row in signatures:
                lines.append(f"- {row.get('signature', '')}: {row.get('count', 0)}")
        else:
            lines.append("- (none)")
        lines.extend(["", "### Root Cause Buckets", ""])
        root_causes = list(item.get("top_root_causes") or [])
        if root_causes:
            for row in root_causes:
                lines.append(f"- {row.get('bucket', '')} ({row.get('label', '')}): {row.get('count', 0)}")
        else:
            lines.append("- (none)")
        lines.extend(["", "### Missing Env Counts", ""])
        missing_env_counts = dict(item.get("missing_env_counts") or {})
        if missing_env_counts:
            for key, value in missing_env_counts.items():
                lines.append(f"- {key}: {value}")
        else:
            lines.append("- (none)")
        lines.extend(["", "### Latest Operator Actions", ""])
        actions = list(item.get("latest_operator_actions") or [])
        if actions:
            lines.extend([f"- {action}" for action in actions])
        else:
            lines.append("- (none)")
        candidate = dict(item.get("incident_candidate") or {})
        if candidate:
            lines.extend(
                [
                    "",
                    "### Incident Candidate",
                    "",
                    f"- recommended: {candidate.get('recommended', False)}",
                    f"- severity: {candidate.get('severity', '')}",
                    f"- incident_id: {candidate.get('incident_id', '')}",
                    f"- source: {candidate.get('source', '')}",
                    f"- reason: {candidate.get('reason', '')}",
                    f"- planning_scope_hint: {candidate.get('planning_scope_hint', '')}",
                    f"- suggested_command: {candidate.get('suggested_command', '')}",
                ]
            )
    return "\n".join(lines).strip() + "\n"


def run_lane_trend_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    artifacts_root = Path(getattr(args, "artifacts_root", "") or (project_root / "planning")).resolve()
    json_output = Path(getattr(args, "json_output", "") or (project_root / "planning" / DEFAULT_TREND_JSON)).resolve()
    markdown_output = Path(getattr(args, "markdown_output", "") or (project_root / "planning" / DEFAULT_TREND_MD)).resolve()
    selected_lanes = [str(item).strip() for item in list(getattr(args, "lane", []) or []) if str(item).strip()]
    lane_filter = set(selected_lanes)
    max_history_days = int(getattr(args, "max_history_days", 7))
    required_pass_streak = max(1, int(getattr(args, "required_pass_streak", 3)))
    now = datetime.now(timezone.utc)

    try:
        warnings: list[str] = []
        records: list[dict[str, Any]] = []
        for path in _discover_triage_paths(artifacts_root):
            try:
                record = _load_triage_record(path)
            except Exception as exc:
                try:
                    display_path = str(path.relative_to(artifacts_root))
                except ValueError:
                    display_path = str(path)
                warnings.append(f"{display_path}: skipped invalid lane triage artifact ({exc.__class__.__name__})")
                continue
            if record["triage_version"] and record["triage_version"] != TRIAGE_SCHEMA_VERSION:
                warnings.append(f"{path.name}: unexpected triage version {record['triage_version']}")
            if lane_filter and str(record["lane"]) not in lane_filter:
                continue
            if not _within_history_window(record, max_history_days=max_history_days, now=now):
                continue
            records.append(record)
        records = _dedupe_records(records)

        lanes = sorted(lane_filter or {str(item["lane"]) for item in records})
        lane_summaries = [_build_lane_summary(lane, [item for item in records if item["lane"] == lane], required_pass_streak=required_pass_streak) for lane in lanes]
        status, overview, remediation, next_action = _build_overview(lane_summaries, required_pass_streak=required_pass_streak)
        root_cause_bucket_counts = _root_cause_bucket_counts(records)
        incident_candidates = _collect_incident_candidates(lane_summaries)
        recommended_incident_candidates = [item for item in incident_candidates if bool(item.get("recommended", False))]
        if recommended_incident_candidates:
            remediation = list(remediation) + [
                "For lanes with `incident_candidate.recommended=true`, choose a repo-local planning scope and run the suggested `kodawari incident-ingest` template.",
            ]
            next_action = "Open or update field reports for recommended lane incidents, then continue the latest lane recovery actions."
        payload = normalize_mutating_payload(
            {
                "status": status,
                "entrypoint": "kodawari lane-trend",
                "schema_version": TREND_SCHEMA_VERSION,
                "trend_version": TREND_SCHEMA_VERSION,
                "artifacts_root": str(artifacts_root),
                "generated_at_utc": _iso_or_empty(now),
                "max_history_days": max_history_days,
                "required_pass_streak": required_pass_streak,
                "selected_lanes": lanes,
                "reports_considered": len(records),
                "warnings": warnings,
                "overview": overview,
                "lanes": lane_summaries,
                "root_cause_bucket_counts": root_cause_bucket_counts,
                "top_root_causes": ranked_root_causes(root_cause_bucket_counts),
                "incident_candidates": incident_candidates,
                "recommended_incidents": recommended_incident_candidates,
                "recommended_incident_candidates_total": len(recommended_incident_candidates),
                "remediation": remediation,
                "next_action": next_action,
                "provenance": build_cli_provenance(
                    command="lane-trend",
                    project_root=project_root,
                    planning_dir=None,
                    module_file=Path(__file__),
                ),
            }
        )
        atomic_write_json(json_output, payload)
        atomic_write_text(markdown_output, _render_markdown(payload))
        payload["artifacts"] = {
            "LANE_WEEKLY_TREND.json": str(json_output),
            "LANE_WEEKLY_TREND.md": str(markdown_output),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if bool(getattr(args, "fail_on_block", False)) and str(payload.get("status") or "").upper() != "PASS":
            return 2
        return 0
    except Exception as exc:
        payload = normalize_mutating_payload(
            build_error_payload(
                command="lane-trend",
                project_root=project_root,
                planning_dir=None,
                module_file=Path(__file__),
                error=str(exc),
                error_code="lane_trend_failed",
                remediation=["Inspect the lane triage artifacts root and rerun `kodawari lane-trend`."],
                extra={"artifacts_root": str(artifacts_root)},
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


__all__ = ["run_lane_trend_command"]


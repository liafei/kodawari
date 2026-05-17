"""Lane stability triage command for operator/CI recovery guidance."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from kodawari.cli.contract.command_contract import build_error_payload, normalize_mutating_payload
from kodawari.cli.io_atomic import atomic_write_json, atomic_write_text, load_json_dict
from kodawari.cli.provenance import build_cli_provenance
from kodawari.cli.gate.root_cause_buckets import classify_root_cause_bucket, root_cause_bucket_label


_INTEGRATION_ENV = (
    "WORKFLOW_REVIEWER_API_KEY",
    "WORKFLOW_REVIEWER_BASE_URL",
    "WORKFLOW_OPUS_API_KEY",
    "WORKFLOW_OPUS_GATEWAY",
)
_TRIAGE_SCHEMA_VERSION = "lane.triage.v1"
_CLASSIFICATION_METADATA: dict[str, tuple[str, str]] = {
    "lane_pass": ("lane.stable_pass", "Stable pass"),
    "integration_env_missing": ("lane.integration_env_missing", "Integration environment missing"),
    "lane_flaky": ("lane.flaky_failure", "Flaky lane"),
    "lane_failure_repeated": ("lane.consistent_failure", "Consistent lane failure"),
    "lane_skipped": ("lane.unclassified", "Lane skipped"),
    "lane_failure": ("lane.consistent_failure", "Consistent lane failure"),
}


def _normalize_signature(message: str) -> str:
    text = str(message or "").strip().lower()
    if not text:
        return "(empty-message)"
    normalized = text
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}t\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:\d{2})?", "<ts>", normalized)
    normalized = re.sub(r"\b\d+\b", "<n>", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized[:240]


def _repeat_command(lane: str, repeat_requested: int) -> str:
    repeat = max(1, int(repeat_requested or 1))
    script_name = f"run_{lane}_lane_repeat.ps1"
    suffix = " -FailIfSkipped" if str(lane) == "integration" else ""
    return f"powershell -ExecutionPolicy Bypass -File .\\scripts\\{script_name} -Repeat {repeat}{suffix}"


def _load_summary(path: Path) -> dict[str, Any]:
    payload = load_json_dict(path, required=True)
    assert payload is not None
    return payload


def _failed_runs(summary: dict[str, Any]) -> list[dict[str, Any]]:
    runs = list(summary.get("runs") or [])
    return [dict(item) for item in runs if str(dict(item).get("status") or "").upper() in {"FAIL", "SKIP"}]


def _messages(runs: list[dict[str, Any]]) -> list[str]:
    return [str(run.get("message") or "").strip() for run in runs if str(run.get("message") or "").strip()]


def _missing_env(runs: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for run in runs:
        for name in list(run.get("missing_env") or []):
            value = str(name).strip()
            if value and value not in names:
                names.append(value)
    return names


def _top_signatures(messages: list[str]) -> list[dict[str, Any]]:
    counts: dict[str, dict[str, Any]] = {}
    for message in messages:
        signature = _normalize_signature(message)
        entry = counts.setdefault(signature, {"signature": signature, "count": 0, "sample": message})
        entry["count"] += 1
    ranked = sorted(counts.values(), key=lambda item: int(item["count"]), reverse=True)
    return ranked[:3]


def _incident_candidate(
    *,
    recommended: bool,
    lane: str,
    severity: str,
    summary_text: str,
    blocking_reason: str,
) -> dict[str, Any]:
    title = f"{lane} lane standing proof degraded"
    return {
        "recommended": bool(recommended),
        "severity": severity if recommended else "",
        "title": title if recommended else "",
        "summary": summary_text if recommended else "",
        "component": f"ci:{lane}_lane" if recommended else "",
        "impact": blocking_reason if recommended else "",
        "suggested_command": (
            "kodawari incident-ingest --project-root <root> "
            f"--incident-id {lane}-lane "
            f'--severity {severity} --title "{title}" '
            f'--summary "{summary_text}" --component ci:{lane}_lane --impact "{blocking_reason or summary_text}"'
        )
        if recommended
        else "",
    }


def _build_triage_payload(summary: dict[str, Any], *, project_root: Path, summary_path: Path) -> dict[str, Any]:
    lane = str(summary.get("lane") or "unknown").strip() or "unknown"
    repeat_requested = int(summary.get("repeat_requested") or 0)
    repeat_completed = int(summary.get("repeat_completed") or 0)
    passed_runs = int(summary.get("passed_runs") or 0)
    failed_runs = int(summary.get("failed_runs") or 0)
    skipped_runs = int(summary.get("skipped_runs") or 0)
    non_pass_runs = _failed_runs(summary)
    messages = _messages(non_pass_runs)
    missing_env = _missing_env(non_pass_runs)
    top_signatures = _top_signatures(messages)
    top_signature_count = int(top_signatures[0]["count"]) if top_signatures else 0
    blocking_reason = messages[0] if messages else ""
    repeat_cmd = _repeat_command(lane, repeat_requested or repeat_completed or 1)

    if failed_runs == 0 and skipped_runs == 0 and passed_runs == repeat_completed:
        classification = "lane_pass"
        status = "PASS"
        severity = "info"
        standing_proof_status = "pass"
        summary_text = f"{lane} lane passed {passed_runs}/{repeat_completed} runs."
        remediation: list[str] = []
        next_action = "Keep the scheduled lane running and review the uploaded triage artifacts during weekly operator checks."
        incident = _incident_candidate(
            recommended=False,
            lane=lane,
            severity=severity,
            summary_text=summary_text,
            blocking_reason=blocking_reason,
        )
    elif lane == "integration" and any(name in _INTEGRATION_ENV for name in missing_env):
        classification = "integration_env_missing"
        status = "BLOCKED"
        severity = "high"
        standing_proof_status = "blocked"
        summary_text = "integration lane could not produce standing proof because required real-review environment variables were missing."
        remediation = [
            "Provision `WORKFLOW_REVIEWER_API_KEY` and `WORKFLOW_REVIEWER_BASE_URL` in the integration runtime or CI secret store.",
            "Keep `WORKFLOW_REVIEW_ENABLED=1` and `WORKFLOW_REVIEW_REQUIRED=1` mapped in the integration workflow.",
            f"Rerun `{repeat_cmd}` after restoring the integration environment.",
        ]
        next_action = "Restore the integration environment and rerun the fixed integration lane recipe."
        incident = _incident_candidate(
            recommended=False,
            lane=lane,
            severity=severity,
            summary_text=summary_text,
            blocking_reason=blocking_reason,
        )
    elif failed_runs > 0 and passed_runs > 0:
        classification = "lane_flaky"
        status = "BLOCKED"
        severity = "critical"
        standing_proof_status = "degraded"
        summary_text = f"{lane} lane is unstable: {passed_runs} pass, {failed_runs} fail across {repeat_completed} runs."
        remediation = [
            f"Rerun `{repeat_cmd}` locally or in CI to reproduce the same lane recipe with the same repeat count.",
            "Inspect the first failing run signature in `lane_triage` and the uploaded lane logs before changing product code.",
            "If the same signature persists across scheduled runs, promote it to an incident with `kodawari incident-ingest`.",
        ]
        next_action = "Treat the standing proof as degraded until consecutive clean runs are restored."
        incident = _incident_candidate(
            recommended=True,
            lane=lane,
            severity="critical",
            summary_text=summary_text,
            blocking_reason=blocking_reason,
        )
    elif failed_runs > 1 and top_signature_count > 1:
        classification = "lane_failure_repeated"
        status = "BLOCKED"
        severity = "critical"
        standing_proof_status = "blocked"
        summary_text = f"{lane} lane failed repeatedly with the same signature across {failed_runs}/{repeat_completed} runs."
        remediation = [
            f"Reproduce the repeated failure with `{repeat_cmd}` before widening the investigation scope.",
            "Use the repeated failure signature in `lane_triage` to isolate the owning stage or external dependency.",
            "Escalate with `kodawari incident-ingest` if the same signature survives the next scheduled run.",
        ]
        next_action = "Investigate the repeated failure signature and convert it into an incident candidate if it persists."
        incident = _incident_candidate(
            recommended=True,
            lane=lane,
            severity="critical",
            summary_text=summary_text,
            blocking_reason=blocking_reason,
        )
    elif skipped_runs > 0:
        classification = "lane_skipped"
        status = "BLOCKED"
        severity = "high" if lane == "integration" else "medium"
        standing_proof_status = "blocked" if lane == "integration" else "degraded"
        summary_text = f"{lane} lane completed with {skipped_runs} skipped run(s); standing proof is incomplete."
        remediation = [
            f"Rerun `{repeat_cmd}` and confirm the lane reaches a terminal PASS without skips.",
            "Inspect the triage evidence to determine whether the skip came from environment setup or lane recipe drift.",
        ]
        next_action = "Resolve the skip cause before treating this lane as standing proof."
        incident = _incident_candidate(
            recommended=False,
            lane=lane,
            severity=severity,
            summary_text=summary_text,
            blocking_reason=blocking_reason,
        )
    else:
        classification = "lane_failure"
        status = "BLOCKED"
        severity = "high"
        standing_proof_status = "blocked"
        summary_text = f"{lane} lane failed {failed_runs}/{repeat_completed} runs."
        remediation = [
            f"Rerun `{repeat_cmd}` to confirm the failure against the fixed lane recipe.",
            "Inspect the uploaded lane stability summary and triage signature before changing unrelated code.",
            "If the failure is integration-only, verify gateway health and secret availability before escalating to product owners.",
        ]
        next_action = "Reproduce the failing lane and isolate whether the breakage is code, gateway, or environment."
        incident = _incident_candidate(
            recommended=False,
            lane=lane,
            severity=severity,
            summary_text=summary_text,
            blocking_reason=blocking_reason,
        )

    classification_id, classification_label = _CLASSIFICATION_METADATA.get(
        classification, ("lane.unclassified", "Unclassified lane outcome")
    )
    root_cause_bucket = classify_root_cause_bucket(
        classification_id=classification_id,
        status=status,
        missing_env=missing_env,
        failure_messages=messages[:5],
        blocking_reason=blocking_reason,
        headline=summary_text,
    )
    payload = normalize_mutating_payload(
        {
            "schema_version": _TRIAGE_SCHEMA_VERSION,
            "triage_version": _TRIAGE_SCHEMA_VERSION,
            "status": status,
            "entrypoint": "kodawari lane-triage",
            "lane": lane,
            "classification": classification,
            "classification_id": classification_id,
            "classification_label": classification_label,
            "root_cause_bucket": root_cause_bucket,
            "root_cause_label": root_cause_bucket_label(root_cause_bucket),
            "severity": severity,
            "standing_proof_status": standing_proof_status,
            "summary": summary_text,
            "blocking_reason": blocking_reason,
            "error_code": "" if status == "PASS" else classification,
            "repeat_requested": repeat_requested,
            "repeat_completed": repeat_completed,
            "passed_runs": passed_runs,
            "failed_runs": failed_runs,
            "skipped_runs": skipped_runs,
            "summary_path": str(summary_path),
            "summary_version": str(summary.get("summary_version") or ""),
            "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "evidence": {
                "missing_env": missing_env,
                "top_messages": messages[:3],
                "top_failure_signatures": top_signatures,
            },
            "incident_candidate": incident,
            "remediation": remediation,
            "next_action": next_action,
            "provenance": build_cli_provenance(
                command="lane-triage",
                project_root=project_root,
                planning_dir=None,
                module_file=Path(__file__),
            ),
        }
    )
    return payload


def _render_markdown(payload: dict[str, Any]) -> str:
    evidence = dict(payload.get("evidence") or {})
    signatures = list(evidence.get("top_failure_signatures") or [])
    messages = [str(item) for item in list(evidence.get("top_messages") or []) if str(item).strip()]
    remediation = [str(item) for item in list(payload.get("remediation") or []) if str(item).strip()]
    incident = dict(payload.get("incident_candidate") or {})
    lines = [
        "# LANE_TRIAGE",
        "",
        f"- lane: {payload.get('lane', '')}",
        f"- status: {payload.get('status', '')}",
        f"- standing_proof_status: {payload.get('standing_proof_status', '')}",
        f"- classification: {payload.get('classification', '')}",
        f"- classification_id: {payload.get('classification_id', '')}",
        f"- root_cause_bucket: {payload.get('root_cause_bucket', '')}",
        f"- severity: {payload.get('severity', '')}",
        f"- summary_version: {payload.get('summary_version', '')}",
        f"- repeat_completed: {payload.get('repeat_completed', 0)}/{payload.get('repeat_requested', 0)}",
        "",
        "## Summary",
        "",
        f"- {payload.get('summary', '')}",
    ]
    if str(payload.get("blocking_reason") or "").strip():
        lines.append(f"- blocking_reason: {payload.get('blocking_reason', '')}")
    lines.extend(["", "## Evidence", ""])
    if signatures:
        for item in signatures:
            lines.append(f"- signature x{item.get('count', 0)}: {item.get('signature', '')}")
    elif messages:
        for message in messages:
            lines.append(f"- {message}")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Recovery Actions", ""])
    if remediation:
        lines.extend([f"- {item}" for item in remediation])
    else:
        lines.append("- (none)")
    lines.extend(["", "## Incident Candidate", ""])
    if incident.get("recommended"):
        lines.append(f"- severity: {incident.get('severity', '')}")
        lines.append(f"- title: {incident.get('title', '')}")
        lines.append(f"- suggested_command: {incident.get('suggested_command', '')}")
    else:
        lines.append("- recommended: false")
    return "\n".join(lines) + "\n"


def run_lane_triage_command(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    lane = str(getattr(args, "lane", "") or "always-on").strip() or "always-on"
    summary_path = Path(
        getattr(args, "summary", "") or (project_root / "planning" / f"lane_stability_{lane}.json")
    ).resolve()
    output_path = Path(
        getattr(args, "output", "") or summary_path.with_name(f"lane_triage_{lane}.json")
    ).resolve()
    markdown_output = Path(
        getattr(args, "markdown_output", "") or summary_path.with_name(f"lane_triage_{lane}.md")
    ).resolve()
    try:
        summary = _load_summary(summary_path)
        payload = _build_triage_payload(summary, project_root=project_root, summary_path=summary_path)
        atomic_write_json(output_path, payload)
        atomic_write_text(markdown_output, _render_markdown(payload))
        payload["artifacts"] = {
            "LANE_TRIAGE.json": str(output_path),
            "LANE_TRIAGE.md": str(markdown_output),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if bool(getattr(args, "fail_on_block", False)) and str(payload.get("status") or "").upper() != "PASS":
            return 2
        return 0
    except Exception as exc:
        payload = normalize_mutating_payload(
            build_error_payload(
                command="lane-triage",
                project_root=project_root,
                planning_dir=None,
                module_file=Path(__file__),
                error=str(exc),
                error_code="lane_triage_failed",
                remediation=["Inspect the lane stability summary artifact and rerun `kodawari lane-triage`."],
                extra={"lane": lane, "summary_path": str(summary_path)},
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


__all__ = ["run_lane_triage_command"]


"""Render a human-readable STATUS.md mirror from status JSON truth."""

from __future__ import annotations

from typing import Any


def _clean(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def render_status_markdown(payload: dict[str, Any]) -> str:
    provenance = dict(payload.get("provenance") or {})
    execution_guard = dict(payload.get("execution_guard") or {})
    execution_host_probe = dict(payload.get("execution_host_probe") or {})
    backend_truth = dict(payload.get("execution_backend_capability_truth") or {})
    worker_statuses = list(payload.get("worker_statuses") or [])
    lines = [
        f"# STATUS ({_clean(payload.get('feature'), default='(unknown)')})",
        "",
        "## Summary",
        f"- planning_artifact_mode: {_clean(payload.get('planning_artifact_mode'))}",
        f"- planning_complete: {bool(payload.get('planning_complete'))}",
        f"- execution_complete: {bool(payload.get('execution_complete'))}",
        f"- review_complete: {bool(payload.get('review_complete'))}",
        f"- verify_complete: {bool(payload.get('verify_complete'))}",
        f"- release_complete: {bool(payload.get('release_complete'))}",
        f"- execution_backend: {_clean(payload.get('execution_backend'))}",
        f"- execution_host_probe_status: {_clean(execution_host_probe.get('status'))}",
        f"- execution_host_probe_reason: {_clean(execution_host_probe.get('reason'))}",
        f"- reasoning_tier: {_clean(payload.get('reasoning_tier'))}",
        f"- effort_score: {_clean(payload.get('effort_score'))}",
        f"- effort_reasons: {', '.join(str(item) for item in list(payload.get('effort_reasons') or []))}",
        f"- parallel_merge_status: {_clean(payload.get('parallel_merge_status'))}",
        f"- worker_count: {len(worker_statuses)}",
        f"- execution_guard_action: {_clean(execution_guard.get('action'))}",
        f"- review_mode: {_clean(payload.get('review_mode'))}",
        f"- verify_scope_mode: {_clean(payload.get('verify_scope_mode'))}",
        f"- interaction_state: {_clean(payload.get('interaction_state'))}",
        f"- blocking_reason: {_clean(payload.get('blocking_reason'))}",
        f"- next_action: {_clean(payload.get('next_action'))}",
        "",
        "## Truth Sources",
        f"- planning_truth_source: {_clean(payload.get('planning_truth_source'))}",
        f"- execution_truth_source: {_clean(payload.get('execution_truth_source'))}",
        f"- review_truth_source: {_clean(payload.get('review_truth_source'))}",
        f"- verify_truth_source: {_clean(payload.get('verify_truth_source'))}",
        f"- status_truth_source: {_clean(payload.get('status_truth_source'))}",
        "",
        "## Execution Guard",
        f"- action: {_clean(execution_guard.get('action'))}",
        f"- policy: {_clean(execution_guard.get('policy'))}",
        f"- pattern: {_clean(execution_guard.get('pattern'))}",
        f"- command: {_clean(execution_guard.get('command'))}",
        "",
        "## Host Probe",
        f"- status: {_clean(execution_host_probe.get('status'))}",
        f"- surface: {_clean(execution_host_probe.get('surface'))}",
        f"- reason: {_clean(execution_host_probe.get('reason'))}",
        f"- executable: {_clean(execution_host_probe.get('executable'))}",
        f"- executable_available: {bool(execution_host_probe.get('executable_available'))}",
        *_home_probe_lines(execution_host_probe.get("home_probe")),
        "",
        *_remediation_lines(execution_host_probe.get("remediation")),
        "## Execution Backend Capability Truth",
        *_backend_truth_lines(backend_truth),
        "",
        "## Mirror Provenance",
        f"- source_json: {_clean(payload.get('status_truth_source'), default='.status_snapshot.json')}",
        f"- digest_algorithm: {_clean(payload.get('digest_algorithm'))}",
        f"- payload_digest: {_clean(payload.get('payload_digest'))}",
        f"- generated_at: {_clean(payload.get('generated_at'))}",
        f"- provenance.command: {_clean(provenance.get('command'))}",
        f"- provenance.planning_dir: {_clean(provenance.get('planning_dir'))}",
        "",
    ]
    return "\n".join(lines)


def _home_probe_lines(home_probe: Any) -> list[str]:
    if not isinstance(home_probe, dict) or not home_probe:
        return []
    lines = [f"- home_probe.status: {_clean(home_probe.get('status'))}"]
    home = _clean(home_probe.get("home"))
    if home:
        lines.append(f"- home_probe.home: {home}")
    error = _clean(home_probe.get("error"))
    if error:
        lines.append(f"- home_probe.error: {error}")
    return lines


def _remediation_lines(remediation: Any) -> list[str]:
    if not isinstance(remediation, list) or not remediation:
        return []
    cleaned = [_clean(str(item)) for item in remediation]
    cleaned = [item for item in cleaned if item]
    if not cleaned:
        return []
    lines = ["## Remediation"]
    lines.extend(f"- {item}" for item in cleaned)
    lines.append("")
    return lines


def _backend_truth_lines(payload: dict[str, Any]) -> list[str]:
    if not payload:
        return ["- none"]
    lines: list[str] = []
    for name, item in payload.items():
        if not isinstance(item, dict):
            continue
        state = _clean(item.get("runtime_state"), default="unknown")
        descriptor_value = bool(item.get("descriptor_value"))
        note = _clean(item.get("note"))
        lines.append(f"- {name}: state={state}; descriptor={descriptor_value}; note={note}")
    return lines or ["- none"]


__all__ = ["render_status_markdown"]

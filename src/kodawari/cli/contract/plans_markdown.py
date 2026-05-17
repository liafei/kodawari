"""Render human-readable Plans.md mirrors from contract-first JSON truth."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime, timezone
import hashlib
import json


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _escape_table(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _task_status(task: dict[str, Any], *, active_task_id: str) -> str:
    task_id = _clean_text(task.get("task_id"))
    if task_id and task_id == active_task_id:
        return "active"
    executability = dict(task.get("executability") or {})
    if _clean_text(executability.get("status"), default="PASS").upper() == "FAIL":
        return "blocked"
    return "planned"


def _task_content(task: dict[str, Any]) -> str:
    layer = _clean_text(task.get("layer_owner"), default="unknown")
    surface = _clean_text(task.get("surface"), default="unknown")
    files = _string_list(task.get("core_files"))
    files_display = ", ".join(files) if files else "(none)"
    return f"layer={layer}; surface={surface}; files={files_display}"


def _task_dod(task: dict[str, Any]) -> str:
    invariants = _string_list(task.get("invariants"))
    test_proof = _clean_text(task.get("test_proof"))
    parts: list[str] = []
    if invariants:
        parts.append("invariants: " + "; ".join(invariants))
    if test_proof:
        parts.append("test_proof: " + test_proof)
    return " | ".join(parts) if parts else "(none)"


def _task_row(task: dict[str, Any], *, active_task_id: str) -> str:
    task_id = _clean_text(task.get("task_id"), default="(unknown)")
    task_name = _clean_text(task.get("task_name"), default="(unnamed)")
    task_label = f"{task_id} - {task_name}"
    depends = ", ".join(_string_list(task.get("depends_on"))) or "(none)"
    return (
        f"| {_escape_table(task_label)} | "
        f"{_escape_table(_task_content(task))} | "
        f"{_escape_table(_task_dod(task))} | "
        f"{_escape_table(depends)} | "
        f"{_escape_table(_task_status(task, active_task_id=active_task_id))} |"
    )


def render_plans_markdown(
    task_graph: dict[str, Any],
    *,
    intake: dict[str, Any] | None = None,
    architecture_plan: dict[str, Any] | None = None,
    task_card_active: dict[str, Any] | None = None,
    generated_at: str | None = None,
    source_digest: str | None = None,
) -> str:
    intake_payload = dict(intake or {})
    architecture_payload = dict(architecture_plan or {})
    active_payload = dict(task_card_active or {})
    active_task_id = _clean_text(active_payload.get("task_id")).upper()

    business_outcome = _clean_text(intake_payload.get("business_outcome")) or _clean_text(task_graph.get("business_outcome"))
    source_of_truth = _string_list(intake_payload.get("source_of_truth_canonical")) or _string_list(intake_payload.get("source_of_truth"))
    planning_mode = _clean_text(task_graph.get("planning_mode"), default=_clean_text(architecture_payload.get("planning_mode"), default="existing"))
    archetype = _clean_text(architecture_payload.get("archetype"), default=_clean_text(task_graph.get("archetype"), default="unknown"))

    tasks = [dict(item) for item in list(task_graph.get("tasks") or []) if isinstance(item, dict)]
    normalized = json.dumps(task_graph, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    mirror_digest = hashlib.sha256(normalized).hexdigest()
    generated_at_value = _clean_text(generated_at, default=datetime.now(timezone.utc).isoformat())
    source_digest_value = _clean_text(source_digest, default=mirror_digest)

    lines = [
        "# Plans",
        "",
        f"- business_outcome: {business_outcome or '(missing)'}",
        f"- planning_mode: {planning_mode}",
        f"- archetype: {archetype}",
        f"- source_of_truth: {', '.join(source_of_truth) if source_of_truth else '(none)'}",
        "",
        "| Task | Content | DoD | Depends | Status |",
        "| --- | --- | --- | --- | --- |",
    ]
    if tasks:
        lines.extend(_task_row(task, active_task_id=active_task_id) for task in tasks)
    else:
        lines.append("| (none) | (none) | (none) | (none) | (none) |")

    lines.extend(
        [
            "",
            "## Mirror Provenance",
            f"- task_graph_schema: {_clean_text(task_graph.get('schema_version'))}",
            f"- generated_at: {generated_at_value}",
            f"- source_digest: {source_digest_value}",
            f"- mirror_digest: {mirror_digest}",
            f"- intake_schema: {_clean_text(intake_payload.get('schema_version'), default='(not provided)')}",
            f"- architecture_schema: {_clean_text(architecture_payload.get('schema_version'), default='(not provided)')}",
            f"- active_task_card_schema: {_clean_text(active_payload.get('schema_version'), default='(not provided)')}",
            "",
        ]
    )
    return "\n".join(lines)


def load_optional_task_card_active(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload

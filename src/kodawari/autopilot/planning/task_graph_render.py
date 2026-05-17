"""Markdown rendering helpers for task graph payloads.

Extracted from task_graph.py to keep that module within the 1000-line redline.
Imported back into task_graph.py so all existing callers are unaffected.
"""

from __future__ import annotations

import json
from typing import Any


def _clean_text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _append_coverage_section(lines: list[str], payload: dict[str, Any]) -> None:
    coverage_hints = _string_list(payload.get("coverage_hints"))
    if not coverage_hints:
        return
    lines.append("## Coverage Hints")
    lines.extend(f"- {item}" for item in coverage_hints)
    lines.append("")


def _append_boundary_section(lines: list[str], payload: dict[str, Any]) -> None:
    boundary_debt = dict(payload.get("boundary_debt") or {})
    items = [dict(item) for item in list(boundary_debt.get("items") or []) if isinstance(item, dict)]
    if not items:
        return
    lines.append("## Boundary Debt")
    lines.append(f"- status: {_clean_text(boundary_debt.get('status'), default='WARN')}")
    lines.append(f"- details: {_clean_text(boundary_debt.get('details'))}")
    for item in items:
        lines.append(
            f"- {item.get('file', '')}: "
            f"severity={_clean_text(item.get('severity'))}; "
            f"layers={', '.join(_string_list(item.get('layers')))}; "
            f"tasks={', '.join(_string_list(item.get('tasks')))}; "
            f"recommended_split={'; '.join(_string_list(item.get('recommended_split')))}"
        )
    lines.append("")


def _append_issue_section(lines: list[str], payload: dict[str, Any]) -> None:
    graph_issues = _string_list(dict(payload.get("executability") or {}).get("issues"))
    if not graph_issues:
        return
    lines.append("## Executability Issues")
    lines.extend(f"- {item}" for item in graph_issues)
    lines.append("")


def _append_task_markdown(lines: list[str], item: dict[str, Any]) -> None:
    lines.append(f"## {item.get('task_id', '')}: {item.get('task_name', '')}")
    lines.append(f"- layer_owner: {item.get('layer_owner', '')}")
    lines.append(f"- depends_on: {', '.join(_string_list(item.get('depends_on'))) or '(none)'}")
    lines.append("- core_files:")
    for path in _string_list(item.get("core_files")):
        lines.append(f"  - {path}")
    lines.append("- invariants:")
    for invariant in _string_list(item.get("invariants")):
        lines.append(f"  - {invariant}")
    for hint in _string_list(item.get("coverage_hints")):
        lines.append(f"  - coverage_hint: {hint}")
    lines.append(f"- test_proof: {_clean_text(item.get('test_proof'))}")
    task_exec = dict(item.get("executability") or {})
    lines.append(f"- executability: {_clean_text(task_exec.get('status'), default='PASS')}")
    for issue in _string_list(task_exec.get("issues")):
        lines.append(f"  - issue: {issue}")
    lines.append("")


def render_task_graph_markdown(payload: dict[str, Any]) -> str:
    tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
    lines = [
        "# Task Graph",
        "",
        f"- schema_version: {_clean_text(payload.get('schema_version'))}",
        f"- business_outcome: {_clean_text(payload.get('business_outcome'))}",
        f"- project_profile: {_clean_text(payload.get('project_profile'))}",
        f"- project_layout: {json.dumps(payload.get('project_layout') or {}, ensure_ascii=False)}",
        f"- executability: {_clean_text(dict(payload.get('executability') or {}).get('status'), default='PASS')}",
        "",
    ]
    _append_coverage_section(lines, payload)
    _append_boundary_section(lines, payload)
    _append_issue_section(lines, payload)
    for item in tasks:
        if not isinstance(item, dict):
            continue
        _append_task_markdown(lines, item)
    return "\n".join(lines).rstrip() + "\n"

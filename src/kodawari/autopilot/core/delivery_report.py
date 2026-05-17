"""Delivery report renderer backed by the authoritative run truth artifact."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _status_line(value: Any, fallback: str = "unknown") -> str:
    return _clean_text(value) or fallback


def _render_task_overview(truth: dict[str, Any], feature: str) -> str:
    return "\n".join(
        [
            "## 1. 任务概览",
            f"- feature: {_clean_text(truth.get('feature')) or feature}",
            f"- final_status: {_status_line(truth.get('final_status'))}",
            f"- run_reason: {_status_line(truth.get('run_reason'))}",
            f"- blocking_reason: {_clean_text(truth.get('blocking_reason')) or '(none)'}",
        ]
    )


def _render_runtime(truth: dict[str, Any]) -> str:
    return "\n".join(
        [
            "## 2. 运行事实",
            f"- planning_rounds: {int(truth.get('planning_rounds') or 0)}",
            f"- runtime_rounds: {int(truth.get('runtime_rounds') or 0)}",
            f"- executor_attempts: {int(truth.get('executor_attempts') or 0)}",
            f"- review_rounds: {int(truth.get('review_rounds') or 0)}",
            f"- review_must_fix_max: {int(truth.get('review_must_fix_max') or 0)}",
            f"- recovery_pressure: {int(truth.get('recovery_pressure') or 0)}",
            f"- deterministic_recovery_hits: {int(truth.get('deterministic_recovery_hits') or 0)}",
            f"- synthesizer_calls: {int(truth.get('synthesizer_calls') or 0)}",
            f"- tasks_split_count: {int(truth.get('tasks_split_count') or 0)}",
        ]
    )


def _render_quality(truth: dict[str, Any]) -> str:
    changed = [str(item) for item in list(truth.get("changed_files") or []) if _clean_text(item)]
    return "\n".join(
        [
            "## 3. 质量结果",
            f"- verify_status: {_status_line(truth.get('verify_status'))}",
            f"- gate_status: {_status_line(truth.get('gate_status'))}",
            f"- review_approved: {bool(truth.get('review_approved', False))}",
            f"- changed_files_count: {len(changed)}",
            f"- changed_files_source: {_status_line(truth.get('changed_files_source'), 'none')}",
        ]
    )


def _render_truth_sources(truth: dict[str, Any]) -> str:
    sources = dict(truth.get("truth_sources") or {})
    stale = dict(truth.get("stale_artifacts") or {})
    lines = [
        "## 4. 真值来源",
        f"- review: {_status_line(sources.get('review'), 'none')}",
        f"- verify: {_status_line(sources.get('verify'), 'none')}",
        f"- stale_review_result: {list(stale.get('review_result') or [])}",
        f"- stale_verify_report: {list(stale.get('verify_report') or [])}",
    ]
    return "\n".join(lines)


def generate_delivery_report(
    *,
    planning_dir: Path,
    feature: str,
) -> str:
    from kodawari.cli.evidence.artifact_truth import load_run_truth

    truth = load_run_truth(planning_dir)
    title = f"# 交付报告: {_clean_text(feature) or planning_dir.name}"
    if not truth:
        return "\n\n".join(
            [
                title,
                "## 1. 任务概览",
                "- final_status: unknown",
                "- run_truth: missing",
            ]
        ).strip() + "\n"
    sections = [
        title,
        _render_task_overview(truth, feature),
        _render_runtime(truth),
        _render_quality(truth),
        _render_truth_sources(truth),
    ]
    return "\n\n".join(sections).strip() + "\n"


__all__ = ["generate_delivery_report"]

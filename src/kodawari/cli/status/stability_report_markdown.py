"""Markdown rendering helpers for stability-report outputs."""

from __future__ import annotations

import json
from typing import Any

from kodawari.cli.status.stability_report_observation import distribution_summary, summarize_run_note


def _safe_pct(numerator: int | float, denominator: int | float) -> float:
    if float(denominator) <= 0:
        return 0.0
    return (float(numerator) / float(denominator)) * 100.0


def _format_pct(value: float) -> str:
    return f"{value:.1f}%"


def _stop_reason_description(reason: str) -> str:
    return {
        "PASS": "全部任务完成",
        "MAX_CYCLES": "达到最大循环次数",
        "TOKEN_BUDGET": "Token 预算耗尽",
        "STUCK": "重复错误 3+ 次",
        "NO_PROGRESS": "无文件变更 3+ 次",
        "HARD_ERROR": "不可恢复错误",
        "USER_INTERRUPT": "用户主动中断",
    }.get(reason, "-")


def _issue_description(issue_name: str) -> str:
    return {
        "429 Rate Limit": "Codex API 限流",
        "VERIFY Setup Error": "测试环境初始化失败",
        "Gate Blocked": "代码质量门禁阻断",
        "Timeout": "执行超时",
    }.get(issue_name, "-")


def _target_status_symbol(actual: float, target: Any) -> str:
    if target is None:
        return "-"
    return "✅" if actual <= float(target) else "❌"


def _target_display(target: Any) -> Any:
    return target if target is not None else "-"


def _render_data_quality_section(lines: list[str], data: dict[str, Any]) -> None:
    if not data["warnings"]:
        return
    lines.extend(["", "## 数据质量说明", "", f"- 已跳过 {len(data['warnings'])} 个损坏或不可解析的 run"])
    for warning in data["warnings"][:10]:
        lines.append(f"- {warning}")


def _render_success_and_stop_sections(lines: list[str], data: dict[str, Any], total_runs: int) -> None:
    cycle_target = data["cycle_target"]
    token_target = data["token_target"]
    cycle_ok = _target_status_symbol(data["avg_cycles"], cycle_target)
    token_ok = _target_status_symbol(data["avg_tokens"], token_target)
    lines.extend(
        [
            "",
            "## 一、总体成功率",
            "",
            "| 指标 | 数值 | 目标 | 达成 |",
            "|---|---:|---:|:---:|",
            f"| 全链路完成率 | {_format_pct(data['completion_rate'])} | >=30% | {'✅' if data['completion_rate'] >= 30.0 else '❌'} |",
            f"| 任务平均完成度 | {data['avg_task_completion_ratio']} | - | - |",
            f"| 平均 Cycles 使用 | {data['avg_cycles']:.2f} | <= {_target_display(cycle_target)} | {cycle_ok} |",
            f"| 平均 Tokens 使用 | {data['avg_tokens']:.2f} | <= {_target_display(token_target)} | {token_ok} |",
            "",
            "## 二、Stop Reason 分布",
            "",
            "| Stop Reason | 次数 | 占比 | 说明 |",
            "|---|---:|---:|---|",
        ]
    )
    for reason in sorted(data["stop_reason_counts"]):
        count = int(data["stop_reason_counts"][reason])
        lines.append(f"| {reason} | {count} | {_format_pct(_safe_pct(count, total_runs))} | {_stop_reason_description(reason)} |")


def _render_blocking_sections(lines: list[str], data: dict[str, Any], total_blocks: int, total_issue_hits: int) -> None:
    lines.extend(
        [
            "",
            "## 三、主要阻塞点",
            "",
            "### 3.1 高频错误（Top 5）",
            "",
            "| 错误签名 | 出现次数 | 典型 run_id | 阻塞阶段 |",
            "|---|---:|---|---|",
        ]
    )
    if data["top_errors"]:
        for item in data["top_errors"]:
            lines.append(f"| `{item['signature']}` | {item['count']} | {item['run_id']} | {item['stage']} |")
    else:
        lines.append("| `(none)` | 0 | - | - |")
    lines.extend(
        [
            "",
            "### 3.2 阻塞阶段分布",
            "",
            "| 阶段 | 阻塞次数 | 占比 |",
            "|---|---:|---:|",
        ]
    )
    if data["stage_block_counts"]:
        for stage in sorted(data["stage_block_counts"]):
            count = int(data["stage_block_counts"][stage])
            lines.append(f"| {stage} | {count} | {_format_pct(_safe_pct(count, total_blocks))} |")
    else:
        lines.append("| `(none)` | 0 | 0.0% |")
    lines.extend(["", "### 3.3 特定问题统计", "", "| 问题类型 | 次数 | 占比 | 说明 |", "|---|---:|---:|---|"])
    for issue, count in data["issue_counts"].items():
        lines.append(f"| {issue} | {int(count)} | {_format_pct(_safe_pct(count, total_issue_hits))} | {_issue_description(issue)} |")
    lines.extend(["", "### 3.4 细分根因桶", "", "| Root Cause Bucket | 次数 |", "|---|---:|"])
    if data.get("top_root_causes"):
        for item in list(data.get("top_root_causes") or []):
            lines.append(f"| {item.get('bucket', '')} | {int(item.get('count', 0) or 0)} |")
    else:
        lines.append("| `(none)` | 0 |")


def _render_p0_diagnostics(lines: list[str], data: dict[str, Any]) -> None:
    lines.extend(
        [
            "",
            "## P0 诊断指标（v2）",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            f"| error_category_distribution | {distribution_summary(dict(data.get('error_category_counts', {})))} |",
            f"| root_cause_bucket_distribution | {distribution_summary(dict(data.get('root_cause_bucket_counts', {})))} |",
            f"| repeated_failure_rate | {_format_pct(float(data.get('repeated_failure_rate', 0.0) or 0.0))} |",
            f"| compact_hit_rate | {_format_pct(float(data.get('compact_hit_rate', 0.0) or 0.0))} |",
            f"| learned_instinct_hit_rate | {_format_pct(float(data.get('learned_instinct_hit_rate', 0.0) or 0.0))} |",
            f"| setup_recovery_success_rate | {_format_pct(float(data.get('setup_recovery_success_rate', 0.0) or 0.0))} |",
            f"| stuck_round_limit_distribution | {distribution_summary(dict(data.get('stuck_round_limit_counts', {})))} |",
        ]
    )


def _render_task_and_suggestion_sections(lines: list[str], data: dict[str, Any]) -> None:
    lines.extend(
        [
            "",
            "## 四、任务完成度分析",
            "",
            "### 4.1 按任务类型",
            "",
            "| 任务类型 | 完成率 | 平均耗时 (cycles) |",
            "|---|---:|---:|",
        ]
    )
    for task_type, stats in data["task_type_stats"].items():
        total = int(stats["total"])
        completed = int(stats["completed"])
        avg_cycles = (float(stats["cycles"]) / float(stats["runs"])) if float(stats["runs"]) > 0 else 0.0
        lines.append(f"| {task_type} | {_format_pct(_safe_pct(completed, total))} | {avg_cycles:.2f} |")
    lines.extend(
        [
            "",
            "### 4.2 子任务统计（如果启用）",
            "",
            "| 指标 | 数值 |",
            "|---|---:|",
            f"| 平均子任务数 | {data['avg_subtasks']:.2f} |",
            f"| 子任务完成率 | {_format_pct(data['subtask_completion_rate'])} |",
            f"| 子任务失败率 | {_format_pct(data['subtask_failure_rate'])} |",
            "",
            "## 五、改进建议",
            "",
            "### 5.1 立即优化项（基于本次测试）",
            "",
        ]
    )
    for item in data["suggestions"]:
        lines.append(f"- [ ] {item}")
    lines.extend(["", "### 5.2 长期优化方向", ""])
    for item in data["long_term_suggestions"]:
        lines.append(f"- [ ] {item}")


def _append_run_detail_rows(lines: list[str], runs: list[dict[str, Any]]) -> None:
    for run in runs:
        state = run["state"]
        lines.append(
            f"| {run['run_id']} | {state.get('stop_reason', 'UNKNOWN')} | {run['tasks_completed']}/{run['tasks_total']} | {int(state.get('cycle', 0) or 0)} | {int(state.get('tokens_used', 0) or 0)} | {summarize_run_note(run)} |"
        )


def _planning_sources_summary(data: dict[str, Any]) -> str:
    values = [str(item) for item in data.get("resolved_planning_dirs", []) if str(item).strip()]
    if not values:
        return "-"
    return " | ".join(values)


def _render_appendix_section(lines: list[str], runs: list[dict[str, Any]], data: dict[str, Any]) -> None:
    merged_status = (
        " | ".join(
            sorted(
                {
                    json.dumps((run.get("compact_context") or {}).get("merged_absorption_status") or {}, ensure_ascii=False, sort_keys=True)
                    for run in runs
                }
            )
        )
        or "-"
    )
    lines.extend(
        [
            "",
            "## 六、附录：Run ID 样本",
            "",
            "### 6.1 Compact / Instincts 观测",
            "",
            "| 指标 | 分布 |",
            "|---|---|",
            f"| context_compact(runtime/mode) | {distribution_summary(dict(data.get('compact_runtime_counts', {})))} |",
            f"| instincts_status | {distribution_summary(dict(data.get('instincts_status_counts', {})))} |",
            f"| round_outcome | {distribution_summary(dict(data.get('round_outcome_counts', {})))} |",
            f"| run_outcome | {distribution_summary(dict(data.get('run_outcome_counts', {})))} |",
            f"| merged_absorption_status(sample) | {merged_status} |",
            "",
            "### 6.2 Run 明细",
            "",
            "| run_id | stop_reason | tasks_completed | cycles_used | tokens_used | 备注 |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    _append_run_detail_rows(lines, runs)


def render_markdown_report(data: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    total_runs = max(1, int(data["total_runs"]))
    total_blocks = max(1, sum(int(value) for value in data["stage_block_counts"].values()))
    total_issue_hits = max(1, sum(int(value) for value in data["issue_counts"].values()))
    lines = [
        "# kodawari 自动化稳定性报告",
        "",
        f"**生成时间**: {data['generated_at']}",
        f"**测试参数**: {data['test_params']}",
        f"**Project Root**: {data.get('project_root', '-') or '-'}",
        f"**Planning Sources**: {_planning_sources_summary(data)}",
        f"**测试轮次**: {data['total_runs']}",
    ]
    _render_data_quality_section(lines, data)
    _render_success_and_stop_sections(lines, data, total_runs)
    _render_blocking_sections(lines, data, total_blocks, total_issue_hits)
    _render_p0_diagnostics(lines, data)
    _render_task_and_suggestion_sections(lines, data)
    _render_appendix_section(lines, runs, data)
    return "\n".join(lines).strip() + "\n"


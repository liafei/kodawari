"""Diff scope guard — Phase 4 硬约束。

Executor 执行后、verify 前，检查实际修改的文件是否全在
``files_to_change ∪ new_files`` 范围内。范围外的修改直接 reject，
不进 verify 和 review。

由 ``WORKFLOW_SCOPED_EXECUTOR`` 环境变量控制（默认关闭，Phase 2 schema 稳定后再开）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

_SCOPED_EXECUTOR_ENV = "WORKFLOW_SCOPED_EXECUTOR"


def scoped_executor_enabled() -> bool:
    """返回 True 当 WORKFLOW_SCOPED_EXECUTOR 明确开启时。默认关闭。"""
    return os.environ.get(_SCOPED_EXECUTOR_ENV, "").strip().lower() in {"1", "on", "true", "yes"}


@dataclass(frozen=True)
class DiffScopeReport:
    """Diff scope 检查结果。"""
    blocked: bool
    out_of_scope_files: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "out_of_scope_files": list(self.out_of_scope_files),
        }


def _normalize_paths(paths: list[str]) -> set[str]:
    return {p.replace("\\", "/") for p in paths if str(p).strip()}


def guard_diff_scope(
    changed_files: list[str],
    files_to_change: list[str],
    new_files: list[str],
) -> DiffScopeReport:
    """检查 changed_files 是否全在 files_to_change ∪ new_files 内。

    - ``changed_files``: executor 实际修改的文件列表（来自 runtime.last_changed_files）
    - ``files_to_change``: task card 声明的可修改文件列表
    - ``new_files``: task card 声明的新建文件列表（允许由 executor 创建）
    - 如果 allowed 为空（卡片未声明），不做范围限制，返回 blocked=False
    """
    allowed = _normalize_paths(files_to_change) | _normalize_paths(new_files)
    if not allowed:
        return DiffScopeReport(blocked=False)
    changed = _normalize_paths(changed_files)
    out_of_scope = sorted(changed - allowed)
    if not out_of_scope:
        return DiffScopeReport(blocked=False)
    return DiffScopeReport(blocked=True, out_of_scope_files=tuple(out_of_scope))


__all__ = [
    "DiffScopeReport",
    "guard_diff_scope",
    "scoped_executor_enabled",
]

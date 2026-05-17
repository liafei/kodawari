"""Shared prompt-rendering helpers for execution backends.

Kept separate from execution_backend.py (capability/contract registry) and
from each backend's own module so both codex_cli and claude_code can import
the same logic without duplication.
"""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.core.action_semantics import normalize_requested_action
from kodawari.autopilot.execution.diff_scope_guard import scoped_executor_enabled


_FIX_ROUND_ACTION = "fix_round"

# P1-#10: escalating preamble. The earlier header was timid ("resolve these items"),
# which let the model treat fix-rounds as a fresh attempt — same playbook as
# attempt 1, same failure mode. The new header tells the model that the previous
# strategy did not work and authorises a more aggressive intervention.
_FIX_ROUND_HEADER = (
    "IMPORTANT: A PREVIOUS ATTEMPT WAS BLOCKED. "
    "The earlier strategy did not work — do not just repeat it with small tweaks.\n"
    "Read each must_fix item carefully and resolve it BEFORE adding any new code:"
)

# Additional guidance appended after the must_fix list when the failure is a
# complexity gate violation (detected by the structured VIOLATING_FUNCTION=...
# prefix that executor_recovery._format_complexity_must_fix injects).
_COMPLEXITY_REFACTOR_TAIL = (
    "",
    "Refactor strategy for complexity violations:",
    "  1. Read the violating function once, in full.",
    "  2. Decide on a target complexity per helper (each ≤ 5) and a target",
    "     for the main function (≤ HARD_TARGET shown above).",
    "  3. PREFER rewriting the whole function body with write_new_file over",
    "     adding helpers around the existing body. Helpers that exist alongside",
    "     the original branching do NOT reduce complexity.",
    "  4. After the rewrite, call check_complexity on the file to verify every",
    "     function meets the target. If any function is still over, iterate.",
    "  5. Only then call finish_execution.",
)


def render_fix_round_preamble(request_payload: dict[str, Any]) -> list[str]:
    """Return prompt lines to prepend when this is a fix round with open items.

    Returns an empty list when either:
    - requested_action is not 'codex_fix' (unless task_card carries a user
      redesign decision; see below), or
    - must_fix list is empty.

    must_fix items may come from gate violations OR from review feedback
    (e.g. Opus review blocking items), so the header is intentionally
    generic — it does not say "gate" or "review".

    Special case: when the task card carries a user-accepted redesign
    (recovery.source_action == "user_redesign_accepted"), the preamble is
    emitted on the first implement round too. The user has already chosen a
    specific refactor approach via `kodawari decide`; forcing the
    executor to see those instructions immediately avoids a wasted round
    of deterministic recovery rediscovering the same gate violation.
    """
    must_fix = [
        str(item).strip()
        for item in list(request_payload.get("must_fix") or [])
        if str(item).strip()
    ]
    if not must_fix:
        return []
    requested_action = normalize_requested_action(request_payload.get("requested_action"))
    is_user_redesign = _is_user_redesign_request(request_payload)
    if requested_action != _FIX_ROUND_ACTION and not is_user_redesign:
        return []
    header = _USER_REDESIGN_HEADER if is_user_redesign else _FIX_ROUND_HEADER
    lines = [header]
    for item in must_fix:
        lines.append(f"  - {item}")
    lines.append("")
    # Append complexity-refactor strategy tail when any must_fix carries the
    # structured VIOLATING_FUNCTION= prefix from executor_recovery.
    if any(str(item).startswith("VIOLATING_FUNCTION=") for item in must_fix):
        lines.extend(_COMPLEXITY_REFACTOR_TAIL)
        lines.append("")
    return lines


def _is_user_redesign_request(request_payload: dict[str, Any]) -> bool:
    task_card = request_payload.get("task_card")
    if not isinstance(task_card, dict):
        return False
    recovery = task_card.get("recovery")
    if not isinstance(recovery, dict):
        return False
    return str(recovery.get("source_action") or "").strip() == "user_redesign_accepted"


_USER_REDESIGN_HEADER = (
    "IMPORTANT: This task was escalated to the human operator and a redesign "
    "approach was chosen via `kodawari decide`. Apply EXACTLY the following "
    "refactor instructions before doing anything else:"
)


def render_scope_constraint_lines(request_payload: dict[str, Any]) -> list[str]:
    """当 WORKFLOW_SCOPED_EXECUTOR=1 时，输出 task card 的软约束提示行。

    从 request_payload 的 ``task_card`` 字段中读取 v1.1 字段：
    - ``do_not_change``：不允许修改的概念清单
    - ``target_symbols``：预期修改的函数/方法
    - ``read_only_symbols``：只读符号（不可修改）
    """
    if not scoped_executor_enabled():
        return []
    card = dict(request_payload.get("task_card") or {})
    lines: list[str] = []
    do_not_change = [str(item).strip() for item in (card.get("do_not_change") or []) if str(item).strip()]
    if do_not_change:
        lines.append("Scope constraints (do NOT change):")
        for item in do_not_change:
            lines.append(f"  - {item}")
    target_symbols = [item for item in (card.get("target_symbols") or []) if isinstance(item, dict)]
    if target_symbols:
        lines.append("Target symbols (primary edit targets):")
        for sym in target_symbols:
            label = _format_symbol(sym)
            if label:
                lines.append(f"  - {label}")
    read_only_symbols = [item for item in (card.get("read_only_symbols") or []) if isinstance(item, dict)]
    if read_only_symbols:
        lines.append("Read-only symbols (do NOT modify):")
        for sym in read_only_symbols:
            label = _format_symbol(sym)
            if label:
                lines.append(f"  - {label}")
    return lines


def render_scope_risk_warning_lines(request_payload: dict[str, Any]) -> list[str]:
    warnings = [
        str(item).strip()
        for item in list(request_payload.get("scope_risk_warnings") or [])
        if str(item).strip()
    ]
    if not warnings:
        return []
    lines = ["Reviewer / scope risk warnings:"]
    lines.extend(f"  - {item}" for item in warnings[:5])
    lines.append("Treat these as high-attention risks while implementing.")
    return lines


def _format_symbol(sym: dict[str, Any]) -> str:
    """格式化 target_symbol 条目为可读字符串。"""
    kind = str(sym.get("kind") or "").strip()
    name = str(sym.get("name") or "").strip()
    class_name = str(sym.get("class") or "").strip()
    file_ref = str(sym.get("file") or "").strip()
    if not name:
        return ""
    label = f"{class_name}.{name}" if class_name and kind == "method" else name
    return f"{label} ({kind}) in {file_ref}" if file_ref else f"{label} ({kind})"


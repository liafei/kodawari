"""Runtime token-budget helpers."""

from __future__ import annotations

from typing import Any


def build_token_budget_snapshot(tokens_used: Any, token_budget: Any) -> dict[str, Any]:
    used = _int_value(tokens_used)
    budget = _int_value(token_budget)
    remaining = max(0, budget - used) if budget > 0 else None
    usage_ratio = (float(used) / float(budget)) if budget > 0 else None
    return {
        "tokens_used": used,
        "token_budget": budget if budget > 0 else None,
        "tokens_remaining": remaining,
        "usage_ratio": usage_ratio,
        "budget_exhausted": bool(budget > 0 and used > budget),
    }


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["build_token_budget_snapshot"]

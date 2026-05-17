"""Conditional auto-approval engine — P2 feature.

evaluate_auto_approval() reads .claude/workflow/approval_rules.yaml and decides
whether a decision kind can be auto-approved or requires human intervention.

Safety-first: require_human rules are evaluated BEFORE auto_approve rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import]
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False

from kodawari.utils.glob_match import glob_match

RULES_FILE_PATH = ".claude/workflow/approval_rules.yaml"
AUDIT_LOG_FILENAME = ".auto_approval_log.jsonl"


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass
class ApprovalDecision:
    """Result of evaluate_auto_approval()."""

    action: str  # "auto_approve" | "require_human"
    log_message: str = ""
    matched_rule_index: int = -1
    message: str = ""


# ---------------------------------------------------------------------------
# Rules loading
# ---------------------------------------------------------------------------

def _load_rules(project_root: Path) -> dict[str, Any] | None:
    """Load approval_rules.yaml. Returns None if unavailable or unparseable."""
    if not _YAML_AVAILABLE:
        return None
    rules_path = project_root / RULES_FILE_PATH
    if not rules_path.exists():
        return None
    try:
        text = rules_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, Exception):
        return None
    if not isinstance(data, dict):
        return None
    rules = data.get("rules")
    if not isinstance(rules, dict):
        return None
    return rules


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

def _eval_numeric(context_value: Any, expr: str) -> bool:
    """Evaluate a numeric comparison expression like '<= 5' or '>= 3' against context_value."""
    expr = expr.strip()
    operators = ["<=", ">=", "<", ">", "=="]
    for op in operators:
        if expr.startswith(op):
            rhs_str = expr[len(op):].strip()
            try:
                rhs = float(rhs_str)
                lhs = float(context_value)
            except (TypeError, ValueError):
                return False
            if op == "<=":
                return lhs <= rhs
            if op == ">=":
                return lhs >= rhs
            if op == "<":
                return lhs < rhs
            if op == ">":
                return lhs > rhs
            if op == "==":
                return lhs == rhs
    return False


def _is_numeric_expr(value: Any) -> bool:
    """Return True if value is a string that starts with a comparison operator."""
    if not isinstance(value, str):
        return False
    for op in ("<=", ">=", "<", ">", "=="):
        if value.strip().startswith(op):
            return True
    return False


def _conditions_match(conditions: dict[str, Any], context: dict[str, Any]) -> bool:
    """Return True if ALL conditions match the given context.

    Supported condition types:
    - Simple equality: {"verify_status": "PASS"}
    - List OR: {"risk_profile": ["low", "medium"]}
    - Numeric compare: {"changed_files_count": "<= 5"}
    - any_files_match: list of glob patterns — True if any file matches any pattern
    - no_files_match: list of glob patterns — True if NO file matches any pattern
    - {} empty conditions → always match
    - Missing context field → no match (returns False, does not crash)
    """
    for key, expected in conditions.items():
        # --- special glob keys ---
        if key == "any_files_match":
            patterns = list(expected) if isinstance(expected, list) else [expected]
            changed_files: list[str] = list(context.get("changed_files") or [])
            matched = any(
                glob_match(str(f), str(p))
                for f in changed_files
                for p in patterns
            )
            if not matched:
                return False
            continue

        if key == "no_files_match":
            patterns = list(expected) if isinstance(expected, list) else [expected]
            changed_files = list(context.get("changed_files") or [])
            matched = any(
                glob_match(str(f), str(p))
                for f in changed_files
                for p in patterns
            )
            if matched:
                return False
            continue

        # --- missing context field → no match ---
        if key not in context:
            return False

        actual = context[key]

        # --- list OR ---
        if isinstance(expected, list):
            str_actual = str(actual)
            if not any(str(item) == str_actual for item in expected):
                return False
            continue

        # --- numeric comparison expression ---
        if _is_numeric_expr(expected):
            if not _eval_numeric(actual, str(expected)):
                return False
            continue

        # --- simple equality ---
        if str(actual) != str(expected):
            return False

    return True


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def _write_audit_log(
    *,
    project_root: Path,
    decision_id: str,
    action: str,
    log_message: str,
    matched_rule_index: int,
    context_snapshot: dict[str, Any],
) -> None:
    """Append one JSON line to .auto_approval_log.jsonl (auto_approve only)."""
    log_path = project_root / AUDIT_LOG_FILENAME
    entry = {
        "decision_id": decision_id,
        "action": action,
        "log_message": log_message,
        "matched_rule_index": matched_rule_index,
        "context_snapshot": context_snapshot,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except (OSError, UnicodeEncodeError):
        pass  # audit log failure must never break the main flow


# ---------------------------------------------------------------------------
# Public evaluation entry point
# ---------------------------------------------------------------------------

def evaluate_auto_approval(
    *,
    decision_kind: Any,
    context: dict[str, Any],
    project_root: Path,
) -> ApprovalDecision:
    """Evaluate whether decision_kind can be auto-approved given context.

    Returns ApprovalDecision(action="require_human") if:
    - rules file is absent or unreadable
    - decision_kind is not in the rules
    - a require_human rule matches (safety-first)
    - no auto_approve rule matches

    Returns ApprovalDecision(action="auto_approve") only when:
    - a rule file exists with an entry for the kind
    - no require_human rule matches
    - at least one auto_approve rule matches
    """
    # Normalise kind: extract .value for enums, then lowercase + strip
    normalized_kind = getattr(decision_kind, "value", str(decision_kind)).strip().lower()

    rules = _load_rules(project_root)
    if rules is None:
        return ApprovalDecision(action="require_human", message="no rules file available")

    kind_rules = rules.get(normalized_kind)
    if kind_rules is None:
        return ApprovalDecision(
            action="require_human",
            message=f"no rules defined for decision kind '{normalized_kind}'",
        )

    # Safety-first: evaluate require_human BEFORE auto_approve
    for i, rule in enumerate(list(kind_rules.get("require_human") or [])):
        cond = rule.get("conditions") if isinstance(rule, dict) else {}
        if not isinstance(cond, dict):
            cond = {}
        if _conditions_match(cond, context):
            return ApprovalDecision(
                action="require_human",
                matched_rule_index=i,
                message=str(rule.get("message") or "") if isinstance(rule, dict) else "",
            )

    # Only check auto_approve if no require_human rule fired
    for i, rule in enumerate(list(kind_rules.get("auto_approve") or [])):
        cond = rule.get("conditions") if isinstance(rule, dict) else {}
        if not isinstance(cond, dict):
            cond = {}
        if _conditions_match(cond, context):
            log_message = str(rule.get("log_message") or "") if isinstance(rule, dict) else ""
            decision_id = f"auto:{normalized_kind}"
            _write_audit_log(
                project_root=project_root,
                decision_id=decision_id,
                action="auto_approve",
                log_message=log_message,
                matched_rule_index=i,
                context_snapshot=dict(context),
            )
            return ApprovalDecision(
                action="auto_approve",
                log_message=log_message,
                matched_rule_index=i,
            )

    # Default: require human
    return ApprovalDecision(
        action="require_human",
        message="no matching auto_approve rule",
    )


__all__ = [
    "ApprovalDecision",
    "evaluate_auto_approval",
    "_conditions_match",
    "_load_rules",
]

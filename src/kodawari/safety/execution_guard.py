"""Canonical execution guard evaluation."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kodawari.safety.policy import DEFAULT_POLICY_PATH, load_guard_policy


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _match_rule(command: str, rules: list[dict[str, str]]) -> dict[str, str] | None:
    for rule in rules:
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern:
            continue
        if re.search(pattern, command, re.IGNORECASE):
            return rule
    return None


def _sanitize_command_for_matching(command: str) -> str:
    """Collapse newlines, carriage returns, and null bytes into spaces.

    Without this, an attacker can split a dangerous command across lines
    (e.g. ``rm\\n-rf /``) and bypass single-line regex patterns.
    """
    text = str(command or "").strip()
    for ch in ("\r\n", "\r", "\n", "\x00"):
        text = text.replace(ch, " ")
    # Collapse multiple spaces so patterns like ``rm\\s+-rf`` still match.
    return " ".join(text.split())


# Matches any shell operator that can chain, pipe, or substitute commands.
# Every one of these can make a safe-looking prefix dangerous:
#   ;            sequential     pytest; git push --force
#   &&           conditional    pytest && rm -rf /
#   ||           fallback       false_cmd || sudo rm /
#   |            pipe           git log | sh
#   &            background     malware &
#   `…`          backtick sub   rm `cat /etc/passwd`
#   $(…)         $() sub        rm $(find / -name "*.py")
# Deny wins over this check; a compound command that also matches deny is denied.
_COMPOUND_SHELL_RE = re.compile(
    r"&&"                       # AND-conditional
    r"|\|\|"                    # OR-conditional
    r"|(?<!\|)\|(?!\|)"         # single pipe (not part of ||)
    r"|;"                       # sequential
    r"|(?<![&])&(?![&])"        # background job (not part of &&)
    r"|`"                       # backtick substitution
    r"|\$\("                    # $() substitution
)


def _evaluate_allow_tier(
    normalized_command: str, policy: dict[str, Any]
) -> tuple[dict[str, str] | None, str, str]:
    allow_matched = _match_rule(normalized_command, list(policy.get("allow") or []))
    if allow_matched is not None:
        return allow_matched, "allow", str(allow_matched.get("reason") or "command explicitly allowed by policy")
    ask_matched = _match_rule(normalized_command, list(policy.get("ask") or []))
    if ask_matched is not None:
        return ask_matched, "ask", str(ask_matched.get("reason") or "command requires confirmation")
    if not normalized_command:
        return None, "deny", "no executable command string to inspect for this backend"
    return None, "deny", "command is not in execution allowlist"


def evaluate_execution_guard(
    *,
    backend: str,
    command: str,
    policy_path: Path | None = None,
) -> dict[str, Any]:
    normalized_backend = str(backend or "").strip().lower()
    normalized_command = _sanitize_command_for_matching(command)
    policy = load_guard_policy(policy_path)
    # Tier precedence: deny > allow (explicit safe) > ask > default-deny.
    # Checking deny first ensures the denylist cannot be bypassed by an allow rule.
    deny_matched = _match_rule(normalized_command, list(policy.get("deny") or []))
    matched: dict[str, str] | None = None
    action = "deny"
    reason = "command is not in execution allowlist"
    if deny_matched is not None:
        matched = deny_matched
        action = "deny"
        reason = str(matched.get("reason") or "command denied by execution guard")
    elif _COMPOUND_SHELL_RE.search(normalized_command):
        # Any compound shell operator (;, &&, ||, |, &, `, $()) means the
        # safe-looking prefix could precede a dangerous continuation.
        # The allow tier CANNOT verify safety for the whole command — go straight
        # to ask so a human confirms before execution.
        action = "ask"
        reason = "compound shell command requires confirmation (contains shell operator)"
    else:
        matched, action, reason = _evaluate_allow_tier(normalized_command, policy)
    return {
        "schema_version": "execution.guard.decision.v1",
        "checked_at": _utc_now_iso(),
        "backend": normalized_backend,
        "command": normalized_command,
        "action": action,
        "reason": reason,
        "matched_pattern": str((matched or {}).get("pattern") or ""),
        "policy_name": str(policy.get("policy_name") or "default"),
        "policy_path": str(Path(policy_path or DEFAULT_POLICY_PATH).resolve()),
    }


def execution_guard_blocks(decision: dict[str, Any]) -> bool:
    return str(decision.get("action") or "").strip().lower() in {"deny", "ask"}


__all__ = ["evaluate_execution_guard", "execution_guard_blocks"]

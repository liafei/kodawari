"""Command-level execution safety guard for backend dispatch."""

from __future__ import annotations

from dataclasses import dataclass

from kodawari.safety.execution_guard import evaluate_execution_guard

DENY = "deny"
ASK = "ask"


@dataclass(frozen=True)
class GuardDecision:
    action: str
    message: str
    policy: str
    pattern: str

    @property
    def error_code(self) -> str:
        if self.action == DENY:
            return "EXECUTION_GUARD_DENY"
        return "EXECUTION_GUARD_CONFIRM_REQUIRED"


def evaluate_execution_command(command: str) -> GuardDecision | None:
    normalized = str(command or "").strip()
    if not normalized:
        return None
    payload = evaluate_execution_guard(backend="external_cli", command=normalized)
    action = str(payload.get("action") or "").strip().lower()
    if action not in {DENY, ASK}:
        return None
    policy_name = str(payload.get("policy_name") or "default")
    matched = str(payload.get("matched_pattern") or "")
    return GuardDecision(
        action=action,
        message=str(payload.get("reason") or "execution guard blocked command"),
        policy=f"{policy_name}:{action}",
        pattern=matched,
    )


__all__ = [
    "ASK",
    "DENY",
    "GuardDecision",
    "evaluate_execution_command",
]

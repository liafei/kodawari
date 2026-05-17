"""Guard policy loading and normalization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policies" / "default.yaml"
POLICY_SCHEMA_VERSION = "execution.guard.policy.v1"


def _normalize_rule(item: Any, *, default_action: str) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    pattern = str(item.get("pattern") or "").strip()
    if not pattern:
        return None
    return {
        "pattern": pattern,
        "action": str(item.get("action") or default_action).strip().lower() or default_action,
        "reason": str(item.get("reason") or f"{default_action} rule matched").strip(),
    }


def load_guard_policy(path: Path | None = None) -> dict[str, Any]:
    resolved = Path(path or DEFAULT_POLICY_PATH).resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"guard policy payload must be an object: {resolved}")
    return {
        "schema_version": str(payload.get("schema_version") or POLICY_SCHEMA_VERSION),
        "policy_name": str(payload.get("policy_name") or "default"),
        "deny": [
            rule
            for rule in (_normalize_rule(item, default_action="deny") for item in list(payload.get("deny") or []))
            if rule is not None
        ],
        "ask": [
            rule
            for rule in (_normalize_rule(item, default_action="ask") for item in list(payload.get("ask") or []))
            if rule is not None
        ],
        "allow": [
            rule
            for rule in (_normalize_rule(item, default_action="allow") for item in list(payload.get("allow") or []))
            if rule is not None
        ],
    }


__all__ = ["DEFAULT_POLICY_PATH", "POLICY_SCHEMA_VERSION", "load_guard_policy"]

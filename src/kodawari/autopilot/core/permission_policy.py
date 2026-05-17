"""Declarative three-tier permission policy for tool invocations.

Phase D of the harness plan. Complements the existing command-level
`execution_guard` (which covers Bash command strings only) with a
**tool + path** policy that answers:

- Read / Grep / Glob / Bash / Edit / Write are being invoked on paths X;
  should this be **auto-approved**, **prompted** to a human, or **blocked**?

The policy is declarative JSON (YAML shape in the .yaml file), loaded from
`src/kodawari/safety/policies/permission.default.yaml`. Each entry has
a `tool` matcher and a `path_glob` matcher; a decision is the first matching
rule in priority order: block > prompt > allow.

Why this exists:
- `execution_guard` only inspects full Bash command strings. It cannot
  distinguish "Edit src/api.py" (probably fine) from "Edit .env" (must block).
- Without a tool-level tier the autopilot has to trust the subprocess prompt
  to respect scope — prompt injection can override that.
- Phase E (injection classifier) depends on this tier for escalation.

The policy is wired into two runtime enforcement points:

1. **Planning validation** (`planning_agent._validate_plan`): rejects tasks
   whose `files_to_change` include paths blocked by the Write permission tier.
2. **Isolation sync-back** (`execution_isolation.sync_isolated_workspace_to_project_root`):
   skips files whose paths are blocked by the Write permission tier.

Phase E (injection classifier) will consume this policy plus the classifier
for full tool-invocation gating inside subprocess sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import fnmatch
import json
from pathlib import Path
from typing import Any


PERMISSION_POLICY_SCHEMA_VERSION = "autopilot.permission_policy.v1"


class PermissionTier(str, Enum):
    """Three-tier verdict for a tool invocation."""

    ALLOW = "allow"     # auto-approved, run without human interaction
    PROMPT = "prompt"   # human must approve via .decision_request.json
    BLOCK = "block"     # reject outright, do not prompt


@dataclass(frozen=True)
class PermissionDecision:
    tier: PermissionTier
    reason: str
    rule_tool: str
    rule_path_glob: str
    policy_name: str


@dataclass(frozen=True)
class PermissionRule:
    tier: PermissionTier
    tool: str           # exact tool name or "*" wildcard
    path_glob: str      # fnmatch glob; "" means "any path / no path arg"
    reason: str


_DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2]
    / "safety"
    / "policies"
    / "permission.default.yaml"
)


def _coerce_rule(item: Any, *, default_tier: PermissionTier) -> PermissionRule | None:
    if not isinstance(item, dict):
        return None
    tool = str(item.get("tool") or "*").strip() or "*"
    path_glob = str(item.get("path_glob") or "").strip()
    reason = str(item.get("reason") or f"{default_tier.value} rule matched").strip()
    tier_raw = str(item.get("tier") or default_tier.value).strip().lower()
    try:
        tier = PermissionTier(tier_raw)
    except ValueError:
        tier = default_tier
    return PermissionRule(tier=tier, tool=tool, path_glob=path_glob, reason=reason)


def load_permission_policy(path: Path | None = None) -> dict[str, Any]:
    """Load the permission policy JSON/YAML file (JSON format; .yaml ext kept
    for consistency with the existing execution guard policy)."""
    resolved = Path(path or _DEFAULT_POLICY_PATH).resolve()
    if not resolved.exists():
        return {
            "schema_version": PERMISSION_POLICY_SCHEMA_VERSION,
            "policy_name": "empty",
            "rules": [],
        }
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"permission policy must be an object: {resolved}")
    rules: list[PermissionRule] = []
    # Order: block > prompt > allow (so block rules always take precedence).
    for tier_key, default_tier in (
        ("block", PermissionTier.BLOCK),
        ("prompt", PermissionTier.PROMPT),
        ("allow", PermissionTier.ALLOW),
    ):
        for item in list(payload.get(tier_key) or []):
            rule = _coerce_rule(item, default_tier=default_tier)
            if rule is not None:
                rules.append(rule)
    return {
        "schema_version": str(payload.get("schema_version") or PERMISSION_POLICY_SCHEMA_VERSION),
        "policy_name": str(payload.get("policy_name") or "default"),
        "rules": rules,
    }


def _path_matches(rule_glob: str, target: str) -> bool:
    """Glob matcher with recursive-** semantics.

    Treats `**` as "match anything including `/`" and `*` as "anything except `/`".
    If the glob starts with `**/`, the `/` is optional (so `**/.env*` matches
    both `backend/.env` and top-level `.env.local`).
    """
    normalized_glob = str(rule_glob or "").strip().replace("\\", "/").lower()
    normalized = str(target or "").replace("\\", "/").lower()
    if not normalized_glob:
        return not normalized  # empty glob matches only "no path arg"
    if normalized_glob == "*" or normalized_glob == "**":
        return True
    # Primary: fnmatch (works for flat `*.py`-style patterns)
    if fnmatch.fnmatchcase(normalized, normalized_glob):
        return True
    # Recursive-** fallback: try stripping optional leading `**/`
    if normalized_glob.startswith("**/"):
        tail = normalized_glob[3:]
        # Try match at root (e.g. `**/.env*` against `.env.local`)
        if fnmatch.fnmatchcase(normalized, tail):
            return True
        # Try match at any path segment
        parts = normalized.split("/")
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            if fnmatch.fnmatchcase(suffix, tail):
                return True
    return False


def _tool_matches(rule_tool: str, target: str) -> bool:
    if not rule_tool or rule_tool == "*":
        return True
    return rule_tool.lower() == str(target or "").strip().lower()


def evaluate_permission(
    *,
    tool: str,
    path: str = "",
    policy: dict[str, Any] | None = None,
) -> PermissionDecision:
    """Deterministic matcher. Returns the first matching rule's tier.

    Order: block > prompt > allow (enforced at load time). If no rule
    matches, defaults to PROMPT — the safe default is to ask the human.
    """
    loaded = policy if policy is not None else load_permission_policy()
    rules: list[PermissionRule] = list(loaded.get("rules") or [])
    for rule in rules:
        if _tool_matches(rule.tool, tool) and _path_matches(rule.path_glob, path):
            return PermissionDecision(
                tier=rule.tier,
                reason=rule.reason,
                rule_tool=rule.tool,
                rule_path_glob=rule.path_glob,
                policy_name=str(loaded.get("policy_name") or "default"),
            )
    return PermissionDecision(
        tier=PermissionTier.PROMPT,
        reason="no matching rule; defaulting to human confirmation",
        rule_tool="",
        rule_path_glob="",
        policy_name=str(loaded.get("policy_name") or "default"),
    )


def find_blocked_writes(
    paths: list[str],
    *,
    policy: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Return a list of ``{path, tool, reason}`` dicts for every path that the
    permission policy blocks under Write or Edit.

    Phase E post-execution gate (non-invasive): the runtime loop runs this on
    ``changed_files`` AFTER the executor finishes so the non-isolation path
    gets the same BLOCK-tier coverage that isolation sync-back already has.
    Empty list means every path is allowed or requires only a prompt.
    """
    blocked: list[dict[str, str]] = []
    for raw in paths:
        path = str(raw or "").strip()
        if not path:
            continue
        for tool in ("Write", "Edit"):
            decision = evaluate_permission(tool=tool, path=path, policy=policy)
            if decision.tier is PermissionTier.BLOCK:
                blocked.append({
                    "path": path,
                    "tool": tool,
                    "reason": decision.reason,
                    "rule_path_glob": decision.rule_path_glob,
                })
                break  # one tool match is enough per path
    return blocked


def is_path_blocked_for_write(path: str, *, policy: dict[str, Any] | None = None) -> bool:
    """Return True if the Write or Edit permission tier blocks this path.

    Used by planning validation and isolation sync-back to reject secret paths
    at runtime — not just in tests. Thin wrapper over ``find_blocked_writes``
    so both paths share a single authoritative implementation.
    """
    return bool(find_blocked_writes([path], policy=policy))


__all__ = [
    "PERMISSION_POLICY_SCHEMA_VERSION",
    "PermissionDecision",
    "PermissionRule",
    "PermissionTier",
    "evaluate_permission",
    "find_blocked_writes",
    "is_path_blocked_for_write",
    "load_permission_policy",
]

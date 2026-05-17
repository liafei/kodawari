"""Load and resolve gate_policy.yaml into actionable rules."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from kodawari.gate.models import GateThresholds
from kodawari.gate.profiles import DEFAULT_THRESHOLDS
from kodawari.utils.glob_match import glob_match

POLICY_SCHEMA_VERSION = "gate.policy.v1"
POLICY_FILENAME = "gate_policy.yaml"
WORKFLOW_CONFIG_DIR = ".claude/workflow"
logger = logging.getLogger(__name__)

# Canonical policy defaults come from the shared code-redline-backed
# blocking thresholds. Repo-local policy files may override these per scope.
_DEFAULT_THRESHOLDS: dict[str, Any] = DEFAULT_THRESHOLDS.to_dict()


@dataclass(frozen=True)
class ScopeRule:
    scope: str
    thresholds: dict[str, int]
    skip_checkers: list[str]
    severity: str | None = None


@dataclass
class GatePolicy:
    schema_version: str
    defaults: dict[str, Any]
    rules: list[ScopeRule]

    def resolve_for_file(self, rel_path: str) -> ScopeRule | None:
        """Return the first matching rule for rel_path, or None."""
        normalized = rel_path.replace("\\", "/")
        for rule in self.rules:
            if glob_match(normalized, rule.scope):
                return rule
        return None

    def effective_thresholds(self, rel_path: str) -> GateThresholds:
        """Merge _DEFAULT_THRESHOLDS <- defaults <- matching rule thresholds."""
        base: dict[str, Any] = dict(_DEFAULT_THRESHOLDS)
        applied_int_keys: set[str] = set()
        # Apply defaults: only int values that are not bool
        default_ints = {
            k: v for k, v in self.defaults.items()
            if isinstance(v, int) and not isinstance(v, bool)
        }
        base.update(default_ints)
        applied_int_keys.update(default_ints)
        if isinstance(self.defaults.get("severity"), str) and str(self.defaults.get("severity")).strip():
            base["severity"] = str(self.defaults.get("severity")).strip()
        # Apply matching rule overrides
        rule = self.resolve_for_file(rel_path)
        if rule:
            base.update(rule.thresholds)
            applied_int_keys.update(rule.thresholds)
            if rule.severity:
                base["severity"] = rule.severity

        # When policy specifies tier-only complexity fields, infer executable
        # complexity_max for checker compatibility.
        if "complexity_max" not in applied_int_keys:
            if "complexity_block" in applied_int_keys:
                base["complexity_max"] = int(base["complexity_block"])
            elif "complexity_warn" in applied_int_keys:
                base["complexity_max"] = int(base["complexity_warn"])

        return GateThresholds(
            file_max_lines=int(base["file_max_lines"]),
            function_max_lines=int(base["function_max_lines"]),
            nesting_max=int(base["nesting_max"]),
            complexity_max=int(base["complexity_max"]),
            complexity_warn=int(base.get("complexity_warn", base["complexity_max"])),
            complexity_block=int(base.get("complexity_block", base["complexity_max"])),
            file_complexity_warn_lines=int(
                base.get("file_complexity_warn_lines", base["file_max_lines"])
            ),
            file_complexity_warn_sum=int(base.get("file_complexity_warn_sum", 0)),
            file_complexity_block_lines=int(
                base.get("file_complexity_block_lines", base["file_max_lines"])
            ),
            file_complexity_block_sum=int(base.get("file_complexity_block_sum", 0)),
            max_violations=int(base["max_violations"]),
            severity=str(base.get("severity", "ERROR")),
        )

    def skip_checkers_for_file(self, rel_path: str) -> list[str]:
        """Return skip list for the first matching rule, or empty list."""
        rule = self.resolve_for_file(rel_path)
        return list(rule.skip_checkers) if rule else []


def load_gate_policy(project_root: Path) -> GatePolicy | None:
    """Load gate_policy.yaml from project_root. Returns None if missing or malformed."""
    path = project_root / WORKFLOW_CONFIG_DIR / POLICY_FILENAME
    if not path.is_file():
        return None
    try:
        import yaml  # optional dependency
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("gate policy: failed to load %s; falling back to defaults (%s)", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("gate policy: %s is not a mapping; falling back to defaults", path)
        return None
    return _parse_policy(raw)


def _parse_policy(raw: dict[str, Any]) -> GatePolicy:
    """Parse a raw dict into a GatePolicy, tolerating missing/extra fields."""
    defaults = dict(raw.get("defaults") or {})
    rules: list[ScopeRule] = []
    for item in list(raw.get("rules") or []):
        if not isinstance(item, dict) or not item.get("scope"):
            continue
        checks = dict(item.get("checks") or {})
        rules.append(ScopeRule(
            scope=str(item["scope"]),
            thresholds={
                k: v for k, v in checks.items()
                if isinstance(v, int) and not isinstance(v, bool)
            },
            skip_checkers=[str(s) for s in list(checks.get("skip") or [])],
            severity=(
                str(checks.get("severity")).strip()
                if isinstance(checks.get("severity"), str) and str(checks.get("severity")).strip()
                else None
            ),
        ))
    return GatePolicy(
        schema_version=str(raw.get("schema_version", POLICY_SCHEMA_VERSION)),
        defaults=defaults,
        rules=rules,
    )

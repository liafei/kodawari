"""Gate engine models and result schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class GateMode(str, Enum):
    ADVISORY = "advisory"
    BLOCKING = "blocking"


class ItemStatus(str, Enum):
    PASS = "PASS"
    PARTIAL = "PARTIAL"
    FAIL = "FAIL"


class GateStatus(str, Enum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GateThresholds:
    file_max_lines: int
    function_max_lines: int
    nesting_max: int
    complexity_max: int
    max_violations: int
    severity: str = "ERROR"
    # Tiered redline extensions:
    # - complexity_warn / complexity_block express warning vs blocking bands
    # - file_complexity_* express composite file-level risk heuristics
    complexity_warn: int | None = None
    complexity_block: int | None = None
    file_complexity_warn_lines: int | None = None
    file_complexity_warn_sum: int | None = None
    file_complexity_block_lines: int | None = None
    file_complexity_block_sum: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "file_max_lines": self.file_max_lines,
            "function_max_lines": self.function_max_lines,
            "nesting_max": self.nesting_max,
            "complexity_max": self.complexity_max,
            "max_violations": self.max_violations,
            "severity": self.severity,
        }
        for key in (
            "complexity_warn",
            "complexity_block",
            "file_complexity_warn_lines",
            "file_complexity_warn_sum",
            "file_complexity_block_lines",
            "file_complexity_block_sum",
        ):
            value = getattr(self, key)
            if value is not None:
                payload[key] = int(value)
        return payload


@dataclass(frozen=True)
class GateProfile:
    name: str
    mode: GateMode
    thresholds: GateThresholds
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode.value,
            "description": self.description,
            "thresholds": self.thresholds.to_dict(),
        }


@dataclass
class Violation:
    checker: str
    path: str
    message: str
    metric: str
    actual: int
    limit: int
    severity: str
    line: int | None = None
    symbol: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checker": self.checker,
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "metric": self.metric,
            "actual": self.actual,
            "limit": self.limit,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass
class CheckerResult:
    checker: str
    status: ItemStatus
    checked_files: int
    violations: list[Violation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checker": self.checker,
            "status": self.status.value,
            "checked_files": self.checked_files,
            "violation_count": len(self.violations),
            "violations": [item.to_dict() for item in self.violations],
        }


@dataclass
class GateEvaluation:
    profile: GateProfile
    total_status: GateStatus
    checker_results: list[CheckerResult]
    scanned_files: int
    total_violations: int
    blocking_violations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "total_status": self.total_status.value,
            "scanned_files": self.scanned_files,
            "total_violations": self.total_violations,
            "max_violations": self.profile.thresholds.max_violations,
            "blocking_violations": self.blocking_violations,
            "items": [item.to_dict() for item in self.checker_results],
        }


@dataclass
class ComplianceEvidence:
    file: str
    rule: str
    hit: str
    confidence: float
    line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "file": str(self.file),
            "rule": str(self.rule),
            "hit": str(self.hit),
            "confidence": float(self.confidence),
        }
        if self.line is not None:
            payload["line"] = int(self.line)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass
class ComplianceCheck:
    check_name: str
    status: str
    details: str = ""
    evidence: list[ComplianceEvidence] = field(default_factory=list)
    evidence_sufficient: bool = True
    blocking_eligible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": str(self.check_name),
            "status": str(self.status),
            "details": str(self.details),
            "evidence": [item.to_dict() for item in self.evidence],
            "evidence_count": len(self.evidence),
            "evidence_sufficient": bool(self.evidence_sufficient),
            "blocking_eligible": bool(self.blocking_eligible),
        }


@dataclass
class ComplianceReport:
    status: str
    checks: list[ComplianceCheck] = field(default_factory=list)
    generated_at: str = field(default_factory=_utc_now_iso)
    mode: str = "contract_first_mvp"
    schema_version: str = "contract_first.compliance_report.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": str(self.status).upper(),
            "generated_at": self.generated_at,
            "mode": str(self.mode),
            "checks": [item.to_dict() for item in self.checks],
        }


def derive_item_status(violation_count: int, max_violations: int) -> ItemStatus:
    if violation_count <= 0:
        return ItemStatus.PASS
    if max_violations >= 0 and violation_count > max_violations:
        return ItemStatus.FAIL
    return ItemStatus.PARTIAL

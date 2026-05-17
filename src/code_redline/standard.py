"""The canonical redline numbers and tier evaluator.

Semantics (frozen contract)
---------------------------
Function-level:
  - complexity  in (complexity_warn, complexity_block] → WARN
  - complexity  >  complexity_block                    → BLOCK
  - nesting     >  nesting_max                         → BLOCK

File-level (composite: line_count AND complexity_sum):
  - lines > file_complexity_warn_lines  AND  sum > file_complexity_warn_sum   → WARN
  - lines > file_complexity_block_lines AND  sum > file_complexity_block_sum  → BLOCK
  - lines > file_complexity_block_lines BUT  sum <= file_complexity_warn_sum  → DASHBOARD
    (large-but-declarative files: surface as trend metric, do not block)

Checker reporting cap:
  - each checker records at most max_violations entries before truncating.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum


class Tier(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"
    DASHBOARD = "DASHBOARD"


@dataclass(frozen=True)
class RedlineStandard:
    """All redline thresholds. Fields are the *only* authoritative source."""

    nesting_max: int = 4
    complexity_warn: int = 7
    complexity_block: int = 10
    file_complexity_warn_lines: int = 1000
    file_complexity_warn_sum: int = 20
    file_complexity_block_lines: int = 1500
    file_complexity_block_sum: int = 30
    max_violations: int = 50

    def __post_init__(self) -> None:
        # Sanity: tier boundaries must not invert.
        if self.complexity_warn >= self.complexity_block:
            raise ValueError(
                f"complexity_warn ({self.complexity_warn}) must be < "
                f"complexity_block ({self.complexity_block})"
            )
        if self.file_complexity_warn_lines > self.file_complexity_block_lines:
            raise ValueError(
                "file_complexity_warn_lines must be <= file_complexity_block_lines"
            )
        if self.file_complexity_warn_sum > self.file_complexity_block_sum:
            raise ValueError(
                "file_complexity_warn_sum must be <= file_complexity_block_sum"
            )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


REDLINE = RedlineStandard()


def evaluate_function(*, complexity: int, std: RedlineStandard = REDLINE) -> Tier:
    """Return the tier for a function given its cyclomatic complexity."""
    if complexity > std.complexity_block:
        return Tier.BLOCK
    if complexity > std.complexity_warn:
        return Tier.WARN
    return Tier.PASS


def evaluate_nesting(*, depth: int, std: RedlineStandard = REDLINE) -> Tier:
    """Return the tier for observed nesting depth. Nesting is binary:
    any breach of ``nesting_max`` is a BLOCK — there is no WARN band."""
    if depth > std.nesting_max:
        return Tier.BLOCK
    return Tier.PASS


def evaluate_file(
    *,
    line_count: int,
    complexity_sum: int,
    std: RedlineStandard = REDLINE,
) -> Tier:
    """Return the tier for a file given its line count AND the sum of
    its functions' cyclomatic complexity.

    Line count alone is not a trigger. A 2000-line schema file with
    complexity_sum=5 is DASHBOARD, not BLOCK.
    """
    # BLOCK requires BOTH large AND complex.
    if line_count > std.file_complexity_block_lines and complexity_sum > std.file_complexity_block_sum:
        return Tier.BLOCK
    # WARN requires BOTH moderately large AND moderately complex.
    if line_count > std.file_complexity_warn_lines and complexity_sum > std.file_complexity_warn_sum:
        return Tier.WARN
    # Large-but-declarative: lines exceed block threshold but complexity is low.
    if line_count > std.file_complexity_block_lines and complexity_sum <= std.file_complexity_warn_sum:
        return Tier.DASHBOARD
    return Tier.PASS

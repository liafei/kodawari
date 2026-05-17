"""Verify analysis — classify pytest failures into actionable tiers.

This subpackage is the home of the 4.23 iteration's Phase 5 analyzer.
Tier A classification (literal-assert stale-assertion candidates) ships as
the 8-vulnerability closeout; broader classification and auto-mutation
wiring lands in subsequent phases.
"""

from kodawari.autopilot.verify.failure_analyzer import (
    FailureClassification,
    PytestFailure,
    classify_failure,
    parse_pytest_failures,
)

__all__ = [
    "FailureClassification",
    "PytestFailure",
    "classify_failure",
    "parse_pytest_failures",
]

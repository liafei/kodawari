"""code-redline — unified code quality redline standard.

Single source of truth. Every project that checks code quality thresholds
(gate engines, complexity checkers, CI reports, architecture docs) imports
from this package instead of hardcoding numbers.

Usage
-----
    from code_redline import REDLINE, evaluate_function, evaluate_file

    if REDLINE.nesting_max < observed_nesting:
        ...

    tier = evaluate_function(complexity=observed_complexity)   # "PASS" / "WARN" / "BLOCK"
    tier = evaluate_file(line_count=lc, complexity_sum=cs)     # "PASS" / "WARN" / "BLOCK" / "DASHBOARD"

Guard invariant
---------------
These constants are the ONLY authoritative source. Tests in consumer
projects should grep for hardcoded copies and fail if any are found
outside this package. See ``code_redline.verify.find_hardcoded_copies``.
"""
from __future__ import annotations

from code_redline.standard import (
    REDLINE,
    RedlineStandard,
    Tier,
    evaluate_file,
    evaluate_function,
    evaluate_nesting,
)
from code_redline.render import render_markdown_table, render_yaml_block

__version__ = "1.0.0"

__all__ = [
    "REDLINE",
    "RedlineStandard",
    "Tier",
    "evaluate_file",
    "evaluate_function",
    "evaluate_nesting",
    "render_markdown_table",
    "render_yaml_block",
    "__version__",
]

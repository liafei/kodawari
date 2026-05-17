"""Render helpers so docs can embed the canonical redline table
without manually re-typing numbers."""
from __future__ import annotations

from code_redline.standard import REDLINE, RedlineStandard


def render_markdown_table(std: RedlineStandard = REDLINE) -> str:
    """Emit a markdown table describing the active redline standard.

    Docs can either call this at build time to paste into a file, or
    embed the result between ``<!-- REDLINE:BEGIN -->`` / ``<!-- REDLINE:END -->``
    markers that a CI check keeps in sync.
    """
    return "\n".join(
        [
            "| Threshold | Value | Tier semantic |",
            "|---|---:|---|",
            f"| `nesting_max` | {std.nesting_max} | nesting > {std.nesting_max} → BLOCK |",
            f"| `complexity_warn` | {std.complexity_warn} | complexity in ({std.complexity_warn}, {std.complexity_block}] → WARN |",
            f"| `complexity_block` | {std.complexity_block} | complexity > {std.complexity_block} → BLOCK |",
            f"| `file_complexity_warn_lines` | {std.file_complexity_warn_lines} | file > {std.file_complexity_warn_lines} lines AND sum > {std.file_complexity_warn_sum} → WARN |",
            f"| `file_complexity_warn_sum` | {std.file_complexity_warn_sum} | (see above) |",
            f"| `file_complexity_block_lines` | {std.file_complexity_block_lines} | file > {std.file_complexity_block_lines} lines AND sum > {std.file_complexity_block_sum} → BLOCK |",
            f"| `file_complexity_block_sum` | {std.file_complexity_block_sum} | (see above) |",
            f"| `max_violations` | {std.max_violations} | each checker truncates after {std.max_violations} entries |",
            "",
            "Large-but-declarative files (lines > "
            f"{std.file_complexity_block_lines} but sum ≤ {std.file_complexity_warn_sum}) → "
            "DASHBOARD only; line count alone is never a BLOCK trigger.",
        ]
    )


def render_yaml_block(std: RedlineStandard = REDLINE) -> str:
    """Emit a gate_policy.yaml-style block. Consumers can drop this
    verbatim into their policy file or import REDLINE programmatically."""
    return "\n".join(
        [
            "redline:",
            f"  nesting_max: {std.nesting_max}",
            f"  complexity_warn: {std.complexity_warn}",
            f"  complexity_block: {std.complexity_block}",
            f"  file_complexity_warn_lines: {std.file_complexity_warn_lines}",
            f"  file_complexity_warn_sum: {std.file_complexity_warn_sum}",
            f"  file_complexity_block_lines: {std.file_complexity_block_lines}",
            f"  file_complexity_block_sum: {std.file_complexity_block_sum}",
            f"  max_violations: {std.max_violations}",
        ]
    )

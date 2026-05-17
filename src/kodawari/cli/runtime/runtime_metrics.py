"""Shared runtime metric helpers for telemetry and stability reports."""

from __future__ import annotations

from typing import Any


def count_peer_review_rounds(records: list[dict[str, Any]]) -> int:
    """Count review rounds using a shared semantic across CLI reports."""

    total = 0
    for row in records:
        if not isinstance(row, dict):
            continue
        stage = str(row.get("stage") or "").strip().upper()
        if stage in {"PEER_REVIEW", "OPUS_REVIEW"}:
            total += 1
            continue
        details = dict(row.get("details") or {})
        reviewer = str(details.get("reviewer") or "").strip().lower()
        if reviewer == "opus":  # CollaborationRole.OPUS — role identifier, not vendor name
            total += 1
    return total

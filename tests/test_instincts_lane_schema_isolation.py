"""Lane observation schema isolation in instincts ingestion.

Lane observation events older than lane.observation.v2 are kept on disk for
audit but must never feed promotion. Unstamped events still ingest for
backward compat with the period before the schema version field landed.
"""

from __future__ import annotations

from pathlib import Path

from kodawari.instincts.engine import (
    SUPPORTED_LANE_SCHEMA_PREFIX,
    ingest_lane_event,
)


def _base_event(schema_version: str | None = None) -> dict:
    metadata: dict = {"feature": "feat-x"}
    if schema_version is not None:
        metadata["schema_version"] = schema_version
    return {
        "category": "lane",
        "phase": "COMPLEXITY_DETECTION",
        "action": "underclassified",
        "message": "predicted=lite actual=heavy diff_loc=10 files=1 blockers=0",
        "metadata": metadata,
    }


def test_v2_event_ingested(tmp_path: Path) -> None:
    result = ingest_lane_event(tmp_path, _base_event(schema_version="lane.observation.v2"))
    assert result.get("reason") != "unsupported_schema_version"


def test_v1_event_rejected(tmp_path: Path) -> None:
    result = ingest_lane_event(tmp_path, _base_event(schema_version="lane.observation.v1"))
    assert result["reason"] == "unsupported_schema_version"
    assert result["schema_version"] == "lane.observation.v1"
    assert result["updated"] is False


def test_unrelated_schema_rejected(tmp_path: Path) -> None:
    result = ingest_lane_event(tmp_path, _base_event(schema_version="some.other.v9"))
    assert result["reason"] == "unsupported_schema_version"


def test_unstamped_event_ingested_for_backcompat(tmp_path: Path) -> None:
    """Events written before the schema_version field landed should still flow."""
    result = ingest_lane_event(tmp_path, _base_event(schema_version=None))
    assert result.get("reason") != "unsupported_schema_version"


def test_supported_prefix_constant_is_v2() -> None:
    assert SUPPORTED_LANE_SCHEMA_PREFIX == "lane.observation.v2"

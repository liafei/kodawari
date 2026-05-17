from __future__ import annotations

import json
from pathlib import Path

from kodawari.testing.pytest_summary_writer import (
    PYTEST_SUMMARY_SCHEMA_VERSION,
    build_pytest_summary_payload,
    write_pytest_summary,
)


def test_summary_payload_counts_outcomes_and_exit_code() -> None:
    payload = build_pytest_summary_payload(
        collected=7,
        exit_code=1,
        stats={
            "passed": [object(), object()],
            "failed": [object()],
            "error": [object()],
            "skipped": [object(), object(), object()],
        },
        started_at_utc="2026-05-02T00:00:00+00:00",
        finished_at_utc="2026-05-02T00:00:03+00:00",
        duration_seconds=3.14159,
    )

    assert payload["schema_version"] == PYTEST_SUMMARY_SCHEMA_VERSION
    assert payload["collected"] == 7
    assert payload["passed"] == 2
    assert payload["failed"] == 1
    assert payload["errors"] == 1
    assert payload["skipped"] == 3
    assert payload["exit_code"] == 1
    assert payload["status"] == "FAIL"
    assert payload["duration_seconds"] == 3.142


def test_write_summary_creates_parent_and_replaces_json(tmp_path: Path) -> None:
    target = tmp_path / "planning" / "pytest_summary_latest.json"
    write_pytest_summary(target, {"schema_version": PYTEST_SUMMARY_SCHEMA_VERSION, "status": "PASS"})

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"schema_version": PYTEST_SUMMARY_SCHEMA_VERSION, "status": "PASS"}


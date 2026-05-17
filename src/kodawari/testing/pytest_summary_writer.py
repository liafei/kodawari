"""Small pytest summary writer used by local lane scripts.

The writer intentionally avoids a dependency on pytest internals beyond the
shape of ``terminalreporter.stats``: a mapping of outcome name to report list.
That keeps the behavior easy to unit test and stable enough for CI metadata.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PYTEST_SUMMARY_SCHEMA_VERSION = "pytest.summary.v1"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count(stats: Mapping[str, Any], *keys: str) -> int:
    total = 0
    for key in keys:
        value = stats.get(key, [])
        try:
            total += len(value)
        except TypeError:
            total += 0
    return total


def build_pytest_summary_payload(
    *,
    collected: int,
    exit_code: int,
    stats: Mapping[str, Any] | None = None,
    started_at_utc: str = "",
    finished_at_utc: str = "",
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    stats = stats or {}
    payload: dict[str, Any] = {
        "schema_version": PYTEST_SUMMARY_SCHEMA_VERSION,
        "collected": int(collected),
        "passed": _count(stats, "passed"),
        "failed": _count(stats, "failed"),
        "errors": _count(stats, "error"),
        "skipped": _count(stats, "skipped"),
        "xfailed": _count(stats, "xfailed"),
        "xpassed": _count(stats, "xpassed"),
        "deselected": _count(stats, "deselected"),
        "exit_code": int(exit_code),
        "status": "PASS" if int(exit_code) == 0 else "FAIL",
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc or utc_now_iso(),
    }
    if duration_seconds is not None:
        payload["duration_seconds"] = round(float(duration_seconds), 3)
    return payload


def write_pytest_summary(path: Path, payload: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


__all__ = [
    "PYTEST_SUMMARY_SCHEMA_VERSION",
    "build_pytest_summary_payload",
    "utc_now_iso",
    "write_pytest_summary",
]


"""Sliding-window read discipline.

Pin the contract that:
  * a sliding-window read of an already-covered region stops earning
    observation_progress once cumulative coverage reaches ~70% of the file
  * the StallDetector hard-stops after too many small windows on one path
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kodawari.autopilot.execution.tool_use_result import tool_observation_made_progress
from kodawari.autopilot.execution.tool_use_stall import StallDetector, _cap
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError


@dataclass
class _RuntimeStub:
    observed_hashes: set[str] = field(default_factory=set)
    read_progress_ends: dict[str, int] = field(default_factory=dict)
    read_progress_windows: set[str] = field(default_factory=set)
    read_progress_window_counts: dict[str, int] = field(default_factory=dict)
    read_progress_total_bytes: dict[str, int] = field(default_factory=dict)


def _read_result(*, path: str, offset: int, content_bytes: int, file_size: int = 9000, sha: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "path": path,
        "offset": offset,
        "content_bytes": content_bytes,
        "file_size": file_size,
        "content_sha256": sha or f"sha-{offset}-{content_bytes}",
    }


def test_first_window_counts_as_observation_progress() -> None:
    runtime = _RuntimeStub()
    result = _read_result(path="src/big.py", offset=0, content_bytes=300)
    assert tool_observation_made_progress(runtime, "read_file_partial", result) is True


def test_sliding_window_stops_counting_once_coverage_saturated() -> None:
    runtime = _RuntimeStub()
    # Eight 1KB windows, end positions 1000, 2000, ..., 8000 — covers 8/9 ≈ 89%.
    for i in range(8):
        result = _read_result(path="src/big.py", offset=i * 1000, content_bytes=1000)
        assert tool_observation_made_progress(runtime, "read_file_partial", result) is True
    # Now a small backwards-shifted window: covers ground already scanned and
    # does not extend max_end. Saturation has set in (>=70%) — no progress.
    rewind = _read_result(path="src/big.py", offset=200, content_bytes=300, sha="rewind")
    assert tool_observation_made_progress(runtime, "read_file_partial", rewind) is False


def test_read_progress_recognizes_size_bytes_key() -> None:
    runtime = _RuntimeStub()
    result = {
        "ok": True,
        "path": "src/small.py",
        "offset": 0,
        "content_bytes": 6000,
        "size_bytes": 6000,
        "content_sha256": "whole-file",
    }
    assert tool_observation_made_progress(runtime, "read_file_partial", result) is True

    repeat = {
        "ok": True,
        "path": "src/small.py",
        "offset": 0,
        "content_bytes": 6000,
        "size_bytes": 6000,
        "content_sha256": "whole-file-again",
    }
    assert tool_observation_made_progress(runtime, "read_file_partial", repeat) is False


def test_re_read_ratio_kicks_in_without_known_file_size() -> None:
    """Even without file_size, pulling >1.5x the highest end seen blocks progress."""

    runtime = _RuntimeStub()
    # No file_size hint in result; total accumulates.
    for i in range(3):
        # Three large reads each covering 0..3000 (overlapping)
        result = _read_result(path="src/big.py", offset=0, content_bytes=3000, file_size=0, sha=f"v{i}")
        tool_observation_made_progress(runtime, "read_file_partial", result)
    # totals[path] = 9000, end_max = 3000 → ratio = 3.0 → saturated; a
    # non-extending small window must not register as progress.
    result = _read_result(path="src/big.py", offset=500, content_bytes=200, file_size=0, sha="rewind")
    assert tool_observation_made_progress(runtime, "read_file_partial", result) is False


def test_extending_window_still_progresses_after_saturation() -> None:
    """Saturation should not trap forward progress — only re-reads."""

    runtime = _RuntimeStub()
    for i in range(8):
        result = _read_result(path="src/big.py", offset=i * 1000, content_bytes=1000)
        tool_observation_made_progress(runtime, "read_file_partial", result)
    # New window beyond the 8000 mark: extends max — counts as progress.
    forward = _read_result(path="src/big.py", offset=8000, content_bytes=1000, sha="forward")
    assert tool_observation_made_progress(runtime, "read_file_partial", forward) is True


class _Cfg:
    pass


def test_fragmented_read_stall_fires_after_window_cap() -> None:
    detector = StallDetector(config=_Cfg())
    for count in range(1, 14):
        detector.record_fragmented_read(path="src/big.py", window_count=count)
    # P1-#7: default cap raised 8 → 12 to give refactor passes room.
    # window_count=13 must still trip the guard.
    with pytest.raises(OpenAIToolUseExecutionError) as info:
        detector.enforce_read_discipline()
    assert info.value.code == "EXECUTOR_STALLED_FRAGMENTED_READS"
    assert "src/big.py" in info.value.message


def test_fragmented_read_quiet_below_cap() -> None:
    detector = StallDetector(config=_Cfg())
    for count in range(1, 6):
        detector.record_fragmented_read(path="src/big.py", window_count=count)
    detector.enforce_read_discipline()  # no raise


def test_fragmented_read_normalizes_separators() -> None:
    detector = StallDetector(config=_Cfg())
    detector.record_fragmented_read(path="src\\big.py", window_count=13)
    with pytest.raises(OpenAIToolUseExecutionError) as info:
        detector.enforce_read_discipline()
    assert "src/big.py" in info.value.message


def test_no_write_with_observation_threshold_default_is_four() -> None:
    assert _cap(_Cfg(), "max_no_write_iterations_with_observation", 4) == 4

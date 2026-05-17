"""Unit tests for tool_use_read_cache.ReadCache."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from kodawari.autopilot.execution.tool_use_read_cache import ReadCache


def _make_file(tmp_path: Path, name: str, lines: int = 100) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(f"line {i}" for i in range(lines)), encoding="utf-8")
    return p


def test_check_returns_miss_for_empty_cache(tmp_path: Path) -> None:
    _make_file(tmp_path, "a.py")
    cache = ReadCache()
    decision = cache.check(tmp_path, "a.py", 0, 100)
    assert decision.is_hit is False
    assert decision.overlap_ratio == 0.0


def test_record_then_full_hit(tmp_path: Path) -> None:
    _make_file(tmp_path, "a.py")
    cache = ReadCache()
    cache.record(tmp_path, "a.py", 0, 100)
    # Same range → full hit
    d = cache.check(tmp_path, "a.py", 0, 100)
    assert d.is_hit is True
    assert d.overlap_ratio == 1.0
    assert (d.cached_start, d.cached_end) == (0, 100)


def test_partial_overlap_below_threshold_is_miss(tmp_path: Path) -> None:
    _make_file(tmp_path, "a.py")
    cache = ReadCache()
    cache.record(tmp_path, "a.py", 0, 100)  # cached [0, 100)
    # New range [50, 150) overlaps cached [0, 100) by 50 lines = 50% < 95%
    d = cache.check(tmp_path, "a.py", 50, 100)
    assert d.is_hit is False
    assert 0.4 < d.overlap_ratio < 0.6


def test_disjoint_range_is_miss(tmp_path: Path) -> None:
    _make_file(tmp_path, "a.py")
    cache = ReadCache()
    cache.record(tmp_path, "a.py", 0, 100)
    d = cache.check(tmp_path, "a.py", 200, 100)
    assert d.is_hit is False
    assert d.overlap_ratio == 0.0


def test_ranges_merge_on_overlap(tmp_path: Path) -> None:
    _make_file(tmp_path, "a.py")
    cache = ReadCache()
    cache.record(tmp_path, "a.py", 0, 50)
    cache.record(tmp_path, "a.py", 40, 60)  # overlaps [40,50)
    # Merged → [0, 100)
    assert cache.ranges["a.py"] == [(0, 100)]


def test_invalidate_drops_ranges(tmp_path: Path) -> None:
    _make_file(tmp_path, "a.py")
    cache = ReadCache()
    cache.record(tmp_path, "a.py", 0, 100)
    cache.invalidate("a.py")
    assert "a.py" not in cache.ranges
    assert "a.py" not in cache.mtimes
    # Subsequent check is a miss
    d = cache.check(tmp_path, "a.py", 0, 100)
    assert d.is_hit is False


def test_mtime_change_invalidates(tmp_path: Path) -> None:
    p = _make_file(tmp_path, "a.py")
    cache = ReadCache()
    cache.record(tmp_path, "a.py", 0, 100)
    assert cache.check(tmp_path, "a.py", 0, 100).is_hit is True

    # Simulate external mutation: bump mtime forward
    new_mtime = cache.mtimes["a.py"] + 10.0
    os.utime(p, (new_mtime, new_mtime))

    d = cache.check(tmp_path, "a.py", 0, 100)
    assert d.is_hit is False
    assert d.stale_mtime is True
    # Ranges were dropped
    assert "a.py" not in cache.ranges


def test_summary_for_prompt_caps_entries(tmp_path: Path) -> None:
    cache = ReadCache()
    for i in range(50):
        # Need real file for mtime; skip and use direct insert
        cache.ranges[f"file{i}.py"] = [(0, 100)]
    lines = cache.summary_for_prompt(max_entries=10)
    assert len(lines) == 11  # 10 entries + 1 elision line
    assert "more file" in lines[-1]


def test_summary_for_prompt_caps_chars(tmp_path: Path) -> None:
    cache = ReadCache()
    for i in range(20):
        cache.ranges[f"backend/api/v1/services/very_long_path_name_{i}.py"] = [(0, 1000)]
    lines = cache.summary_for_prompt(max_chars=200)
    total = sum(len(line) for line in lines)
    # Should be elided well under 30 entries
    assert len(lines) < 20
    assert any("more file" in line for line in lines)


def test_summary_for_prompt_empty_cache(tmp_path: Path) -> None:
    cache = ReadCache()
    assert cache.summary_for_prompt() == []


def test_full_hit_threshold_boundary(tmp_path: Path) -> None:
    """Verify is_hit triggers at ≥95% overlap."""
    _make_file(tmp_path, "a.py", lines=1000)
    cache = ReadCache()
    cache.record(tmp_path, "a.py", 0, 100)
    # 94% overlap → not a hit
    d = cache.check(tmp_path, "a.py", 6, 100)  # [6, 106), overlap [6,100) = 94 lines / 100
    assert d.is_hit is False
    # 95% overlap exactly → hit
    d = cache.check(tmp_path, "a.py", 5, 100)  # [5, 105), overlap [5,100) = 95 lines / 100
    assert d.is_hit is True

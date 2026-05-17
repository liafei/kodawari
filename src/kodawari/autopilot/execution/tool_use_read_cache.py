"""Per-session read-range cache for the openai_tool_use executor.

Tracks what ``[start, end)`` ranges of each path have been served to the
model. Cache hits do not block the read — the runtime still returns real
content (re-reading from disk is cheap) — but the result is tagged with
``_workflow_cache_hit=True`` so the stall detector can count "wasted"
reads and let deterministic recovery kick in sooner.

External-mutation defense: ``check()`` invalidates a path if its
``stat().st_mtime`` has changed since the last ``record()``. Same-session
mutations go through ``invalidate()`` directly from the mutation
handlers (``_str_replace``, ``_write_file``, ``_delete_file``,
``apply_patch_plan_item``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


_FULL_HIT_OVERLAP_THRESHOLD = 0.95


@dataclass
class CacheDecision:
    """Outcome of a ReadCache lookup.

    Attributes:
        is_hit: True when the queried range is ≥95% covered by an existing
            recorded range. Runtime should tag the result with
            ``_workflow_cache_hit`` to feed the stall detector.
        cached_start / cached_end: best-matching cached range, used for
            the human-readable instruction surfaced to the model.
        overlap_ratio: 0-1 fraction of the new range covered by cache.
        stale_mtime: True when the path's on-disk mtime changed since
            recording; the cache was just invalidated.
    """

    is_hit: bool
    cached_start: int = 0
    cached_end: int = 0
    overlap_ratio: float = 0.0
    stale_mtime: bool = False


@dataclass
class ReadCache:
    """Per-runtime read-range tracker. Not thread-safe (the openai_tool_use
    runtime runs one tool call at a time per session)."""

    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    mtimes: dict[str, float] = field(default_factory=dict)

    def check(self, project_root: Path, path: str, offset: int, limit: int) -> CacheDecision:
        end = int(offset) + max(int(limit), 1)
        try:
            cur_mtime = (project_root / path).stat().st_mtime
        except OSError:
            cur_mtime = 0.0
        prev_mtime = self.mtimes.get(path)
        if prev_mtime is not None and cur_mtime != prev_mtime:
            self.invalidate(path)
            return CacheDecision(is_hit=False, stale_mtime=True)
        existing = self.ranges.get(path, [])
        if not existing:
            return CacheDecision(is_hit=False)
        best_overlap = 0.0
        cs, ce = 0, 0
        new_size = max(end - int(offset), 1)
        for s, e in existing:
            inter_s = max(s, int(offset))
            inter_e = min(e, end)
            if inter_e <= inter_s:
                continue
            ratio = (inter_e - inter_s) / new_size
            if ratio > best_overlap:
                best_overlap, cs, ce = ratio, s, e
        return CacheDecision(
            is_hit=best_overlap >= _FULL_HIT_OVERLAP_THRESHOLD,
            cached_start=cs,
            cached_end=ce,
            overlap_ratio=best_overlap,
        )

    def record(self, project_root: Path, path: str, offset: int, limit: int) -> None:
        end = int(offset) + max(int(limit), 1)
        existing = self.ranges.setdefault(path, [])
        existing.append((int(offset), end))
        existing.sort()
        merged: list[tuple[int, int]] = []
        for s, e in existing:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        self.ranges[path] = merged
        try:
            self.mtimes[path] = (project_root / path).stat().st_mtime
        except OSError:
            pass

    def invalidate(self, path: str) -> None:
        """Drop all known ranges for ``path``. Call on any mutation attempt."""
        self.ranges.pop(path, None)
        self.mtimes.pop(path, None)

    def summary_for_prompt(self, max_entries: int = 30, max_chars: int = 1500) -> list[str]:
        """Lines like ``'channel_upgrade_engine.py: lines 1-100, 150-300'``.

        Capped at ``max_entries`` files and ``max_chars`` total chars so the
        injected reminder stays cheap. Excess elided as ``'… plus N more file(s)'``.
        """
        out: list[str] = []
        total = 0
        items = list(self.ranges.items())
        for i, (path, rngs) in enumerate(items):
            rng_str = ", ".join(f"lines {s}-{e}" for s, e in rngs)
            line = f"{path}: {rng_str}"
            if total + len(line) > max_chars or len(out) >= max_entries:
                remaining = len(items) - i
                if remaining > 0:
                    out.append(f"… plus {remaining} more file(s)")
                break
            out.append(line)
            total += len(line)
        return out


__all__ = ["CacheDecision", "ReadCache"]

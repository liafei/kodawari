"""File-level rollback for autopilot implement rounds."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _normalize_rel_path(path: str) -> str:
    return str(path or "").strip().replace("\\", "/")


def _parse_dirty_line(line: str) -> str:
    if len(line) <= 3:
        return ""
    return _normalize_rel_path(line[3:].strip().split(" -> ")[-1])


def _capture_snapshot(root: Path, normalized_rel: str) -> "FileSnapshot | None":
    full = (root / normalized_rel).resolve()
    if not full.is_relative_to(root):
        logger.warning("rollback capture: path escapes root: %s", normalized_rel)
        return None
    if full.is_file():
        try:
            return FileSnapshot(path=normalized_rel, existed=True, content=full.read_bytes())
        except OSError:
            logger.warning("rollback: cannot read %s", normalized_rel)
            return None
    return FileSnapshot(path=normalized_rel, existed=False, content=b"")


def _restore_snapshot(
    snap: "FileSnapshot",
    full: Path,
    path: str,
    reverted: list[str],
    removed: list[str],
    skipped: list[str],
) -> None:
    if not snap.existed:
        if full.exists():
            try:
                full.unlink()
                removed.append(path)
            except OSError:
                skipped.append(path)
    else:
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(snap.content)
            reverted.append(path)
        except OSError:
            skipped.append(path)


@dataclass
class FileSnapshot:
    path: str
    existed: bool
    content: bytes


@dataclass
class RollbackCheckpoint:
    cycle: int
    pre_dirty_files: set[str] = field(default_factory=set)
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)

    @classmethod
    def capture(
        cls,
        project_root: Path,
        target_files: list[str],
        *,
        cycle: int,
    ) -> "RollbackCheckpoint":
        """Call before implement. Snapshots target_files and records git dirty state."""
        root = project_root.resolve()

        # Record dirty files before implement
        pre_dirty: set[str] = set()
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root),
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    normalized = _parse_dirty_line(line)
                    if normalized:
                        pre_dirty.add(normalized)
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Snapshot target_files content
        snapshots: dict[str, FileSnapshot] = {}
        for rel_path in target_files:
            normalized_rel = _normalize_rel_path(rel_path)
            if not normalized_rel:
                continue
            snapshot = _capture_snapshot(root, normalized_rel)
            if snapshot is not None:
                snapshots[normalized_rel] = snapshot

        return cls(cycle=cycle, pre_dirty_files=pre_dirty, snapshots=snapshots)

    def rollback(
        self,
        project_root: Path,
        changed_files: list[str],
    ) -> dict[str, Any]:
        """Restore files to pre-implement state."""
        root = project_root.resolve()
        reverted: list[str] = []
        removed: list[str] = []
        skipped: list[str] = []
        dirty_scan_available = False

        # Use git status to discover changes the adapter didn't report
        reported_changed: set[str] = {
            normalized for normalized in (_normalize_rel_path(path) for path in changed_files)
            if normalized
        }
        actually_changed: set[str] = set(reported_changed)
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(root),
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=10,
            )
            dirty_scan_available = result.returncode == 0
            if dirty_scan_available:
                for line in result.stdout.splitlines():
                    f = _parse_dirty_line(line)
                    if f and f not in self.pre_dirty_files:
                        actually_changed.add(f)
        except (subprocess.TimeoutExpired, OSError):
            pass

        for path in sorted(actually_changed):
            full = (root / path).resolve()
            # Path containment check (security)
            if not full.is_relative_to(root):
                logger.warning("rollback: path escapes root: %s", path)
                skipped.append(path)
                continue

            snap = self.snapshots.get(path)
            if snap is None:
                # Not in snapshot — skip only, do NOT run git checkout.
                # git checkout HEAD -- <file> would overwrite concurrently
                # made clean edits (parallel processes, user edits).
                # Leave in skipped list for round_record visibility.
                skipped.append(path)
                continue

            _restore_snapshot(snap, full, path, reverted, removed, skipped)

        return {
            "reverted": reverted,
            "removed": removed,
            "skipped": skipped,
            "cycle": self.cycle,
            "extra_dirty_found": len(actually_changed) - len(reported_changed),
            "dirty_scan_available": dirty_scan_available,
        }


__all__ = ["FileSnapshot", "RollbackCheckpoint"]

"""Archive previous planning_dir artifacts under .history/<timestamp>/ on rerun.

The default semantics for restarting a feature are *idempotent + archived*:
the new run produces fresh artifacts, but the prior run's artifacts are not
clobbered — they move into ``planning_dir/.history/<utc_timestamp>/`` so
post-mortem and lane stability dashboards can still inspect what the
previous run produced.

This module is the single source of truth for what counts as an archivable
artifact and how the timestamped subdirectory is shaped.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


HISTORY_DIRNAME = ".history"

# Names of files (under planning_dir/) that should be archived on rerun.
# Mirrors the RunTruth artifact_paths manifest plus a few derived artifacts.
_ARCHIVE_NAMES: tuple[str, ...] = (
    ".execution_request.json",
    ".execution_result.json",
    ".execution_readiness.json",
    ".execution_recovery_card.json",
    ".execution_recovery_decision.json",
    ".execution_failure_snapshot.json",
    ".execution_stall_report.json",
    ".review_result.json",
    ".review_evidence.json",
    ".review_bundle.json",
    ".verify_report.json",
    ".task_run_result.json",
    ".lane_observation.json",
    ".workflow_chain.json",
    ".planning_failure.json",
    ".planning_in_progress.json",
    ".run_truth.json",
    "REVIEW.md",
    "DELIVERY_REPORT.md",
)


def archive_planning_artifacts(
    planning_dir: Path,
    *,
    timestamp: str | None = None,
    extra_names: Iterable[str] | None = None,
) -> Path | None:
    """Move existing artifacts under planning_dir into a timestamped history dir.

    Returns the created archive directory, or ``None`` if nothing was archived
    (no prior artifacts present, so the rerun starts cleanly).

    Behavior:
      * Only files (not directories) at the top of planning_dir are archived.
      * The timestamped directory is created lazily — if no artifacts exist
        the call is a no-op and the .history root is not created either.
      * Existing .history/ tree is never touched.

    The default name set covers the canonical RunTruth-tracked artifacts.
    Pass ``extra_names`` to archive additional bespoke files.
    """

    root = Path(planning_dir).resolve()
    if not root.exists() or not root.is_dir():
        return None
    names = list(_ARCHIVE_NAMES)
    if extra_names:
        names.extend(str(item) for item in extra_names if str(item).strip())

    movable: list[Path] = []
    for name in names:
        candidate = root / name
        if candidate.exists() and candidate.is_file():
            movable.append(candidate)

    if not movable:
        return None

    stamp = (timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).strip()
    archive_dir = root / HISTORY_DIRNAME / stamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    for source in movable:
        destination = archive_dir / source.name
        # If the same timestamp directory already has the file (unlikely but
        # possible under fast retries), append a numeric suffix.
        if destination.exists():
            counter = 1
            while True:
                candidate = archive_dir / f"{source.stem}.{counter}{source.suffix}"
                if not candidate.exists():
                    destination = candidate
                    break
                counter += 1
        shutil.move(str(source), str(destination))

    return archive_dir


__all__ = ["HISTORY_DIRNAME", "archive_planning_artifacts"]

"""Legacy import shim for canonical autopilot state models.

REMOVE_AFTER: 2026-08-01
REMOVAL_PLAN: Migrate callers to autopilot.state; delete once state_legacy imports are zero.
"""

from __future__ import annotations

from kodawari.autopilot.core.state_models import (
    LegacySubtaskCheckpoint,
    LegacySubtaskStatus,
    StateManager,
    TaskState,
    TaskStatus,
)

__all__ = [
    "LegacySubtaskCheckpoint",
    "LegacySubtaskStatus",
    "StateManager",
    "TaskState",
    "TaskStatus",
]

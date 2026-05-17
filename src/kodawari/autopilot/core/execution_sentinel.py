"""Shared backend-agnostic timeout-hint and completion-sentinel protocol.

Two executor backends (`codex_cli`, `claude_code`) need to handle the same
problem: a subprocess can take a long time, and a hard timeout that just
returns FAIL discards work that may have actually completed. The protocol:

1. The orchestrator sends an ``execution_timeout_hint`` field (``fast`` /
   ``normal`` / ``heavy``) in the request payload, computed from task
   complexity. ``resolve_timeout_seconds`` maps hints to wall-clock seconds.

2. The executor subprocess is instructed (via prompt boilerplate) to write a
   sentinel JSON file at the end of its run with ``status='verify_passed'``
   (or other terminal status) before exiting.

3. If the subprocess hits its wall-clock timeout, the parent reads the
   sentinel via ``read_sentinel`` BEFORE giving up. If sentinel exists with
   ``status='verify_passed'``, the parent performs a controlled sync of
   the isolated workspace into project_root and returns success-from-recovery.

Both executors must share these constants so a "verify_passed" written by
the codex backend would be readable by claude tooling and vice versa
(operationally rare, but the protocol must be one).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Filename written into the per-task isolated execution_root.
SENTINEL_FILENAME = ".execution_sentinel.json"

# Hint -> wall-clock seconds. The orchestrator sends a hint based on planned
# task complexity; the backend resolves it to an actual subprocess timeout.
TIMEOUT_HINT_MAP: dict[str, int] = {"fast": 600, "normal": 1200, "heavy": 1800}


def resolve_timeout_seconds(config: Any, request_payload: dict[str, Any]) -> int:
    """Return the effective subprocess timeout in seconds.

    A non-empty ``execution_timeout_hint`` in the request payload (one of
    ``fast``/``normal``/``heavy``) takes precedence over the backend config's
    static ``timeout_seconds``. Unknown hints fall through to the config
    default. ``config.timeout_seconds`` defaults to 600 if missing/zero.
    """
    hint = str(request_payload.get("execution_timeout_hint") or "").strip().lower()
    if hint in TIMEOUT_HINT_MAP:
        return TIMEOUT_HINT_MAP[hint]
    return int(getattr(config, "timeout_seconds", 600) or 600)


def sentinel_path(execution_root: Path) -> Path:
    """Return the conventional sentinel file path inside ``execution_root``."""
    return execution_root / SENTINEL_FILENAME


def read_sentinel(execution_root: Path) -> dict[str, Any] | None:
    """Read the sentinel JSON if present.

    Returns the parsed dict on success, ``None`` if missing/invalid.
    Used by the parent on TimeoutExpired to decide whether the subprocess
    actually completed (sentinel present) or genuinely hung (sentinel absent).
    """
    path = sentinel_path(execution_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def sentinel_indicates_verify_passed(sentinel: dict[str, Any] | None) -> bool:
    """Return True iff the sentinel says the subprocess completed verification.

    Centralizes the magic-string ``"verify_passed"`` comparison so that
    extending the protocol to other terminal statuses can happen in one place.
    """
    if not sentinel:
        return False
    return str(sentinel.get("status") or "").strip().lower() == "verify_passed"


__all__ = [
    "SENTINEL_FILENAME",
    "TIMEOUT_HINT_MAP",
    "read_sentinel",
    "resolve_timeout_seconds",
    "sentinel_indicates_verify_passed",
    "sentinel_path",
]

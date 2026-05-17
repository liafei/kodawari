"""Shared environment variable helpers for the autopilot subsystem."""

from __future__ import annotations

import logging
import os
from typing import Any
import warnings

logger = logging.getLogger(__name__)
LEGACY_ENV_REMOVE_AFTER = "2026-11-01"


def warn_deprecated_env(old_name: str, new_name: str) -> None:
    warnings.warn(
        (
            f"Environment variable {old_name} is deprecated; set {new_name} instead. "
            f"{old_name} will be removed after {LEGACY_ENV_REMOVE_AFTER}."
        ),
        DeprecationWarning,
        stacklevel=3,
    )


def clean_text(value: Any) -> str:
    return str(value or "").strip()


def env_text(name: str, default: str) -> str:
    raw = str(os.getenv(name, "") or "").strip()
    return raw if raw else str(default or "")


def env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return int(raw)
    except ValueError:
        return int(default)


def env_flag(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    return raw in {"1", "true", "yes", "on"}


def _env_state(name: str) -> str:
    return str(os.getenv(name, "") or "").strip().lower()


def env_is_enabled(name: str) -> bool:
    return _env_state(name) in {"1", "true", "yes", "on"}


def env_is_disabled(name: str) -> bool:
    return _env_state(name) in {"0", "false", "no", "off"}


def env_flag_optional(name: str) -> bool | None:
    """Three-state: True if explicitly enabled, False if explicitly disabled, None if unset."""
    raw = _env_state(name)
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def env_flag_new_or_old(new_name: str, old_name: str) -> bool | None:
    """New var takes absolute priority when explicitly set; else fall back to old var."""
    new_val = env_flag_optional(new_name)
    if new_val is not None:
        return new_val
    old_val = env_flag_optional(old_name)
    if old_val is not None:
        warn_deprecated_env(old_name, new_name)
    return old_val


def env_new_or_old(new_name: str, old_name: str, default: str) -> str:
    """Read from new env var; fall back to old env var; then to default."""
    new_val = env_text(new_name, "")
    if new_val:
        return new_val
    old_val = env_text(old_name, "")
    if old_val:
        warn_deprecated_env(old_name, new_name)
        return old_val
    return str(default or "")


def sanitize_model(value: str) -> str:
    """Validate and sanitize a model identifier for CLI --model flags."""
    clean = str(value or "").strip()
    if not clean:
        return ""
    if len(clean) > 200:
        logger.error("model value too long (%d chars), rejecting", len(clean))
        return ""
    if clean.startswith("-"):
        logger.error("model value %r starts with dash, rejecting to prevent CLI flag injection", clean)
        return ""
    if any(ord(c) < 32 for c in clean):
        logger.error("model value contains control characters, rejecting")
        return ""
    return clean

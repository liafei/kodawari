"""Secret redaction helpers for workflow runtime artifacts."""

from __future__ import annotations

import json
import os
import re
from typing import Any


_STATIC_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9._\-]{20,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9._\-]{20,}\b"),
    re.compile(r"\btp-[A-Za-z0-9._\-]{20,}\b"),
)
_SECRET_ENV_TOKENS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")


def _secret_env_values() -> list[str]:
    values: list[str] = []
    for key, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        upper = key.upper()
        if any(token in upper for token in _SECRET_ENV_TOKENS):
            values.append(str(value))
    return sorted(set(values), key=len, reverse=True)


def redact_secret_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return text
    redacted = text
    for secret in _secret_env_values():
        redacted = redacted.replace(secret, "<redacted>")
    for pattern in _STATIC_SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(0).split()[0] + " <redacted>" if match.group(0).lower().startswith("bearer ") else "<redacted>", redacted)
    return redacted


def redact_secrets(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secret_text(value)
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_secrets(item) for key, item in value.items()}
    return value


def redact_jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except TypeError:
        return redact_secret_text(value)
    return redact_secrets(value)


__all__ = ["redact_jsonable", "redact_secret_text", "redact_secrets"]

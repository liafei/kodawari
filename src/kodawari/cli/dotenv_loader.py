"""Minimal .env loader for the kodawari CLI.

The workflow CLI needs API keys (reviewer gateway, executor backend,
instincts ingest, etc.) but is invoked from many shells across many
machines. We load a project-local ``.env`` at CLI startup so contributors
do not have to pre-export every key for every invocation.

Behavior:
  * Look for ``.env`` first in the current working directory, then walk
    upward to the nearest ``planning/`` or ``.git/`` parent. Stop at the
    filesystem root.
  * Parse ``KEY=VALUE`` lines. Strip surrounding single/double quotes.
    Skip blank lines and ``#``-prefixed comments.
  * ``os.environ.setdefault`` — never clobber a pre-existing variable.
    A shell-exported value wins over the file.

Pure Python; no dependency on python-dotenv.
"""

from __future__ import annotations

import os
from pathlib import Path

DOTENV_FILENAME = ".env"
_MAX_LOOKUP_DEPTH = 6


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse the textual contents of a .env file into a mapping."""

    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not key[0].isalpha() and key[0] != "_":
            continue
        out[key] = _strip_quotes(value.strip())
    return out


def find_dotenv(start: Path | None = None) -> Path | None:
    """Walk upward from ``start`` (default cwd) looking for a .env file."""

    cursor = (Path(start) if start else Path.cwd()).resolve()
    for _ in range(_MAX_LOOKUP_DEPTH):
        candidate = cursor / DOTENV_FILENAME
        if candidate.exists() and candidate.is_file():
            return candidate
        if cursor.parent == cursor:
            return None
        cursor = cursor.parent
    return None


def load_dotenv(start: Path | None = None, *, override: bool = False) -> Path | None:
    """Load .env into os.environ. Returns the path loaded, or None."""

    path = find_dotenv(start)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for key, value in parse_dotenv(text).items():
        if override or os.environ.get(key) is None:
            os.environ[key] = value
    return path


__all__ = ["DOTENV_FILENAME", "find_dotenv", "load_dotenv", "parse_dotenv"]

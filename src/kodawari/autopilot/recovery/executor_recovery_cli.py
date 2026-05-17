"""CLI input helpers for executor recovery synthesis."""

from __future__ import annotations

from pathlib import Path
import shutil


def _resolved_executable(executable: str) -> str:
    candidate = Path(str(executable or "").strip())
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(str(executable or "").strip())
    return str(Path(resolved)) if resolved else str(executable or "").strip()


def _looks_like_executable(executable: str, stem: str) -> bool:
    name = Path(executable).stem.lower()
    return name == stem or name.startswith(f"{stem}-") or name.startswith(f"{stem}_")


def _safe_cli_model(value: str) -> str:
    text = str(value or "").strip()
    if not text or text.startswith("-") or len(text) > 200 or any(ord(char) < 32 for char in text):
        return ""
    return text


def _safe_reasoning_effort(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"low", "medium", "high", "xhigh"} else ""


"""Shared path and text helpers for tool-use execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any


_SKIP_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "token.json",
}


def _looks_like_repo_context_path(text: str) -> bool:
    if "\n" in text or "\r" in text:
        return False
    if "/" in text or "\\" in text:
        return True
    suffixes = (".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".md", ".sql", ".txt")
    return text.endswith(suffixes)


def _is_test_path(text: str) -> bool:
    normalized = _normalize_rel(text).lower()
    name = Path(normalized).name
    return normalized.startswith("tests/") or name.startswith("test_") or name.endswith("_test.py")


def _task_id_tokens(payload: dict[str, Any]) -> list[str]:
    values = [
        payload.get("task_id"),
        payload.get("task"),
        payload.get("feature"),
    ]
    task_card = payload.get("task_card")
    if isinstance(task_card, dict):
        values.extend([task_card.get("task_id"), task_card.get("task_name")])
    tokens: list[str] = []
    for raw in values:
        text = str(raw or "")
        for match in re.finditer(r"t[0-9]{2,4}", text, re.IGNORECASE):
            token = match.group(0)
            if token and token not in tokens:
                tokens.append(token)
    return tokens


def _dedupe_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    for raw in paths:
        text = _normalize_rel(str(raw or ""))
        if text and text not in out:
            out.append(text)
    return out


def _normalize_rel(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _replacement_texts_for_content(
    content: str,
    *,
    old_text: str,
    new_text: str,
    expected: int,
) -> tuple[int, str, str]:
    effective_old_text = old_text
    effective_new_text = new_text
    actual = content.count(effective_old_text)
    if actual != expected and "\n" in old_text and "\r\n" not in old_text and "\r\n" in content:
        crlf_old_text = old_text.replace("\n", "\r\n")
        crlf_actual = content.count(crlf_old_text)
        if crlf_actual == expected:
            effective_old_text = crlf_old_text
            effective_new_text = new_text.replace("\n", "\r\n")
            actual = crlf_actual
    if actual != expected and "\r\n" in old_text and "\r\n" not in content:
        lf_old_text = old_text.replace("\r\n", "\n")
        lf_actual = content.count(lf_old_text)
        if lf_actual == expected:
            effective_old_text = lf_old_text
            effective_new_text = new_text.replace("\r\n", "\n")
            actual = lf_actual
    return actual, effective_old_text, effective_new_text


def _text_count_with_line_ending_variants(content: str, text: str) -> int:
    counts = [content.count(text)]
    if "\n" in text and "\r\n" not in text and "\r\n" in content:
        counts.append(content.count(text.replace("\n", "\r\n")))
    if "\r\n" in text and "\r\n" not in content:
        counts.append(content.count(text.replace("\r\n", "\n")))
    return max(counts)


def _file_hash(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_hashes(root: Path, files: list[str]) -> dict[str, str | None]:
    return {rel: _file_hash(root / rel) for rel in files}


def _changed_files_from_hashes(root: Path, files: list[str], before: dict[str, str | None]) -> list[str]:
    changed: list[str] = []
    for rel in files:
        if _file_hash(root / rel) != before.get(rel):
            changed.append(rel)
    return changed


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    base = Path(directory)
    return {
        name
        for name in names
        if name in _SKIP_NAMES or _looks_secret(name) or (base / name).is_symlink()
    }


def _looks_secret(name: str) -> bool:
    lower = str(name or "").lower()
    return lower in _SECRET_NAMES or lower.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"))


def _cap(config: Any, key: str, default: int) -> int:
    caps = getattr(config, "runtime_caps", None)
    if isinstance(caps, dict):
        try:
            return int(caps.get(key) or default)
        except (TypeError, ValueError):
            return default
    return default


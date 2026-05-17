"""Duplication checker for kodawari source trees."""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Sequence


_SKIP_DIRS = {
    ".git",
    ".hg",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
}

_DUPLICATE_MESSAGE_ID = "R0801"
_DUPLICATE_RULE = "duplicate_code"


@dataclass(frozen=True)
class DuplicationEvidence:
    file: str
    rule: str
    hit: str
    confidence: float
    line: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "file": self.file,
            "rule": self.rule,
            "hit": self.hit,
            "confidence": float(self.confidence),
        }
        if self.line is not None:
            payload["line"] = int(self.line)
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class DuplicationBlock:
    group_key: str
    message_id: str
    message: str
    file: str
    line: int | None
    symbol: str | None
    related_paths: list[str] = field(default_factory=list)
    occurrence_count: int = 1
    raw_messages: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "group_key": self.group_key,
            "message_id": self.message_id,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "symbol": self.symbol,
            "related_paths": list(self.related_paths),
            "occurrence_count": int(self.occurrence_count),
        }
        if self.raw_messages:
            payload["raw_messages"] = [dict(item) for item in self.raw_messages]
        return payload


@dataclass(frozen=True)
class PylintRunResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DuplicationReport:
    checker: str
    status: str
    tool: str
    tool_available: bool
    checked_files: int
    target_count: int
    duplicate_blocks: list[DuplicationBlock] = field(default_factory=list)
    evidence: list[DuplicationEvidence] = field(default_factory=list)
    details: str = ""
    tool_command: list[str] = field(default_factory=list)
    returncode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checker": self.checker,
            "status": self.status,
            "tool": self.tool,
            "tool_available": bool(self.tool_available),
            "checked_files": int(self.checked_files),
            "target_count": int(self.target_count),
            "duplicate_block_count": len(self.duplicate_blocks),
            "duplicate_occurrence_count": sum(block.occurrence_count for block in self.duplicate_blocks),
            "blocks": [item.to_dict() for item in self.duplicate_blocks],
            "evidence": [item.to_dict() for item in self.evidence],
            "evidence_count": len(self.evidence),
            "details": self.details,
            "tool_command": list(self.tool_command),
            "returncode": self.returncode,
        }


def _normalize_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _is_skipped_path(path: Path) -> bool:
    return any(part in _SKIP_DIRS for part in path.parts)


def _iter_python_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target.resolve()] if target.suffix == ".py" else []
    if not target.exists():
        return []
    files: list[Path] = []
    for path in target.rglob("*.py"):
        if _is_skipped_path(path):
            continue
        files.append(path.resolve())
    return files


def discover_python_files(targets: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        files.extend(_iter_python_files(target))
    return sorted({path for path in files})


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = path.resolve()
        key = resolved.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _resolve_pylint_command() -> list[str] | None:
    explicit = str(os.environ.get("WORKFLOW_PYLINT_EXECUTABLE") or "").strip()
    if explicit:
        return [explicit]
    found = shutil.which("pylint")
    if found:
        return [found]
    if importlib.util.find_spec("pylint") is not None:
        return [sys.executable, "-m", "pylint"]
    return None


def _build_pylint_command(targets: Sequence[Path], *, min_similarity_lines: int) -> list[str]:
    command = _resolve_pylint_command()
    if command is None:
        return []
    return [
        *command,
        "--output-format=json",
        "--reports=n",
        "--disable=all",
        "--enable=R0801",
        f"--min-similarity-lines={int(min_similarity_lines)}",
        *[path.as_posix() for path in targets],
    ]


def _run_pylint(command: Sequence[str]) -> PylintRunResult:
    try:
        run = subprocess.run(
            list(command),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        return PylintRunResult(
            command=list(command),
            returncode=127,
            stdout="",
            stderr=str(exc),
        )
    return PylintRunResult(
        command=list(command),
        returncode=int(run.returncode),
        stdout=str(run.stdout or ""),
        stderr=str(run.stderr or ""),
    )


def _normalize_message_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "\n".join(line.rstrip() for line in text.splitlines())


def _group_key(message_id: str, message: str) -> str:
    return f"{message_id}:{message}"


def _extract_referenced_paths(message: str, project_root: Path) -> list[str]:
    related: list[str] = []
    for line in message.splitlines():
        text = line.strip()
        if not text.startswith("=="):
            continue
        body = text[2:].strip()
        if ":" not in body:
            continue
        path_text, suffix = body.rsplit(":", 1)
        path_text = path_text.strip()
        if not path_text:
            continue
        normalized = _normalize_text_path(path_text, project_root)
        if normalized not in related:
            related.append(normalized)
    return related


def _normalize_text_path(raw: str, project_root: Path) -> str:
    candidate = Path(str(raw).strip())
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            return candidate.resolve().as_posix()
    return candidate.as_posix().replace("\\", "/")


def _parse_pylint_messages(stdout: str) -> tuple[list[dict[str, Any]], str]:
    text = str(stdout or "").strip()
    if not text:
        return [], ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"pylint output is not valid JSON: {exc.msg}"
    if not isinstance(payload, list):
        return [], "pylint output root is not a JSON array"
    messages = [item for item in payload if isinstance(item, dict)]
    return messages, ""


def _duplicate_message(item: dict[str, Any]) -> bool:
    message_id = str(item.get("message-id") or item.get("message_id") or "").strip().upper()
    symbol = str(item.get("symbol") or "").strip().lower()
    message = str(item.get("message") or "").strip().lower()
    return message_id == _DUPLICATE_MESSAGE_ID or symbol == "duplicate-code" or symbol == "duplicate_code" or "duplicate code" in message


def _normalize_message_record(item: dict[str, Any], *, project_root: Path) -> dict[str, Any]:
    path = _normalize_text_path(str(item.get("path") or ""), project_root)
    line_raw = item.get("line")
    line = None
    try:
        if line_raw is not None and str(line_raw).strip():
            line = int(line_raw)
    except (TypeError, ValueError):
        line = None
    message = _normalize_message_text(item.get("message"))
    related = _extract_referenced_paths(message, project_root)
    if path and path not in related:
        related.insert(0, path)
    return {
        "message_id": str(item.get("message-id") or item.get("message_id") or "").strip().upper() or _DUPLICATE_MESSAGE_ID,
        "message": message,
        "path": path,
        "line": line,
        "symbol": str(item.get("symbol") or "").strip() or None,
        "module": str(item.get("module") or "").strip() or None,
        "obj": str(item.get("obj") or "").strip() or None,
        "related_paths": related,
    }


def _build_blocks(messages: list[dict[str, Any]], *, project_root: Path) -> list[DuplicationBlock]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in messages:
        if not _duplicate_message(item):
            continue
        normalized = _normalize_message_record(item, project_root=project_root)
        key = _group_key(normalized["message_id"], normalized["message"])
        grouped.setdefault(key, []).append(normalized)

    blocks: list[DuplicationBlock] = []
    for key, items in grouped.items():
        primary = items[0]
        related_paths: list[str] = []
        for item in items:
            for related in list(item.get("related_paths") or []):
                if related not in related_paths:
                    related_paths.append(related)
        blocks.append(
            DuplicationBlock(
                group_key=key,
                message_id=str(primary.get("message_id") or _DUPLICATE_MESSAGE_ID),
                message=str(primary.get("message") or ""),
                file=str(primary.get("path") or ""),
                line=primary.get("line"),
                symbol=primary.get("symbol"),
                related_paths=related_paths,
                occurrence_count=len(items),
                raw_messages=[dict(item) for item in items],
            )
        )
    blocks.sort(key=lambda item: (item.file, item.line or 0, item.group_key))
    return blocks


def _build_evidence(blocks: list[DuplicationBlock]) -> list[DuplicationEvidence]:
    evidence: list[DuplicationEvidence] = []
    for block in blocks:
        evidence.append(
            DuplicationEvidence(
                file=block.file or ", ".join(block.related_paths[:2]) or "<unknown>",
                rule=_DUPLICATE_RULE,
                hit=block.message or "duplicate-code block detected",
                confidence=0.95,
                line=block.line,
                metadata={
                    "group_key": block.group_key,
                    "message_id": block.message_id,
                    "related_paths": list(block.related_paths),
                    "occurrence_count": block.occurrence_count,
                },
            )
        )
    return evidence


def run_duplication_checker(
    files: Iterable[Path],
    *,
    project_root: Path,
    min_similarity_lines: int = 10,
) -> DuplicationReport:
    project_root = project_root.resolve()
    targets = _dedupe_paths(Path(path) for path in files)
    if not targets:
        targets = [project_root]
    checked_files = discover_python_files(targets)
    command = _build_pylint_command(targets, min_similarity_lines=min_similarity_lines)
    if not command:
        evidence = [
            DuplicationEvidence(
                file="<tool>",
                rule="duplicate_code.tool_unavailable",
                hit="pylint is not available in this environment",
                confidence=0.0,
                metadata={"reason": "pylint unavailable", "target_count": len(targets)},
            )
        ]
        return DuplicationReport(
            checker="duplication",
            status="WARN",
            tool="pylint",
            tool_available=False,
            checked_files=len(checked_files),
            target_count=len(targets),
            duplicate_blocks=[],
            evidence=evidence,
            details="pylint is not available; duplication scan skipped.",
        )

    run = _run_pylint(command)
    messages, parse_error = _parse_pylint_messages(run.stdout)
    blocks = _build_blocks(messages, project_root=project_root) if not parse_error else []
    evidence = _build_evidence(blocks)

    if parse_error:
        evidence.append(
            DuplicationEvidence(
                file="<tool>",
                rule="duplicate_code.parse_error",
                hit=parse_error,
                confidence=0.0,
                metadata={"returncode": run.returncode, "stderr": run.stderr, "command": list(run.command)},
            )
        )
        return DuplicationReport(
            checker="duplication",
            status="WARN",
            tool="pylint",
            tool_available=True,
            checked_files=len(checked_files),
            target_count=len(targets),
            duplicate_blocks=[],
            evidence=evidence,
            details=parse_error,
            tool_command=list(run.command),
            returncode=run.returncode,
        )

    if blocks:
        return DuplicationReport(
            checker="duplication",
            status="FAIL",
            tool="pylint",
            tool_available=True,
            checked_files=len(checked_files),
            target_count=len(targets),
            duplicate_blocks=blocks,
            evidence=evidence,
            details=f"Detected {len(blocks)} duplicate-code block(s).",
            tool_command=list(run.command),
            returncode=run.returncode,
        )

    if run.returncode != 0:
        evidence.append(
            DuplicationEvidence(
                file="<tool>",
                rule="duplicate_code.tool_error",
                hit="pylint returned a non-zero exit code without duplicate blocks",
                confidence=0.0,
                metadata={"returncode": run.returncode, "stderr": run.stderr, "command": list(run.command)},
            )
        )
        return DuplicationReport(
            checker="duplication",
            status="WARN",
            tool="pylint",
            tool_available=True,
            checked_files=len(checked_files),
            target_count=len(targets),
            duplicate_blocks=[],
            evidence=evidence,
            details="pylint returned a non-zero exit code.",
            tool_command=list(run.command),
            returncode=run.returncode,
        )

    return DuplicationReport(
        checker="duplication",
        status="PASS",
        tool="pylint",
        tool_available=True,
        checked_files=len(checked_files),
        target_count=len(targets),
        duplicate_blocks=[],
        evidence=[],
        details="No duplicate-code blocks were reported.",
        tool_command=list(run.command),
        returncode=run.returncode,
    )


__all__ = [
    "DuplicationBlock",
    "DuplicationEvidence",
    "DuplicationReport",
    "PylintRunResult",
    "discover_python_files",
    "run_duplication_checker",
]

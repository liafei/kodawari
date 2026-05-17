"""Audit release archives for runtime, planning, and credential residue."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path
from typing import Iterable, Sequence
import sys
import tarfile
import zipfile


SCHEMA_VERSION = "release.audit.v1"
DEFAULT_DIST_DIR = "dist"
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024

ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz", ".tgz")
FORBIDDEN_DIR_SEGMENTS = {
    ".workflow",
    ".workflow_runtime",
    ".workflow_real_runs",
    ".venv",
    ".venv_probe",
    ".tmp",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".codex",
    ".claude",
    "node_modules",
    "target",
}
FORBIDDEN_ROOT_PREFIXES = ("planning/", ".workflow/", ".workflow_runtime/", ".workflow_real_runs/", ".tmp/")
FORBIDDEN_EXACT_BASENAMES = {
    ".env",
    ".execution_request.json",
    ".execution_result.json",
    ".review_bundle.json",
    ".review_result.json",
    ".review_evidence.json",
    ".verify_report.json",
    ".gate_result.json",
    ".status_snapshot.json",
    ".autopilot_state.json",
    ".work_all_manifest.json",
    ".autopilot_rounds.jsonl",
    "auth.json",
    ".credentials.json",
    "credentials.json",
    "pytest_summary_latest.json",
}
FORBIDDEN_SUFFIXES = (
    ".log",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".har",
    ".trace",
    ".coverage",
)
CREDENTIAL_GLOBS = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.token",
    "*.secret",
    "*credentials*.json",
    "*auth.json",
)


@dataclass(frozen=True)
class ReleaseMember:
    source: str
    raw_path: str
    logical_path: str
    size: int
    is_dir: bool = False


def _normalize_archive_path(name: str) -> str:
    normalized = name.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            parts.append("__parent__")
        else:
            parts.append(part)
    return "/".join(parts)


def _strip_common_archive_root(paths: Sequence[str]) -> dict[str, str]:
    first_segments = {path.split("/", 1)[0] for path in paths if "/" in path}
    if len(first_segments) != 1:
        return {path: path for path in paths}
    root = next(iter(first_segments))
    if root in {"kodawari", "kodawari.egg-info"} or root.endswith(".dist-info"):
        return {path: path for path in paths}
    prefix = f"{root}/"
    return {path: path[len(prefix) :] if path.startswith(prefix) else path for path in paths}


def _is_archive(path: Path) -> bool:
    text = path.name.lower()
    return text.endswith(ARCHIVE_SUFFIXES)


def _expand_inputs(paths: Sequence[Path]) -> list[Path]:
    expanded: list[Path] = []
    for path in paths:
        if path.is_dir():
            archives = sorted(child for child in path.iterdir() if child.is_file() and _is_archive(child))
            if archives:
                expanded.extend(archives)
            else:
                expanded.append(path)
        else:
            expanded.append(path)
    return expanded


def _directory_members(path: Path) -> list[ReleaseMember]:
    members: list[ReleaseMember] = []
    for item in sorted(path.rglob("*")):
        relative = _normalize_archive_path(item.relative_to(path).as_posix())
        members.append(
            ReleaseMember(
                source=str(path),
                raw_path=relative,
                logical_path=relative,
                size=0 if item.is_dir() else item.stat().st_size,
                is_dir=item.is_dir(),
            )
        )
    return members


def _zip_members(path: Path) -> list[ReleaseMember]:
    with zipfile.ZipFile(path) as archive:
        infos = [info for info in archive.infolist() if _normalize_archive_path(info.filename)]
    normalized_paths = [_normalize_archive_path(info.filename) for info in infos]
    logical_map = _strip_common_archive_root(normalized_paths)
    return [
        ReleaseMember(
            source=str(path),
            raw_path=raw,
            logical_path=logical_map[raw],
            size=int(info.file_size),
            is_dir=info.is_dir(),
        )
        for info, raw in zip(infos, normalized_paths)
    ]


def _tar_members(path: Path) -> list[ReleaseMember]:
    with tarfile.open(path) as archive:
        infos = [info for info in archive.getmembers() if _normalize_archive_path(info.name)]
    normalized_paths = [_normalize_archive_path(info.name) for info in infos]
    logical_map = _strip_common_archive_root(normalized_paths)
    return [
        ReleaseMember(
            source=str(path),
            raw_path=raw,
            logical_path=logical_map[raw],
            size=int(info.size),
            is_dir=info.isdir(),
        )
        for info, raw in zip(infos, normalized_paths)
    ]


def _iter_members(path: Path) -> list[ReleaseMember]:
    if not path.exists():
        raise FileNotFoundError(f"release audit input does not exist: {path}")
    if path.is_dir():
        return _directory_members(path)
    lower_name = path.name.lower()
    if lower_name.endswith((".whl", ".zip")):
        return _zip_members(path)
    if lower_name.endswith((".tar.gz", ".tgz")):
        return _tar_members(path)
    raise ValueError(f"unsupported release audit input: {path}")


def _is_allowed(path: str, allow_globs: Sequence[str]) -> bool:
    normalized = path.lower()
    return any(fnmatch.fnmatch(normalized, pattern.lower()) for pattern in allow_globs)


def _classify_violation(member: ReleaseMember, *, max_file_bytes: int) -> str:
    path = member.logical_path
    lower_path = path.lower()
    parts = [part for part in lower_path.split("/") if part]
    basename = parts[-1] if parts else lower_path

    if "__parent__" in parts:
        return "path_traversal"
    if any(segment in FORBIDDEN_DIR_SEGMENTS for segment in parts):
        return "forbidden_runtime_directory"
    if any(lower_path.startswith(prefix) for prefix in FORBIDDEN_ROOT_PREFIXES):
        return "forbidden_runtime_or_planning_root"
    if basename in FORBIDDEN_EXACT_BASENAMES:
        return "forbidden_runtime_or_secret_file"
    if any(fnmatch.fnmatch(basename, pattern.lower()) for pattern in CREDENTIAL_GLOBS):
        return "forbidden_credential_like_file"
    if lower_path.endswith(FORBIDDEN_SUFFIXES):
        return "forbidden_log_or_evidence_file"
    if not member.is_dir and max_file_bytes > 0 and member.size > max_file_bytes:
        return "large_release_member"
    return ""


def audit_paths(
    paths: Sequence[Path],
    *,
    allow_globs: Sequence[str] = (),
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> dict[str, object]:
    inputs = _expand_inputs(paths)
    violations: list[dict[str, object]] = []
    scanned_members = 0

    for path in inputs:
        members = _iter_members(path)
        scanned_members += len(members)
        for member in members:
            if _is_allowed(member.logical_path, allow_globs):
                continue
            reason = _classify_violation(member, max_file_bytes=max_file_bytes)
            if not reason:
                continue
            violations.append(
                {
                    "source": member.source,
                    "path": member.logical_path,
                    "raw_path": member.raw_path,
                    "reason": reason,
                    "size": member.size,
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "FAIL" if violations else "PASS",
        "inputs": [str(path) for path in inputs],
        "scanned_members": scanned_members,
        "violations": violations,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit kodawari release archives for forbidden runtime residue.")
    parser.add_argument("paths", nargs="*", help="Release archives or directories to audit. Defaults to ./dist.")
    parser.add_argument("--allow", action="append", default=[], help="Case-insensitive glob allowlist for member paths.")
    parser.add_argument("--max-file-mb", type=float, default=10.0, help="Maximum non-allowlisted member size in MiB.")
    parser.add_argument("--output", default="", help="Optional JSON report path.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    raw_paths = args.paths or [DEFAULT_DIST_DIR]
    max_file_bytes = int(float(args.max_file_mb) * 1024 * 1024)
    try:
        payload = audit_paths(
            [Path(path) for path in raw_paths],
            allow_globs=[str(item) for item in args.allow],
            max_file_bytes=max_file_bytes,
        )
    except Exception as exc:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "error": str(exc),
            "inputs": raw_paths,
            "violations": [],
        }
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

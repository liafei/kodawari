"""Task-card contract preflight checks.

Phase-3 preflight starts with file-existence checks (v1 cards) and adds richer
contract validation for v1.1 cards:

- verify_cmd must be non-empty and must not reference absolute paths outside
  project_root.
- Large files (>800 lines) require target_symbols unless deep-mode exemption is
  explicitly acknowledged.
- Python target_symbols/read_only_symbols must be resolvable via AST.
- freshness.source_file_hashes must match HEAD content (sha256 + line_count).
- allowed_test_mutations must reference behavior_changes and stay within
  related_existing_tests ∪ files_to_change.
"""
from __future__ import annotations

import ast
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
from typing import Any, Optional


_ENV_VAR = "WORKFLOW_CONTRACT_PREFLIGHT"
_DISABLE_PREEXISTING_NEW_FILES_ENV = "WORKFLOW_DISABLE_PREEXISTING_NEW_FILE_ACCEPTANCE"
_OFF_VALUES = {"0", "off", "false", "no", ""}
_V1_1 = "contract_first.task_card.v1.1"
_LARGE_FILE_LINE_THRESHOLD = 800
_WINDOWS_ABS_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def preflight_enabled() -> bool:
    """Return True unless env var explicitly disables preflight."""
    raw = os.environ.get(_ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() not in _OFF_VALUES


@dataclass(frozen=True)
class FilePreflightIssue:
    kind: str
    path: str
    possible_matches: tuple[str, ...] = ()
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "possible_matches": list(self.possible_matches),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class FilePreflightReport:
    blocked: bool
    issues: tuple[FilePreflightIssue, ...] = field(default_factory=tuple)
    warnings: tuple[FilePreflightIssue, ...] = field(default_factory=tuple)
    skipped: bool = False
    skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "issues": [issue.to_dict() for issue in self.issues],
            "warnings": [issue.to_dict() for issue in self.warnings],
        }


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean_text(item) for item in value if _clean_text(item)]


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _normalize_rel(path: str) -> str:
    return _clean_text(path).replace("\\", "/")


def _tokenize(stem: str) -> set[str]:
    raw = stem.lower().replace("-", "_").replace(".", "_")
    return {tok for tok in raw.split("_") if len(tok) >= 3}


def _safe_iterdir(start: Path) -> list[Path]:
    if not start.is_dir():
        return []
    try:
        return list(start.iterdir())
    except (PermissionError, OSError):
        return []


def _score_one_entry(
    entry: Path,
    missing_resolved: Path,
    target_tokens: set[str],
) -> Optional[tuple[int, Path]]:
    if not entry.is_file():
        return None
    if entry.resolve() == missing_resolved:
        return None
    overlap = len(target_tokens & _tokenize(entry.stem))
    if overlap == 0:
        return None
    return (overlap, entry)


def _score_dir_candidates(
    start: Path,
    missing: Path,
    target_tokens: set[str],
) -> list[tuple[int, Path]]:
    missing_resolved = missing.resolve()
    scored: list[tuple[int, Path]] = []
    for entry in _safe_iterdir(start):
        row = _score_one_entry(entry, missing_resolved, target_tokens)
        if row is not None:
            scored.append(row)
    return scored


def _relativize(entry: Path, project_root: Path) -> Optional[str]:
    try:
        rel = entry.resolve().relative_to(project_root.resolve())
    except ValueError:
        return None
    return _normalize_rel(str(rel))


def _find_possible_matches(missing: Path, project_root: Path) -> tuple[str, ...]:
    target_tokens = _tokenize(missing.stem)
    if not target_tokens:
        return ()
    parent = missing.parent
    grand = parent.parent if parent != parent.parent else parent
    scored: list[tuple[int, Path]] = []
    for start in (parent, grand):
        scored.extend(_score_dir_candidates(start, missing, target_tokens))
    scored.sort(key=lambda row: -row[0])
    seen: set[str] = set()
    unique: list[str] = []
    for _, entry in scored:
        key = _relativize(entry, project_root)
        if key is None or key in seen:
            continue
        seen.add(key)
        unique.append(key)
        if len(unique) >= 3:
            break
    return tuple(unique)


def _resolve_under_project_root(project_root: Path, relative_path: str) -> Optional[Path]:
    candidate = (project_root / relative_path).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return None
    return candidate


def _command_tokens(command: str) -> list[str]:
    if not command:
        return []
    try:
        return [item for item in shlex.split(command, posix=False) if _clean_text(item)]
    except ValueError:
        return [_clean_text(item) for item in command.split() if _clean_text(item)]


def _looks_like_absolute_path(token: str) -> bool:
    if _WINDOWS_ABS_PATH.match(token):
        return True
    # Avoid flags such as /k, /q on Windows shells.
    if token.startswith("/") and token.count("/") >= 2:
        return True
    return False


def _symbol_key(kind: str, name: str, class_name: str) -> str:
    if kind == "method":
        return f"{class_name}.{name}" if class_name else name
    return name


def _python_symbols(file_path: Path) -> tuple[set[str], set[str], set[str]]:
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(file_path))
    classes: set[str] = set()
    functions: set[str] = set()
    methods: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.add(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.add(node.name)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(f"{node.name}.{item.name}")
    return classes, functions, methods


def _is_deep_exempt(card: dict[str, Any]) -> bool:
    budget_tier = _clean_text(card.get("budget_tier")).lower()
    scout_report = dict(card.get("scout_report") or {})
    user_ack = bool(scout_report.get("user_acknowledged_partial_symbol_map"))
    do_not_change = _string_list(card.get("do_not_change"))
    return budget_tier == "deep" and user_ack and bool(do_not_change)


def _sha256_hex(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _line_count(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _validate_new_files_subset(
    *,
    files_to_change: list[str],
    new_files: list[str],
) -> list[FilePreflightIssue]:
    issues: list[FilePreflightIssue] = []
    for rel in new_files:
        if rel not in files_to_change:
            issues.append(
                FilePreflightIssue(
                    kind="new_files_not_subset",
                    path=rel,
                    detail="declared as new_files but not listed in files_to_change",
                )
            )
    return issues


def _validate_file_presence(
    *,
    files_to_change: list[str],
    new_files: list[str],
    root: Path,
    allow_existing_new_files: bool = False,
) -> tuple[list[FilePreflightIssue], list[FilePreflightIssue], dict[str, Path]]:
    issues: list[FilePreflightIssue] = []
    warnings: list[FilePreflightIssue] = []
    resolved: dict[str, Path] = {}
    allow_existing = allow_existing_new_files and _preexisting_new_file_acceptance_enabled()
    for rel in files_to_change:
        abs_path = _resolve_under_project_root(root, rel)
        if abs_path is None:
            issues.append(
                FilePreflightIssue(
                    kind="path_outside_project_root",
                    path=rel,
                    detail=(
                        "resolved path escapes project_root; use repository-relative paths "
                        "without '..' traversal"
                    ),
                )
            )
            continue
        resolved[_normalize_rel(rel)] = abs_path
        exists = abs_path.exists()
        if rel in new_files:
            if exists:
                if allow_existing:
                    warnings.append(
                        FilePreflightIssue(
                            kind="new_file_already_exists_reused",
                            path=rel,
                            detail=(
                                "declared as new_files but already exists; continuing because "
                                "pre-existing task state acceptance is enabled"
                            ),
                        )
                    )
                else:
                    issues.append(
                        FilePreflightIssue(
                            kind="new_file_already_exists",
                            path=rel,
                            detail="declared as new_files but already exists; remove from new_files or rename the target",
                        )
                    )
        elif not exists:
            matches = _find_possible_matches(abs_path, root)
            issues.append(
                FilePreflightIssue(
                    kind="missing_source",
                    path=rel,
                    possible_matches=matches,
                    detail=(
                        "declared in files_to_change but not present on disk; "
                        "list in new_files if executor is expected to create it"
                    ),
                )
            )
    return issues, warnings, resolved


def _preexisting_new_file_acceptance_enabled() -> bool:
    raw = os.environ.get(_DISABLE_PREEXISTING_NEW_FILES_ENV, "")
    return raw.strip().lower() not in {"1", "true", "yes", "on"}


def _validate_verify_cmd(card: dict[str, Any], root: Path) -> list[FilePreflightIssue]:
    command = _clean_text(card.get("verify_cmd"))
    if not command:
        return [
            FilePreflightIssue(
                kind="invalid_verify_cmd",
                path="verify_cmd",
                detail="verify_cmd must be non-empty for v1.1 task cards",
            )
        ]
    issues: list[FilePreflightIssue] = []
    for token in _command_tokens(command):
        stripped = token.strip("\"'")
        if not _looks_like_absolute_path(stripped):
            continue
        candidate = Path(stripped).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            issues.append(
                FilePreflightIssue(
                    kind="invalid_verify_cmd",
                    path="verify_cmd",
                    detail=f"verify_cmd references path outside project_root: {stripped}",
                )
            )
    return issues


def _validate_large_files_have_symbols(
    *,
    files_to_change: list[str],
    new_files: list[str],
    resolved: dict[str, Path],
    target_symbols: list[dict[str, Any]],
    deep_exempt: bool,
) -> tuple[list[FilePreflightIssue], list[FilePreflightIssue]]:
    symbol_files = {_normalize_rel(_clean_text(item.get("file"))) for item in target_symbols if _clean_text(item.get("file"))}
    issues: list[FilePreflightIssue] = []
    warnings: list[FilePreflightIssue] = []
    for rel in files_to_change:
        if rel in new_files:
            continue
        abs_path = resolved.get(_normalize_rel(rel))
        if abs_path is None or not abs_path.exists():
            continue
        if _line_count(abs_path) <= _LARGE_FILE_LINE_THRESHOLD:
            continue
        if _normalize_rel(rel) in symbol_files:
            continue
        if deep_exempt:
            warnings.append(
                FilePreflightIssue(
                    kind="large_file_symbol_map_deep_exempt",
                    path=rel,
                    detail=(
                        f"file exceeds {_LARGE_FILE_LINE_THRESHOLD} lines without target_symbols, "
                        "but deep-mode user acknowledgement allows continuation"
                    ),
                )
            )
            continue
        issues.append(
            FilePreflightIssue(
                kind="large_file_requires_target_symbols",
                path=rel,
                detail=(
                    f"file exceeds {_LARGE_FILE_LINE_THRESHOLD} lines and no target_symbols entry was provided"
                ),
            )
        )
    return issues, warnings


def _validate_symbol_resolution(
    *,
    symbols: list[dict[str, Any]],
    root: Path,
    issue_kind: str,
) -> list[FilePreflightIssue]:
    issues: list[FilePreflightIssue] = []
    for entry in symbols:
        file_rel = _normalize_rel(_clean_text(entry.get("file")))
        kind = _clean_text(entry.get("kind")).lower()
        name = _clean_text(entry.get("name"))
        class_name = _clean_text(entry.get("class"))
        label = _symbol_key(kind, name, class_name)
        if not (file_rel and kind and name):
            issues.append(
                FilePreflightIssue(
                    kind=issue_kind,
                    path=file_rel or "<missing-file>",
                    detail=f"invalid symbol entry: kind={kind or '<missing>'}, name={name or '<missing>'}",
                )
            )
            continue
        abs_path = _resolve_under_project_root(root, file_rel)
        if abs_path is None or not abs_path.exists():
            issues.append(
                FilePreflightIssue(
                    kind=issue_kind,
                    path=f"{file_rel}:{label}",
                    detail="symbol target file does not exist under project_root",
                )
            )
            continue
        if abs_path.suffix.lower() != ".py":
            # Non-Python AST check is out of scope for now.
            continue
        try:
            classes, functions, methods = _python_symbols(abs_path)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            issues.append(
                FilePreflightIssue(
                    kind=issue_kind,
                    path=f"{file_rel}:{label}",
                    detail=f"failed to parse python file for symbol lookup: {exc}",
                )
            )
            continue
        if kind == "class":
            if name in classes:
                continue
            possible = tuple(sorted(item for item in classes if name.lower() in item.lower())[:3])
        elif kind == "function":
            if name in functions:
                continue
            possible = tuple(sorted(item for item in functions if name.lower() in item.lower())[:3])
        elif kind == "method":
            key = f"{class_name}.{name}" if class_name else name
            if key in methods:
                continue
            if class_name:
                possible = tuple(sorted(item.split(".", 1)[1] for item in methods if item.startswith(f"{class_name}."))[:3])
            else:
                possible = tuple(sorted(item for item in methods if name.lower() in item.lower())[:3])
        else:
            issues.append(
                FilePreflightIssue(
                    kind=issue_kind,
                    path=f"{file_rel}:{label}",
                    detail=f"unsupported symbol kind: {kind}",
                )
            )
            continue
        issues.append(
            FilePreflightIssue(
                kind=issue_kind,
                path=f"{file_rel}:{label}",
                possible_matches=possible,
                detail="symbol not found",
            )
        )
    return issues


def _validate_freshness(card: dict[str, Any], root: Path) -> list[FilePreflightIssue]:
    freshness = dict(card.get("freshness") or {})
    hashes = _dict_list(freshness.get("source_file_hashes"))
    issues: list[FilePreflightIssue] = []
    for entry in hashes:
        rel = _normalize_rel(_clean_text(entry.get("path")))
        expected_sha = _clean_text(entry.get("sha256"))
        expected_line_count = entry.get("line_count")
        if not rel:
            continue
        abs_path = _resolve_under_project_root(root, rel)
        if abs_path is None or not abs_path.exists():
            issues.append(
                FilePreflightIssue(
                    kind="stale_task_card",
                    path=rel,
                    detail="freshness path no longer exists at HEAD",
                )
            )
            continue
        if expected_sha and _sha256_hex(abs_path) != expected_sha:
            issues.append(
                FilePreflightIssue(
                    kind="stale_task_card",
                    path=rel,
                    detail="freshness sha256 mismatch at HEAD",
                )
            )
            continue
        if isinstance(expected_line_count, int) and expected_line_count >= 0 and _line_count(abs_path) != expected_line_count:
            issues.append(
                FilePreflightIssue(
                    kind="stale_task_card",
                    path=rel,
                    detail="freshness line_count mismatch at HEAD",
                )
            )
    return issues


def _validate_mutation_authorization(card: dict[str, Any]) -> list[FilePreflightIssue]:
    behavior_ids = {
        _clean_text(item.get("id"))
        for item in _dict_list(card.get("behavior_changes"))
        if _clean_text(item.get("id"))
    }
    allowed_files = set(_string_list(card.get("files_to_change"))) | set(_string_list(card.get("related_existing_tests")))
    issues: list[FilePreflightIssue] = []
    for mutation in _dict_list(card.get("allowed_test_mutations")):
        file_rel = _clean_text(mutation.get("file"))
        behavior_change_id = _clean_text(mutation.get("behavior_change_id"))
        if behavior_change_id and behavior_change_id not in behavior_ids:
            issues.append(
                FilePreflightIssue(
                    kind="unauthorized_mutation",
                    path=file_rel or "<missing-file>",
                    detail=f"behavior_change_id not declared in behavior_changes: {behavior_change_id}",
                )
            )
        if file_rel and file_rel not in allowed_files:
            issues.append(
                FilePreflightIssue(
                    kind="unauthorized_mutation",
                    path=file_rel,
                    detail="mutation file must be in related_existing_tests or files_to_change",
                )
            )
    return issues


def _is_v1_1_card(card: dict[str, Any]) -> bool:
    return _clean_text(card.get("schema_version")) == _V1_1


def run_file_preflight(
    card: dict[str, Any],
    project_root: Path,
    *,
    allow_existing_new_files: bool = False,
) -> FilePreflightReport:
    """Validate task-card executability against the working tree."""
    if not preflight_enabled():
        return FilePreflightReport(
            blocked=False,
            skipped=True,
            skip_reason=f"{_ENV_VAR} is disabled",
        )

    files_to_change = _string_list(card.get("files_to_change"))
    new_files = _string_list(card.get("new_files"))
    root = project_root.resolve()
    issues: list[FilePreflightIssue] = []
    warnings: list[FilePreflightIssue] = []

    issues.extend(_validate_new_files_subset(files_to_change=files_to_change, new_files=new_files))
    file_issues, file_warnings, resolved = _validate_file_presence(
        files_to_change=files_to_change,
        new_files=new_files,
        root=root,
        allow_existing_new_files=allow_existing_new_files,
    )
    issues.extend(file_issues)
    warnings.extend(file_warnings)

    # v1 cards keep minimal preflight semantics for backward compatibility.
    if not _is_v1_1_card(card):
        return FilePreflightReport(blocked=bool(issues), issues=tuple(issues), warnings=tuple(warnings))

    issues.extend(_validate_verify_cmd(card, root))
    target_symbols = _dict_list(card.get("target_symbols"))
    read_only_symbols = _dict_list(card.get("read_only_symbols"))
    large_file_issues, large_file_warnings = _validate_large_files_have_symbols(
        files_to_change=files_to_change,
        new_files=new_files,
        resolved=resolved,
        target_symbols=target_symbols,
        deep_exempt=_is_deep_exempt(card),
    )
    issues.extend(large_file_issues)
    warnings.extend(large_file_warnings)
    issues.extend(_validate_symbol_resolution(symbols=target_symbols, root=root, issue_kind="symbol_not_found"))
    issues.extend(_validate_symbol_resolution(symbols=read_only_symbols, root=root, issue_kind="symbol_not_found"))
    issues.extend(_validate_freshness(card, root))
    issues.extend(_validate_mutation_authorization(card))
    return FilePreflightReport(blocked=bool(issues), issues=tuple(issues), warnings=tuple(warnings))


__all__ = [
    "FilePreflightIssue",
    "FilePreflightReport",
    "preflight_enabled",
    "run_file_preflight",
]

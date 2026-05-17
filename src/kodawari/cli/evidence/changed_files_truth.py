"""Shared changed-files truth helpers for kodawari commands."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Iterable

from jsonschema import Draft202012Validator

WORKTREE_BASELINE_SCHEMA_VERSION = "worktree.baseline.v1"
WORKTREE_BASELINE_FILENAME = ".worktree_baseline.json"
_RUNTIME_INTERNAL_PREFIXES: tuple[str, ...] = (
    ".workflow/",
    # Legacy location of the instincts store; keep filtering it during the
    # migration window so old worktrees do not surface it as a "user changed
    # file" in scope-drift checks.
    ".claude/memory/",
)

# Path segments that always belong to kodawari's own scratch under a
# planning directory. These must never show up as "user changed files" in
# scope-drift checks — they're the worktree isolation workspace and round
# artifacts the orchestrator writes itself.
_RUNTIME_INTERNAL_SEGMENTS: tuple[str, ...] = (
    ".parallel_workers",
)

_SCHEMA_CACHE: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_path(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def dedupe_paths(values: Iterable[Any]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = normalize_path(item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _path_within_project_root(project_root: Path, raw: Any) -> str:
    normalized = normalize_path(raw)
    if not normalized:
        return ""
    root = project_root.resolve()
    candidate_path = Path(normalized)
    if candidate_path.is_absolute():
        resolved = candidate_path.resolve(strict=False)
    else:
        resolved = (root / normalized).resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return ""
    return normalize_path(relative.as_posix())


def _is_runtime_internal_path(path: str) -> bool:
    normalized = normalize_path(path).lower()
    if not normalized:
        return False
    if any(normalized.startswith(prefix) for prefix in _RUNTIME_INTERNAL_PREFIXES):
        return True
    segments = normalized.split("/")
    return any(seg in _RUNTIME_INTERNAL_SEGMENTS for seg in segments)


def filter_runtime_internal_paths(values: Iterable[Any]) -> list[str]:
    return [item for item in dedupe_paths(values) if not _is_runtime_internal_path(item)]


def _planning_dir_prefix(project_root: Path, planning_dir: Path) -> str:
    """Return the project-root-relative prefix for *planning_dir* (always
    ending with ``/``), or an empty string if planning_dir is not within
    project_root."""
    try:
        rel = planning_dir.resolve().relative_to(project_root.resolve())
    except ValueError:
        return ""
    return normalize_path(rel.as_posix()).rstrip("/") + "/"


def filter_planning_dir_paths(project_root: Path, planning_dir: Path, values: Iterable[Any]) -> list[str]:
    """Drop any path that lives under *planning_dir* (kodawari's own
    state / round artifacts / task cards / worktree baseline). These are
    orchestrator-owned and must never appear in user-facing changed_files."""
    prefix = _planning_dir_prefix(project_root, planning_dir)
    if not prefix:
        return list(dedupe_paths(values))
    out: list[str] = []
    for item in dedupe_paths(values):
        if item.lower().startswith(prefix.lower()):
            continue
        out.append(item)
    return out


def filter_project_root_paths(project_root: Path, values: Iterable[Any]) -> list[str]:
    scoped: list[str] = []
    seen: set[str] = set()
    for item in values:
        normalized = _path_within_project_root(project_root, item)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        scoped.append(normalized)
    return scoped


def existing_paths(project_root: Path, values: Iterable[str]) -> list[str]:
    existing: list[str] = []
    root = project_root.resolve()
    for item in filter_project_root_paths(project_root, values):
        candidate = (root / item).resolve()
        if candidate.exists():
            existing.append(item)
    return existing


def _git_scope_prefix(project_root: Path) -> str:
    full_command = ["git", "-C", str(project_root.resolve()), "rev-parse", "--show-prefix"]
    try:
        result = subprocess.run(full_command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""
    return normalize_path(result.stdout.strip())


def _strip_git_scope_prefix(path: str, prefix: str) -> str:
    normalized = normalize_path(path)
    scoped_prefix = normalize_path(prefix)
    if not normalized:
        return ""
    if not scoped_prefix:
        return normalized
    if normalized == scoped_prefix.rstrip("/"):
        return ""
    if normalized.startswith(scoped_prefix):
        return normalize_path(normalized[len(scoped_prefix) :])
    return ""


def _git_lines(project_root: Path, command: list[str]) -> list[str]:
    full_command = ["git", "-C", str(project_root.resolve()), *command]
    try:
        result = subprocess.run(full_command, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    prefix = _git_scope_prefix(project_root)
    scoped_lines = [_strip_git_scope_prefix(line, prefix) for line in result.stdout.splitlines()]
    return filter_project_root_paths(project_root, scoped_lines)


def git_tracked_dirty_files(project_root: Path) -> list[str]:
    return _git_lines(project_root, ["diff", "--name-only", "--diff-filter=ACMR"])


def git_untracked_files(project_root: Path) -> list[str]:
    return _git_lines(project_root, ["ls-files", "--others", "--exclude-standard"])


def git_worktree_changed_files(project_root: Path) -> list[str]:
    return dedupe_paths([*git_tracked_dirty_files(project_root), *git_untracked_files(project_root)])


def git_base_branch_diff_files(project_root: Path, base_branch: str) -> list[str]:
    return _git_lines(
        project_root,
        ["diff", "--name-only", "--diff-filter=ACMR", f"{str(base_branch or 'main').strip() or 'main'}...HEAD"],
    )


def baseline_path(planning_dir: Path) -> Path:
    return planning_dir / WORKTREE_BASELINE_FILENAME


def _schema_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "schemas" / "observability"


def _load_schema(schema_name: str) -> dict[str, Any]:
    cached = _SCHEMA_CACHE.get(schema_name)
    if cached is not None:
        return cached
    path = _schema_dir() / f"{schema_name}.schema.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid schema document: {path}")
    _SCHEMA_CACHE[schema_name] = payload
    return payload


def validate_worktree_baseline(payload: dict[str, Any]) -> None:
    schema = _load_schema("worktree_baseline")
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    if errors:
        messages = []
        for error in errors:
            field = ".".join(str(part) for part in error.path) or "<root>"
            messages.append(f"{field}: {error.message}")
        raise ValueError(f"worktree baseline schema validation failed: {'; '.join(messages)}")


def load_worktree_baseline(planning_dir: Path) -> dict[str, Any] | None:
    path = baseline_path(planning_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        validate_worktree_baseline(payload)
    except ValueError:
        return None
    return payload


def _path_matches_scope(path: str, scope: str) -> bool:
    normalized = normalize_path(path)
    allowed = normalize_path(scope)
    if not normalized or not allowed:
        return False
    if normalized == allowed:
        return True
    if allowed.endswith("/"):
        return normalized.startswith(allowed)
    return normalized.startswith(allowed + "/")


def capture_worktree_baseline(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    command: str,
    mode: str,
    allowed_files: Iterable[str] | None = None,
) -> dict[str, Any]:
    planning_dir = planning_dir.resolve()
    tracked_dirty = git_tracked_dirty_files(project_root)
    untracked_files = git_untracked_files(project_root)
    dirty_files = dedupe_paths([*tracked_dirty, *untracked_files])
    allowed = dedupe_paths(allowed_files or [])
    core_dirty_files = [
        item
        for item in dirty_files
        if any(_path_matches_scope(item, scope) for scope in allowed)
    ]
    status = "PASS"
    if str(mode or "").strip().lower() == "fail" and core_dirty_files:
        status = "FAIL"
    elif dirty_files:
        status = "WARN"
    payload = {
        "schema_version": WORKTREE_BASELINE_SCHEMA_VERSION,
        "captured_at": _now_iso(),
        "feature": str(feature or planning_dir.name).strip() or planning_dir.name,
        "planning_dir": str(planning_dir),
        "command": str(command or "").strip(),
        "mode": str(mode or "warn").strip().lower() or "warn",
        "status": status,
        "dirty_files": dirty_files,
        "tracked_dirty_files": tracked_dirty,
        "untracked_files": untracked_files,
        "allowed_files": allowed,
        "core_dirty_files": core_dirty_files,
        "details": _baseline_details(status=status, dirty_files=dirty_files, core_dirty_files=core_dirty_files),
    }
    validate_worktree_baseline(payload)
    path = baseline_path(planning_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _baseline_details(*, status: str, dirty_files: list[str], core_dirty_files: list[str]) -> str:
    if status == "FAIL":
        return f"Core task files already dirty before run: {core_dirty_files}"
    if dirty_files:
        return f"Pre-existing dirty worktree files detected: {len(dirty_files)}"
    return "Worktree clean at baseline capture."


def resolve_task_delta_changed_files(
    *,
    project_root: Path,
    planning_dir: Path,
    fallback_candidates: Iterable[tuple[str, Iterable[str]]] = (),
    baseline_diagnostic_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[str], str]:
    def _filter(values: Iterable[Any]) -> list[str]:
        # Two-stage filter: drop .parallel_workers/.claude internal prefixes,
        # then drop anything inside the current planning_dir (kodawari's
        # own state / task cards / rounds log / compact context / baseline).
        stage1 = filter_runtime_internal_paths(filter_project_root_paths(project_root, values))
        return filter_planning_dir_paths(project_root, planning_dir, stage1)

    def _candidate_result(source: str, values: Iterable[Any]) -> tuple[list[str], str]:
        normalized = _filter(values)
        existing = existing_paths(project_root, normalized)
        if existing:
            return existing, f"{source}:existing"
        if normalized:
            return normalized, f"{source}:raw"
        return [], ""

    def _is_worktree_source(source: str) -> bool:
        normalized = str(source or "").strip().lower()
        return normalized in {"git", "git_worktree", "worktree", "baseline_delta", "baseline_delta:git_worktree"}

    chosen: list[str] = []
    chosen_source = ""
    worktree_candidates: list[tuple[str, Iterable[str]]] = []
    for source, values in fallback_candidates:
        source_text = str(source or "").strip() or "fallback"
        if _is_worktree_source(source_text):
            worktree_candidates.append((source_text, values))
            continue
        chosen, chosen_source = _candidate_result(source_text, values)
        if chosen:
            break

    baseline_delta: list[str] = []
    baseline = load_worktree_baseline(planning_dir)
    if baseline is not None:
        baseline_dirty = {item.lower() for item in _filter(baseline.get("dirty_files") or [])}
        current_dirty = _filter(git_worktree_changed_files(project_root))
        baseline_delta = [item for item in current_dirty if item.lower() not in baseline_dirty]

    if chosen:
        if baseline_delta and baseline_diagnostic_callback is not None:
            chosen_set = {item.lower() for item in chosen}
            baseline_set = {item.lower() for item in baseline_delta}
            extras = [item for item in baseline_delta if item.lower() not in chosen_set]
            missing = [item for item in chosen if item.lower() not in baseline_set]
            if extras or missing:
                baseline_diagnostic_callback(
                    {
                        "code": "baseline_delta_disagrees_with_executor",
                        "executor_changed_files": list(chosen),
                        "executor_changed_files_source": chosen_source,
                        "baseline_delta": list(baseline_delta),
                        "extras_in_baseline_only": extras,
                        "missing_in_baseline": missing,
                    }
                )
        return chosen, chosen_source

    if baseline_delta:
        return baseline_delta, "baseline_delta:git_worktree"

    for source, values in worktree_candidates:
        chosen, chosen_source = _candidate_result(source, values)
        if chosen:
            return chosen, chosen_source

    return [], "none"

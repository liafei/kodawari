"""Runtime state and guarded file tools for OpenAI-compatible execution."""

from __future__ import annotations

from dataclasses import dataclass, field
import functools
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest
from uuid import uuid4

from kodawari.autopilot.core.repo_path_guard import (
    DEFAULT_MAX_READ_BYTES,
    guard_repo_read_path,
    guard_repo_write_path,
)
from kodawari.autopilot.core.http_safety import RedirectBlocked, SafeRedirectHandler
from kodawari.autopilot.core.prompt_profiles import (
    render_learned_prompt_lesson_text,
    nudge_policy_for_model,
    render_prompt_profile_text,
)
from kodawari.autopilot.core.secret_redactor import redact_jsonable, redact_secret_text
from kodawari.autopilot.execution import tool_use_prompt as _tool_use_prompt
from kodawari.autopilot.execution import tool_use_result as _tool_use_result
from kodawari.autopilot.execution import tool_use_stall as _tool_use_stall
from kodawari.autopilot.execution import tool_use_patch_plan as _tool_use_patch_plan
from kodawari.autopilot.execution.tool_use_stall import StallDetector
from kodawari.autopilot.execution import tool_use_transport as _tool_use_transport
from kodawari.autopilot.execution.tool_use_common import (
    _cap,
    _changed_files_from_hashes,
    _copy_ignore,
    _dedupe_paths,
    _file_hash,
    _file_hashes,
    _is_test_path,
    _looks_like_repo_context_path,
    _looks_secret,
    _normalize_rel,
    _replacement_texts_for_content,
    _task_id_tokens,
    _text_count_with_line_ending_variants,
)
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError
from kodawari.autopilot.execution.verify_execution import VERIFY_TIMEOUT_SECONDS, maybe_execute_verify_command
from kodawari.infra.io_atomic import append_jsonl_atomic, atomic_write_canonical_json, atomic_write_json


OPENAI_TOOL_USE_BACKEND = "openai_tool_use"
FULL_FILE_PROTOCOL = _tool_use_prompt.FULL_FILE_PROTOCOL
EXACT_STR_REPLACE_PROTOCOL = _tool_use_prompt.EXACT_STR_REPLACE_PROTOCOL
TOOL_MANIFEST_VERSION = "execution.tool_manifest.v1"
TOOL_CALL_LOG_FILENAME = ".execution_tool_calls.jsonl"
PATCH_ATTEMPTS_FILENAME = ".execution_patch_attempts.jsonl"
READ_SCOPE_WIDEN_FILENAME = ".execution_read_scope_widen.jsonl"
STALL_REPORT_FILENAME = ".execution_stall_report.json"
INFEASIBILITY_REPORT_FILENAME = ".execution_infeasibility.json"
FULL_FILE_TOOL_MANIFEST_V1 = _tool_use_prompt.FULL_FILE_TOOL_MANIFEST_V1
PATCH_TOOL_MANIFEST_V1 = _tool_use_prompt.PATCH_TOOL_MANIFEST_V1
TOOL_MANIFEST_V2 = _tool_use_prompt.TOOL_MANIFEST_V2
_SKIP_TOP_LEVEL = {".git", ".venv", "node_modules", ".tox", ".workflow", ".workflow_runtime", ".android-studio", ".idea", ".gradle"}
_UNSUPPORTED_REQUEST_CAPABILITIES = {
    "shell.exec",
    "bash",
    "repo.move_file",
    "repo.rename_file",
    "file.move",
    "file.rename",
    "patch.emit",
    "patch.apply",
    "replace_in_file",
    "apply_patch",
}
_UNSUPPORTED_REQUEST_MARKERS = (
    "replace_in_file",
    "apply_patch",
    "search/replace",
    "search-replace",
    "patch protocol",
    "move_file",
    "rename_file",
    "move file",
    "rename file",
    "shell.exec",
    "run shell",
    "bash -c",
    "run bash",
    "use bash",
)


def _action_only_tool_names(execution_protocol: str) -> set[str]:
    if str(execution_protocol or "").strip().lower() == EXACT_STR_REPLACE_PROTOCOL:
        return {
            "list_allowed_files",
            "get_file_hash",
            "list_patch_plan",
            "apply_patch_plan_item",
            "str_replace",
            "write_new_file",
            "finish_execution",
            "declare_task_infeasible",
        }
    return {
        "list_allowed_files",
        "write_new_file",
        "delete_file",
        "finish_execution",
        "declare_task_infeasible",
    }


_RECOVERABLE_READ_SCOPE_ERRORS = {
    "PATH_OUT_OF_SCOPE",
    "PATH_GUARD_BLOCKED",
    "DIR_OUT_OF_SCOPE",
    "READ_FAILED",
}
_READ_ONLY_TOOLS = {"list_files_in_dir", "read_file", "read_file_partial", "get_file_hash", "search_file"}
_INTERNAL_TOOL_NAME_KEY = _tool_use_prompt.INTERNAL_TOOL_NAME_KEY
_STALL_ERROR_CODES = {
    "EXECUTOR_STALLED_BUDGET_PRESSURE",
    "EXECUTOR_STALLED_CONTEXT_OVERFLOW",
    "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
    "EXECUTOR_STALLED_PATCH_FAILURES",
    "EXECUTOR_STALLED_REDUNDANT_READS",
    "EXECUTOR_STALLED_REPEATED_SEARCH",
    "MAX_SAME_TOOL_CALLS_PER_PATH",
    "READ_SCOPE_EXHAUSTED",
}
_SAFE_ROOT_READ_NAMES = {
    "README.md",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "tox.ini",
    "package.json",
    "tsconfig.json",
}
_SAFE_CONTEXT_FILE_NAMES = {
    "conftest.py",
    "db.py",
    "database.py",
    "schema.py",
    "db_schema.py",
    "schemas.py",
}
_SAFE_CONTEXT_SUFFIXES = ("_schema.py", "_schemas.py", "_models.py", "_types.py")


def _tool_path_arg(arguments: dict[str, Any]) -> str:
    return str(arguments.get("path") or arguments.get("dir") or "").strip()


def _recoverable_tool_error(name: str, code: str) -> bool:
    return name in _READ_ONLY_TOOLS and code in _RECOVERABLE_READ_SCOPE_ERRORS


def _verify_command_runner() -> Callable[..., dict[str, Any]]:
    public_module = sys.modules.get("kodawari.autopilot.execution.execution_openai_tool_use")
    public_runner = getattr(public_module, "maybe_execute_verify_command", None) if public_module is not None else None
    if callable(public_runner) and public_runner is not maybe_execute_verify_command:
        return public_runner
    return maybe_execute_verify_command



@dataclass
class ToolUseRuntime:
    config: Any
    request_path: Path
    request_payload: dict[str, Any]
    project_root: Path
    planning_dir: Path
    allowed_files: list[str]
    read_only_files: list[str] = field(default_factory=list)
    run_id: str = field(default_factory=lambda: uuid4().hex)
    scratch_root: Path = field(init=False)
    workspace: Path = field(init=False)
    changed_paths: set[str] = field(default_factory=set)
    deleted_paths: set[str] = field(default_factory=set)
    tool_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    verify_failures: int = 0
    finish_seen: bool = False
    finished_summary: str = ""
    before_hashes: dict[str, str | None] = field(default_factory=dict)
    observed_hashes: set[str] = field(default_factory=set)
    read_progress_ends: dict[str, int] = field(default_factory=dict)
    read_progress_windows: set[str] = field(default_factory=set)
    read_progress_window_counts: dict[str, int] = field(default_factory=dict)
    read_progress_total_bytes: dict[str, int] = field(default_factory=dict)
    applied_patch_plan_items: set[str] = field(default_factory=set)
    read_scope_widenings: list[dict[str, Any]] = field(default_factory=list)
    read_scope_exhausted: bool = False
    action_only_mode: bool = False
    action_only_reason: str = ""
    action_only_error_code: str = ""
    action_only_checkpoint_attempts: int = 0
    stall_detector: StallDetector = field(init=False)
    read_cache: Any = field(init=False)

    def __post_init__(self) -> None:
        writable = set(self.allowed_files)
        self.read_only_files = [item for item in self.read_only_files if item not in writable]
        self.scratch_root = (self.project_root / ".workflow" / ".executor_scratch" / self.run_id).resolve()
        self.workspace = self.scratch_root / "workspace"
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self._clear_stale_stall_report()
        self._copy_project_snapshot()
        self.before_hashes = _file_hashes(self.project_root, self.allowed_files)
        self.stall_detector = StallDetector(self.config)
        # S4: lazy-import to avoid potential circular dependency.
        from kodawari.autopilot.execution.tool_use_read_cache import ReadCache
        self.read_cache = ReadCache()

    def tool_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": TOOL_MANIFEST_VERSION,
            "backend": OPENAI_TOOL_USE_BACKEND,
            "run_id": self.run_id,
            "execution_protocol": self.execution_protocol(),
            "tools": list(self.active_tools()),
            "allowed_files": list(self.allowed_files),
            "read_only_files": list(self.read_only_files),
            "readable_files": list(self.readable_files()),
            "read_scope_widen_budget": self._read_scope_widen_budget(),
            "read_scope_widenings": list(self.read_scope_widenings),
            "read_scope_exhausted": bool(self.read_scope_exhausted),
            "scope_mode": "inline_guard",
            "scratch_root": str(self.scratch_root),
        }

    def write_tool_manifest(self) -> None:
        atomic_write_json(self.planning_dir / ".execution_tool_manifest.json", self.tool_manifest())

    def readable_files(self) -> list[str]:
        return _dedupe_paths([*self.allowed_files, *self.read_only_files])

    def cleanup_success(self) -> None:
        shutil.rmtree(self.scratch_root, ignore_errors=True)

    def tool_log_path(self) -> Path:
        return self.planning_dir / TOOL_CALL_LOG_FILENAME

    def patch_log_path(self) -> Path:
        return self.planning_dir / PATCH_ATTEMPTS_FILENAME

    def read_scope_widen_log_path(self) -> Path:
        return self.planning_dir / READ_SCOPE_WIDEN_FILENAME

    def stall_report_path(self) -> Path:
        return self.planning_dir / STALL_REPORT_FILENAME

    def _clear_stale_stall_report(self) -> None:
        try:
            self.stall_report_path().unlink(missing_ok=True)
        except OSError:
            pass

    def write_stall_report(self, *, error_code: str, message: str, iteration: int) -> dict[str, Any]:
        detector = self.stall_detector
        detector.last_code = error_code or detector.last_code
        detector.last_message = message or detector.last_message
        payload = detector.snapshot(runtime=self, iteration=iteration, reason=error_code)
        atomic_write_canonical_json(self.stall_report_path(), redact_jsonable(payload))
        return payload

    def patch_plan_status(self) -> dict[str, Any]:
        items = self._patch_plan()
        ids = [str(item.get("id") or f"patch_{index + 1}") for index, item in enumerate(items)]
        applied = [item for item in ids if item in self.applied_patch_plan_items]
        return {
            "total": len(ids),
            "applied": applied,
            "remaining": [item for item in ids if item not in self.applied_patch_plan_items],
        }

    def execution_protocol(self) -> str:
        protocol = str(
            getattr(self.config, "execution_protocol", "")
            or self.request_payload.get("execution_protocol")
            or FULL_FILE_PROTOCOL
        ).strip().lower().replace("-", "_")
        if protocol not in {FULL_FILE_PROTOCOL, EXACT_STR_REPLACE_PROTOCOL}:
            return FULL_FILE_PROTOCOL
        return protocol

    def active_tools(self) -> list[str]:
        if self.execution_protocol() == EXACT_STR_REPLACE_PROTOCOL:
            base = list(PATCH_TOOL_MANIFEST_V1)
        else:
            base = list(FULL_FILE_TOOL_MANIFEST_V1)
        if self.action_only_mode:
            return [tool for tool in base if tool in _action_only_tool_names(self.execution_protocol())]
        return base

    def log_tool_call(
        self,
        *,
        iteration: int,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        append_jsonl_atomic(
            self.tool_log_path(),
            redact_jsonable(
                {
                    "schema_version": "execution.tool_call.v1",
                    "run_id": self.run_id,
                    "iteration": int(iteration),
                    "tool_call_id": str(tool_call_id or ""),
                    "tool": str(name or ""),
                    "arguments": _summarize_tool_payload(arguments),
                    "result": _summarize_tool_payload(result or {}),
                    "error_code": str(error_code or ""),
                    "error_message": redact_secret_text(error_message),
                    "timestamp": time.time(),
                }
            ),
        )

    def log_patch_attempt(
        self,
        *,
        path: str,
        old_text: str,
        new_text: str,
        precondition_sha256: str,
        expected_occurrences: int,
        actual_occurrences: int,
        status: str,
        error_code: str = "",
        error_message: str = "",
        before_sha256: str = "",
        after_sha256: str = "",
    ) -> None:
        old_bytes = old_text.encode("utf-8", errors="replace")
        new_bytes = new_text.encode("utf-8", errors="replace")
        append_jsonl_atomic(
            self.patch_log_path(),
            redact_jsonable(
                {
                    "schema_version": "execution.patch_attempt.v1",
                    "run_id": self.run_id,
                    "protocol": self.execution_protocol(),
                    "path": path,
                    "old_text_bytes": len(old_bytes),
                    "old_text_sha256": hashlib.sha256(old_bytes).hexdigest(),
                    "new_text_bytes": len(new_bytes),
                    "new_text_sha256": hashlib.sha256(new_bytes).hexdigest(),
                    "precondition_sha256": precondition_sha256,
                    "expected_occurrences": int(expected_occurrences),
                    "actual_occurrences": int(actual_occurrences),
                    "status": status,
                    "error_code": error_code,
                    "error_message": redact_secret_text(error_message),
                    "before_sha256": before_sha256,
                    "after_sha256": after_sha256,
                    "timestamp": time.time(),
                }
            ),
        )

    def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.active_tools():
            if self.action_only_mode:
                code = self.action_only_error_code or "EXECUTOR_STALLED_NO_WRITE_PROGRESS"
                raise OpenAIToolUseExecutionError(
                    code,
                    f"executor refused action-only checkpoint by requesting forbidden tool: {name}",
                )
            raise OpenAIToolUseExecutionError("TOOL_FORBIDDEN", f"tool is not allowed: {name}")
        path = _tool_path_arg(arguments)
        self._count_tool_call(name, path, arguments)
        if name == "finish_execution":
            self.finish_seen = True
            self.finished_summary = str(arguments.get("summary") or "OpenAI tool-use executor finished.").strip()
            return self._finish()
        if name == "declare_task_infeasible":
            return self._declare_task_infeasible(arguments)
        handler = self._tool_dispatch.get(name)
        if handler is None:
            raise OpenAIToolUseExecutionError("TOOL_FORBIDDEN", f"tool is not allowed: {name}")
        return handler(arguments)

    def _declare_task_infeasible(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """First-class infeasibility signal — the task cannot proceed without
        out-of-scope work (missing schema column / module / API). Persists a
        structured ``.execution_infeasibility.json`` artifact and raises so the
        outer flow maps it to a BLOCKED execution result with a dedicated
        error_code that the recovery registry routes to a stop-and-replan
        decision (see registry detector ``task_infeasibility``).
        """

        reason = str(arguments.get("infeasible_reason") or "").strip()
        missing = [
            str(item).strip()
            for item in list(arguments.get("missing_preconditions") or [])
            if str(item or "").strip()
        ]
        evidence = str(arguments.get("evidence") or "").strip()
        if not reason or not missing:
            raise OpenAIToolUseExecutionError(
                "INVALID_TOOL_CALL",
                "declare_task_infeasible requires a non-empty infeasible_reason and at least one missing precondition",
            )
        self.finish_seen = True
        self.finished_summary = reason
        payload = {
            "schema_version": "execution.infeasibility.v1",
            "run_id": self.run_id,
            "infeasible_reason": reason,
            "missing_preconditions": missing,
            "evidence": evidence,
        }
        atomic_write_canonical_json(
            self.planning_dir / INFEASIBILITY_REPORT_FILENAME,
            redact_jsonable(payload),
        )
        raise OpenAIToolUseExecutionError(
            "TASK_BLOCKED_BY_PRECONDITION",
            f"task infeasible: {reason} (missing: {', '.join(missing)})",
        )

    @functools.cached_property
    def _tool_dispatch(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        return {
            "list_allowed_files": lambda args: {
                "ok": True,
                "allowed_files": list(self.allowed_files),
                "read_only_files": list(self.read_only_files),
                "readable_files": list(self.readable_files()),
                "read_scope_widen_budget": self._read_scope_widen_budget(),
                "read_scope_widenings": list(self.read_scope_widenings),
            },
            "list_files_in_dir": lambda args: self._list_files_in_dir(_tool_path_arg(args)),
            "read_file": lambda args: self._read_file(
                _tool_path_arg(args),
                offset=0,
                limit=int(args.get("limit") or DEFAULT_MAX_READ_BYTES),
            ),
            "read_file_partial": lambda args: self._read_file(
                _tool_path_arg(args),
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 16_000),
            ),
            "get_file_hash": lambda args: self._get_file_hash(_tool_path_arg(args)),
            "list_patch_plan": lambda _args: self._list_patch_plan(),
            "apply_patch_plan_item": lambda args: self._apply_patch_plan_item(
                str(args.get("id") or args.get("patch_id") or ""),
                args.get("index"),
            ),
            "search_file": lambda args: self._search_file(
                path=_tool_path_arg(args),
                query=str(args.get("query") or ""),
                case_sensitive=bool(args.get("case_sensitive") or False),
                max_matches=int(args.get("max_matches") or 20),
                context_chars=int(args.get("context_chars") or 160),
            ),
            "str_replace": lambda args: self._str_replace(
                path=_tool_path_arg(args),
                old_text=str(args.get("old_text") or ""),
                new_text=str(args.get("new_text") or ""),
                precondition_sha256=str(args.get("precondition_sha256") or "").strip(),
                expected_occurrences=int(args.get("expected_occurrences") or 1),
            ),
            "write_new_file": lambda args: self._write_file(
                _tool_path_arg(args),
                str(args.get("content") or ""),
                require_missing=self.execution_protocol() == EXACT_STR_REPLACE_PROTOCOL,
            ),
            "delete_file": lambda args: self._delete_file(_tool_path_arg(args)),
            # P1-#3: complexity self-check. Reads the file in the scratch
            # workspace and returns per-function CC + nesting so the model
            # can verify its refactor before calling finish_execution.
            "check_complexity": lambda args: self._check_complexity(_tool_path_arg(args)),
        }

    def _copy_project_snapshot(self) -> None:
        base_workspace = self._recovery_base_workspace()
        if base_workspace is not None:
            shutil.copytree(base_workspace, self.workspace, symlinks=True, dirs_exist_ok=True, ignore=_copy_ignore)
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        planning = self.planning_dir.resolve()
        for source in self.project_root.iterdir():
            if source.name in _SKIP_TOP_LEVEL:
                continue
            if source.resolve() == planning or planning.is_relative_to(source.resolve()):
                continue
            if _looks_secret(source.name) or source.is_symlink():
                continue
            target = self.workspace / source.name
            if source.is_dir():
                shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True, ignore=_copy_ignore)
            elif source.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    def _recovery_base_workspace(self) -> Path | None:
        task_card = self.request_payload.get("task_card")
        if not isinstance(task_card, dict):
            return None
        recovery = task_card.get("recovery")
        if not isinstance(recovery, dict):
            return None
        raw = str(recovery.get("base_workspace_path") or "").strip()
        if not raw:
            return None
        try:
            candidate = Path(raw).resolve()
            scratch_root = (self.project_root / ".workflow" / ".executor_scratch").resolve()
        except OSError:
            return None
        if not candidate.exists() or not candidate.is_dir():
            return None
        if candidate.name != "workspace" or not candidate.is_relative_to(scratch_root):
            return None
        return candidate

    def _count_tool_call(self, name: str, path: str, arguments: dict[str, Any]) -> None:
        if not path:
            return
        normalized = _normalize_rel(path)
        key_path = normalized
        if name == "read_file_partial":
            key_path = f"{normalized}:{int(arguments.get('offset') or 0)}:{int(arguments.get('limit') or 0)}"
        if name == "search_file":
            query = str(arguments.get("query") or "")
            key_path = f"{normalized}:{hashlib.sha256(query.encode('utf-8', errors='replace')).hexdigest()}"
        key = (name, key_path)
        self.tool_counts[key] = self.tool_counts.get(key, 0) + 1
        # P1-#5: bumped 5→10. The earlier value blocked legitimate iterative
        # refinement of one file (multi-pass str_replace on a complex function
        # routinely hits 6-9 calls). 10 still catches actual loops while
        # giving refactor tasks room to converge. Project models.yaml can
        # override via runtime_caps.max_same_tool_calls_per_path.
        max_calls = _cap(self.config, "max_same_tool_calls_per_path", 10)
        if self.tool_counts[key] > max_calls:
            self.stall_detector.record_tool_call_limit(
                tool=name,
                path=normalized,
                count=self.tool_counts[key],
            )
            raise OpenAIToolUseExecutionError(
                "MAX_SAME_TOOL_CALLS_PER_PATH",
                f"{name} called too many times for {normalized}",
            )

    def _require_allowed(self, path: str, *, write: bool = False) -> str:
        normalized = _normalize_rel(path)
        in_scope = set(self.allowed_files if write else self.readable_files())
        if normalized not in in_scope:
            if not write and self._try_widen_read_scope(normalized):
                return normalized
            if not write and self.read_scope_exhausted:
                raise OpenAIToolUseExecutionError(
                    "READ_SCOPE_EXHAUSTED",
                    f"read scope widening budget exhausted before allowing: {normalized}",
                )
            detail = "files_to_change" if write else "readable task scope"
            raise OpenAIToolUseExecutionError("PATH_OUT_OF_SCOPE", f"path is not in {detail}: {normalized}")
        guard = guard_repo_write_path(project_root=self.project_root, path=normalized) if write else guard_repo_read_path(project_root=self.project_root, path=normalized, require_file=False)
        if not guard.allowed:
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", f"{normalized}: {guard.reason}")
        return normalized

    def _read_scope_widen_budget(self) -> int:
        caps = getattr(self.config, "runtime_caps", None)
        if isinstance(caps, dict) and "max_read_scope_widenings" in caps:
            try:
                return max(0, int(caps.get("max_read_scope_widenings")))
            except (TypeError, ValueError):
                return 20
        return 20

    def _try_widen_read_scope(self, normalized: str) -> bool:
        reason = self._read_scope_widen_reason(normalized)
        if not reason:
            return False
        if len(self.read_scope_widenings) >= self._read_scope_widen_budget():
            self.read_scope_exhausted = True
            return False
        guard = guard_repo_read_path(project_root=self.project_root, path=normalized, require_file=True)
        if not guard.allowed:
            return False
        self.read_only_files = _dedupe_paths([*self.read_only_files, normalized])
        event = {
            "schema_version": "execution.read_scope_widen.v1",
            "run_id": self.run_id,
            "path": normalized,
            "reason": reason,
            "budget_used": len(self.read_scope_widenings) + 1,
            "budget_limit": self._read_scope_widen_budget(),
            "timestamp": time.time(),
        }
        self.read_scope_widenings.append(event)
        append_jsonl_atomic(self.read_scope_widen_log_path(), redact_jsonable(event))
        return True

    def _read_scope_widen_reason(self, normalized: str) -> str:
        path = Path(normalized)
        if not normalized or normalized in self.allowed_files or normalized in self.read_only_files:
            return ""
        if _looks_secret(path.name) or not _looks_like_repo_context_path(normalized):
            return ""
        target = self.project_root / normalized
        if not target.exists() or not target.is_file():
            return ""
        parent = str(path.parent).replace("\\", "/")
        if parent == "":
            parent = "."
        writable_parents = {str(Path(item).parent).replace("\\", "/") or "." for item in self.allowed_files}
        if parent in writable_parents:
            return "same_writable_parent"
        if parent == "." and path.name in _SAFE_ROOT_READ_NAMES:
            return "safe_root_context"
        if path.name == "conftest.py" and self._path_is_test_context_parent(normalized):
            return "test_conftest_context"
        if path.name in _SAFE_CONTEXT_FILE_NAMES or path.name.endswith(_SAFE_CONTEXT_SUFFIXES):
            return "safe_context_file"
        if self._path_matches_task_test_context(normalized):
            return "related_test_context"
        return ""

    def _path_is_test_context_parent(self, normalized: str) -> bool:
        parent = str(Path(normalized).parent).replace("\\", "/")
        if not parent:
            return False
        prefix = "" if parent == "." else f"{parent}/"
        return any(item.startswith(prefix) for item in self.allowed_files if _is_test_path(item))

    def _path_matches_task_test_context(self, normalized: str) -> bool:
        if not _is_test_path(normalized):
            return False
        path = Path(normalized)
        task_ids = _task_id_tokens(self.request_payload)
        if task_ids and any(token.lower() in path.name.lower() for token in task_ids):
            return True
        test_parents = {str(Path(item).parent).replace("\\", "/") or "." for item in self.allowed_files if _is_test_path(item)}
        parent = str(path.parent).replace("\\", "/") or "."
        return parent in test_parents

    def _list_files_in_dir(self, path: str) -> dict[str, Any]:
        normalized = _normalize_rel(path or ".")
        guard = guard_repo_read_path(project_root=self.project_root, path=normalized, require_file=False)
        if not guard.allowed:
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", f"{normalized}: {guard.reason}")
        allowed_parents = {str(Path(item).parent).replace("\\", "/") or "." for item in self.readable_files()}
        if normalized not in allowed_parents and normalized != ".":
            raise OpenAIToolUseExecutionError("DIR_OUT_OF_SCOPE", f"directory is not an allowed file parent: {normalized}")
        target = (self.workspace / normalized).resolve()
        if not target.is_relative_to(self.workspace.resolve()) or not target.exists() or not target.is_dir():
            return {"ok": True, "files": []}
        files = [
            child.name + ("/" if child.is_dir() else "")
            for child in sorted(target.iterdir(), key=lambda item: item.name.lower())
            if not _looks_secret(child.name)
        ][:200]
        return {"ok": True, "files": files}

    def _read_file(self, path: str, *, offset: int, limit: int) -> dict[str, Any]:
        normalized = self._require_allowed(path, write=False)
        target = (self.workspace / normalized).resolve()
        if not target.is_relative_to(self.workspace.resolve()):
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", "path resolves outside scratch workspace")
        if normalized in self.deleted_paths or not target.exists():
            return {"ok": False, "error": "file does not exist in scratch workspace"}
        if not target.is_file():
            return {"ok": False, "error": "path is not a file"}
        start = max(0, int(offset))
        # Change 4: first read of a small file (≤24K bytes ≈ 400 LOC) returns
        # the full file instead of a partial slice. Eliminates the "I should
        # partial-read more to see the rest" loop pattern for typical service
        # source files. Threshold aligns with max_full_read_tool_result_bytes
        # so the compactor doesn't immediately stub the result on next round.
        size_bytes = target.stat().st_size
        auto_expanded = False
        if (
            normalized not in self.read_cache.ranges
            and start == 0
            and int(limit) <= 0
            and size_bytes <= 24_000
        ):
            max_len = max(1, min(size_bytes or 1, DEFAULT_MAX_READ_BYTES))
            auto_expanded = True
        else:
            max_len = max(1, min(int(limit) or DEFAULT_MAX_READ_BYTES, DEFAULT_MAX_READ_BYTES))
        # Change 2: ReadCache check happens BEFORE the actual read. The runtime
        # still returns real content (cheap), but tags cache hits so the stall
        # detector can count "wasted" re-reads and tighten the no-write window.
        decision = self.read_cache.check(self.workspace, normalized, start, max_len)
        try:
            with target.open("rb") as handle:
                handle.seek(start)
                chunk = handle.read(max_len)
        except OSError as exc:
            raise OpenAIToolUseExecutionError("READ_FAILED", str(exc)) from exc
        content = chunk.decode("utf-8", errors="replace")
        result: dict[str, Any] = {
            "ok": True,
            "path": normalized,
            "offset": start,
            "content": content,
            "content_bytes": len(chunk),
            "truncated": start + len(chunk) < size_bytes,
            "size_bytes": size_bytes,
        }
        if auto_expanded:
            result["instruction"] = (
                f"Full file returned ({len(chunk)} bytes, ≤24K cap). "
                "DO NOT request partial reads of this file; the full content is above."
            )
        elif decision.is_hit:
            result["_workflow_cache_hit"] = True
            result["_cache_hit_overlap"] = decision.overlap_ratio
            result["instruction"] = (
                f"You already read {normalized} bytes {decision.cached_start}-{decision.cached_end}. "
                "This range is re-served from disk only because you re-requested it. "
                "Refer to your prior context — do not re-read this range."
            )
        self.read_cache.record(self.workspace, normalized, start, max_len)
        return result

    def _get_file_hash(self, path: str) -> dict[str, Any]:
        normalized = self._require_allowed(path, write=False)
        target = (self.workspace / normalized).resolve()
        if not target.is_relative_to(self.workspace.resolve()):
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", "path resolves outside scratch workspace")
        if normalized in self.deleted_paths or not target.exists() or not target.is_file():
            return {"ok": False, "path": normalized, "sha256": None, "error": "file does not exist"}
        return {
            "ok": True,
            "path": normalized,
            "sha256": _file_hash(target),
            "size_bytes": target.stat().st_size,
        }

    def _patch_plan(self) -> list[dict[str, Any]]:
        return _tool_use_patch_plan.patch_plan(self)

    def _list_patch_plan(self) -> dict[str, Any]:
        return _tool_use_patch_plan.list_patch_plan(self)

    def _resolve_patch_item(
        self,
        plan: list[dict[str, Any]],
        plan_id: str,
        raw_index: Any,
    ) -> tuple[dict[str, Any], int]:
        return _tool_use_patch_plan.resolve_patch_item(plan, plan_id, raw_index)

    def _apply_patch_plan_item(self, plan_id: str, raw_index: Any) -> dict[str, Any]:
        return _tool_use_patch_plan.apply_patch_plan_item(self, plan_id, raw_index)

    def _apply_patch_item_operation(
        self,
        item: dict[str, Any],
        operation: str,
        path: str,
    ) -> dict[str, Any]:
        return _tool_use_patch_plan.apply_patch_item_operation(self, item, operation, path)

    def _apply_str_replace_patch_item(self, item: dict[str, Any], path: str) -> dict[str, Any]:
        return _tool_use_patch_plan.apply_str_replace_patch_item(self, item, path)

    def _apply_write_patch_item(self, item: dict[str, Any], path: str) -> dict[str, Any]:
        return _tool_use_patch_plan.apply_write_patch_item(self, item, path)

    def apply_recovery_patch_plan(self) -> dict[str, Any]:
        return _tool_use_patch_plan.apply_recovery_patch_plan(self)

    def _search_file(
        self,
        path: str,
        *,
        query: str,
        case_sensitive: bool,
        max_matches: int,
        context_chars: int,
    ) -> dict[str, Any]:
        normalized = self._require_allowed(path, write=False)
        needle = str(query or "")
        if not needle:
            raise OpenAIToolUseExecutionError("SEARCH_QUERY_EMPTY", "query must be non-empty")
        target = (self.workspace / normalized).resolve()
        if not target.is_relative_to(self.workspace.resolve()):
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", "path resolves outside scratch workspace")
        if normalized in self.deleted_paths or not target.exists():
            return {"ok": False, "path": normalized, "error": "file does not exist in scratch workspace"}
        if not target.is_file():
            return {"ok": False, "path": normalized, "error": "path is not a file"}
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise OpenAIToolUseExecutionError("SEARCH_NON_UTF8", "search_file supports utf-8 text files only") from exc
        haystack = content if case_sensitive else content.lower()
        needle_cmp = needle if case_sensitive else needle.lower()
        max_items = max(1, min(int(max_matches or 20), 50))
        context = max(40, min(int(context_chars or 160), 500))
        matches: list[dict[str, Any]] = []
        start = 0
        while len(matches) < max_items:
            index = haystack.find(needle_cmp, start)
            if index < 0:
                break
            excerpt_start = max(0, index - context)
            excerpt_end = min(len(content), index + len(needle) + context)
            line_number = content.count("\n", 0, index) + 1
            matches.append(
                {
                    "offset": index,
                    "line": line_number,
                    "excerpt": content[excerpt_start:excerpt_end],
                }
            )
            start = index + max(1, len(needle_cmp))
        return {
            "ok": True,
            "path": normalized,
            "query": needle,
            "case_sensitive": bool(case_sensitive),
            "match_count_returned": len(matches),
            "truncated": len(matches) >= max_items and haystack.find(needle_cmp, start) >= 0,
            "matches": matches,
            "size_bytes": target.stat().st_size,
            "sha256": _file_hash(target),
        }

    def _str_replace(
        self,
        *,
        path: str,
        old_text: str,
        new_text: str,
        precondition_sha256: str,
        expected_occurrences: int,
    ) -> dict[str, Any]:
        normalized = self._require_allowed(path, write=True)
        # Invalidate ReadCache on attempt (not success): a partially-applied
        # str_replace can still mutate the file, so serving cached pre-write
        # bytes after this point would be stale.
        self.read_cache.invalidate(normalized)
        expected = max(1, int(expected_occurrences or 1))
        target = (self.workspace / normalized).resolve()
        if not target.is_relative_to(self.workspace.resolve()):
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", "path resolves outside scratch workspace")
        if not target.exists() or not target.is_file():
            return self._patch_failed(
                normalized,
                old_text,
                new_text,
                precondition_sha256,
                expected,
                0,
                "PATCH_TARGET_MISSING",
                "file does not exist in scratch workspace",
                "",
            )
        if not old_text:
            before_hash = _file_hash(target) or ""
            return self._patch_failed(
                normalized,
                old_text,
                new_text,
                precondition_sha256,
                expected,
                0,
                "PATCH_OLD_TEXT_EMPTY",
                "old_text must be non-empty",
                before_hash,
            )
        before_hash = _file_hash(target) or ""
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._patch_failed(
                normalized,
                old_text,
                new_text,
                precondition_sha256,
                expected,
                0,
                "PATCH_NON_UTF8",
                "str_replace supports utf-8 text files only",
                before_hash,
            )
        actual, effective_old_text, effective_new_text = _replacement_texts_for_content(
            content,
            old_text=old_text,
            new_text=new_text,
            expected=expected,
        )
        precondition_mismatch_ignored = False
        if precondition_sha256 and precondition_sha256 != before_hash:
            new_count = _text_count_with_line_ending_variants(content, new_text) if new_text else 0
            if actual == 0 and new_count >= expected:
                self.log_patch_attempt(
                    path=normalized,
                    old_text=old_text,
                    new_text=new_text,
                    precondition_sha256=precondition_sha256,
                    expected_occurrences=expected,
                    actual_occurrences=actual,
                    status="ALREADY_APPLIED",
                    error_code="PATCH_ALREADY_APPLIED",
                    error_message="patch appears already applied",
                    before_sha256=before_hash,
                    after_sha256=before_hash,
                )
                return {"ok": True, "path": normalized, "changed": False, "already_applied": True}
            if actual != expected:
                return self._patch_failed(
                    normalized,
                    old_text,
                    new_text,
                    precondition_sha256,
                    expected,
                    actual,
                    "PATCH_PRECONDITION_MISMATCH",
                    "file sha256 does not match precondition_sha256",
                    before_hash,
                )
            precondition_mismatch_ignored = True
        if actual != expected:
            return self._patch_failed(
                normalized,
                old_text,
                new_text,
                precondition_sha256,
                expected,
                actual,
                "PATCH_OCCURRENCE_MISMATCH",
                f"old_text matched {actual} times; expected {expected}",
                before_hash,
            )
        updated = content.replace(effective_old_text, effective_new_text, expected)
        target.write_text(updated, encoding="utf-8")
        after_hash = _file_hash(target) or ""
        if before_hash != after_hash:
            self.changed_paths.add(normalized)
        self.deleted_paths.discard(normalized)
        self.log_patch_attempt(
            path=normalized,
            old_text=old_text,
            new_text=new_text,
            precondition_sha256=precondition_sha256,
            expected_occurrences=expected,
            actual_occurrences=actual,
            status="PASS",
            before_sha256=before_hash,
            after_sha256=after_hash,
        )
        return {
            "ok": True,
            "path": normalized,
            "changed": before_hash != after_hash,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "occurrences_replaced": actual,
            "precondition_mismatch_ignored": precondition_mismatch_ignored,
        }

    def _patch_failed(
        self,
        path: str,
        old_text: str,
        new_text: str,
        precondition_sha256: str,
        expected_occurrences: int,
        actual_occurrences: int,
        error_code: str,
        error_message: str,
        before_sha256: str,
    ) -> dict[str, Any]:
        self.log_patch_attempt(
            path=path,
            old_text=old_text,
            new_text=new_text,
            precondition_sha256=precondition_sha256,
            expected_occurrences=expected_occurrences,
            actual_occurrences=actual_occurrences,
            status="FAILED",
            error_code=error_code,
            error_message=error_message,
            before_sha256=before_sha256,
            after_sha256=before_sha256,
        )
        return {
            "ok": False,
            "status": "PATCH_FAILED",
            "path": path,
            "error_code": error_code,
            "error": error_message,
            "actual_occurrences": int(actual_occurrences),
            "expected_occurrences": int(expected_occurrences),
            "current_sha256": before_sha256,
        }

    def _write_file(self, path: str, content: str, *, require_missing: bool = False) -> dict[str, Any]:
        normalized = self._require_allowed(path, write=True)
        self.read_cache.invalidate(normalized)  # invalidate on attempt
        target = (self.workspace / normalized).resolve()
        if not target.is_relative_to(self.workspace.resolve()):
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", "path resolves outside scratch workspace")
        if require_missing and target.exists():
            return {
                "ok": False,
                "status": "TOOL_ERROR",
                "path": normalized,
                "error_code": "WRITE_NEW_FILE_EXISTS",
                "error": "write_new_file may only create missing files under exact_str_replace_v1; use str_replace for existing files",
                "sha256": _file_hash(target),
            }
        previous_hash = _file_hash(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        new_hash = _file_hash(target)
        if previous_hash != new_hash:
            self.changed_paths.add(normalized)
        self.deleted_paths.discard(normalized)
        return {"ok": True, "path": normalized, "changed": previous_hash != new_hash}

    def _delete_file(self, path: str) -> dict[str, Any]:
        normalized = self._require_allowed(path, write=True)
        self.read_cache.invalidate(normalized)  # invalidate on attempt
        target = (self.workspace / normalized).resolve()
        if not target.is_relative_to(self.workspace.resolve()):
            raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", "path resolves outside scratch workspace")
        if target.exists() and target.is_file():
            target.unlink()
            self.changed_paths.add(normalized)
        self.deleted_paths.add(normalized)
        return {"ok": True, "path": normalized, "deleted": True}

    def _check_complexity(self, path: str) -> dict[str, Any]:
        """Compute per-function cyclomatic complexity + nesting for a Python file.

        P1-#3: gives the executor a way to verify its own refactor BEFORE
        finish_execution. Non-Python paths return ok=False/NOT_APPLICABLE so
        the model isn't tempted to spam the tool on docs / yaml.

        Uses the AST counter shipped in kodawari's gate engine; matches
        the BLOCK thresholds in ``code_redline.standard.REDLINE`` (CC > 10
        and nesting > 4).
        """
        try:
            normalized = self._require_allowed(path, write=False)
        except OpenAIToolUseExecutionError as exc:
            return {"ok": False, "status": "TOOL_ERROR", "error_code": exc.code, "error": exc.message}
        if not normalized.endswith(".py"):
            return {
                "ok": False,
                "status": "TOOL_ERROR",
                "error_code": "NOT_APPLICABLE",
                "error": f"check_complexity only supports Python files; got {normalized}",
            }
        target = (self.workspace / normalized).resolve()
        if not target.exists():
            return {
                "ok": False,
                "status": "TOOL_ERROR",
                "error_code": "FILE_NOT_FOUND",
                "error": f"file does not exist in scratch workspace: {normalized}",
            }
        try:
            source = target.read_text(encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "status": "TOOL_ERROR", "error_code": "READ_ERROR", "error": str(exc)}
        import ast

        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return {
                "ok": False,
                "status": "TOOL_ERROR",
                "error_code": "SYNTAX_ERROR",
                "error": f"file did not parse: {exc.msg} at line {exc.lineno}",
            }

        def cc(node: ast.AST) -> int:
            score = 1
            for child in ast.walk(node):
                if isinstance(
                    child,
                    (
                        ast.If,
                        ast.For,
                        ast.AsyncFor,
                        ast.While,
                        ast.Try,
                        ast.With,
                        ast.AsyncWith,
                        ast.ExceptHandler,
                        ast.BoolOp,
                        ast.comprehension,
                        ast.Match,
                    ),
                ):
                    score += 1
                elif isinstance(child, ast.IfExp):
                    score += 1
            return score

        def max_nest(node: ast.AST, depth: int = 0) -> int:
            best = depth
            for child in ast.iter_child_nodes(node):
                is_block = isinstance(
                    child,
                    (
                        ast.If,
                        ast.For,
                        ast.AsyncFor,
                        ast.While,
                        ast.Try,
                        ast.With,
                        ast.AsyncWith,
                        ast.ExceptHandler,
                    ),
                )
                best = max(best, max_nest(child, depth + 1 if is_block else depth))
            return best

        cc_limit = int(_cap(self.config, "complexity_block", 10))
        nest_limit = int(_cap(self.config, "nesting_max", 4))
        functions: list[dict[str, Any]] = []
        violations: list[dict[str, Any]] = []
        for fn_node in ast.walk(tree):
            if not isinstance(fn_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            score = cc(fn_node)
            nest = max_nest(fn_node)
            entry = {
                "name": fn_node.name,
                "line": fn_node.lineno,
                "complexity": score,
                "nesting": nest,
            }
            functions.append(entry)
            if score > cc_limit or nest > nest_limit:
                violations.append(
                    {
                        **entry,
                        "complexity_limit": cc_limit,
                        "nesting_limit": nest_limit,
                    }
                )
        return {
            "ok": True,
            "path": normalized,
            "functions": functions,
            "violations": violations,
            "complexity_limit": cc_limit,
            "nesting_limit": nest_limit,
        }

    def _finish(self) -> dict[str, Any]:
        changed = _changed_files_from_hashes(self.workspace, self.allowed_files, self.before_hashes)
        if not changed:
            return {"ok": False, "status": "VERIFY_FAILED", "error": "no deterministic changed files"}
        verify = self._run_verify(changed)
        if verify and not bool(verify.get("passed")):
            self.verify_failures += 1
            if self.verify_failures <= _cap(self.config, "max_verify_retries", 2):
                return {
                    "ok": False,
                    "status": "VERIFY_FAILED_RETRYABLE",
                    "verify_summary": redact_jsonable(verify),
                    "remaining_verify_retries": _cap(self.config, "max_verify_retries", 2) - self.verify_failures,
                }
            raise OpenAIToolUseExecutionError(
                "VERIFY_FAILED",
                str(verify.get("blocking_reason") or verify.get("summary") or "verify failed"),
            )
        self._commit()
        committed = _changed_files_from_hashes(self.project_root, self.allowed_files, self.before_hashes)
        if not committed:
            raise OpenAIToolUseExecutionError("CHANGED_FILES_MISSING", "finish_execution produced no project changes")
        return {
            "ok": True,
            "status": "FINISHED",
            "changed_files": committed,
            "verify_summary": redact_jsonable(verify or {"status": "SKIPPED", "passed": True}),
        }

    def _run_verify(self, changed_files: list[str]) -> dict[str, Any] | None:
        verify_cmd = str(self.request_payload.get("verify_cmd") or "").strip()
        if not verify_cmd:
            return None
        return _verify_command_runner()(
            project_root=self.workspace,
            feature=str(self.request_payload.get("feature") or ""),
            task_label=str(self.request_payload.get("task") or ""),
            verify_cmd=verify_cmd,
            changed_files=changed_files,
            timeout_seconds=_cap(self.config, "verify_timeout_seconds", VERIFY_TIMEOUT_SECONDS),
        )

    def _commit(self) -> None:
        backups = self.scratch_root / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        touched = sorted(set(self.allowed_files) & (self.changed_paths | self.deleted_paths | set(_changed_files_from_hashes(self.workspace, self.allowed_files, self.before_hashes))))
        originally_missing: set[str] = set()
        try:
            for rel in touched:
                source = (self.workspace / rel).resolve()
                target = (self.project_root / rel).resolve()
                if not target.is_relative_to(self.project_root.resolve()):
                    raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", f"commit target outside project: {rel}")
                if source.exists() and not source.is_relative_to(self.workspace.resolve()):
                    raise OpenAIToolUseExecutionError("PATH_GUARD_BLOCKED", f"commit source outside scratch workspace: {rel}")
                if _file_hash(target) != self.before_hashes.get(rel):
                    raise OpenAIToolUseExecutionError(
                        "PROJECT_CHANGED_DURING_EXECUTION",
                        f"project file changed while executor was running: {rel}",
                    )
                backup = backups / rel
                if target.exists() and target.is_file():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, backup)
                else:
                    originally_missing.add(rel)
                if source.exists() and source.is_file():
                    staging = self.scratch_root / "staging" / rel
                    staging.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, staging)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(staging, target)
                elif target.exists() and target.is_file():
                    target.unlink()
        except Exception:
            for rel in touched:
                target = self.project_root / rel
                backup = backups / rel
                if backup.exists() and backup.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(backup, target)
                elif rel in originally_missing and target.exists() and target.is_file():
                    target.unlink()
            raise

def _summarize_tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return _tool_use_result.summarize_tool_payload(payload)


__all__ = ["ToolUseRuntime"]

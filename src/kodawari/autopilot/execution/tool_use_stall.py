"""Stall detection helpers for the tool-use executor."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.secret_redactor import redact_jsonable, redact_secret_text
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError

TOOL_CALL_LOG_FILENAME = ".execution_tool_calls.jsonl"
PATCH_ATTEMPTS_FILENAME = ".execution_patch_attempts.jsonl"


@dataclass
class StallDetector:
    config: Any
    read_signatures: dict[str, int] = field(default_factory=dict)
    search_signatures: dict[str, int] = field(default_factory=dict)
    patch_apply_failures: int = 0
    last_write_iteration: int = 0
    last_observation_iteration: int = 0
    budget_pressure: bool = False
    budget_pressure_iteration: int = 0
    token_spend_reported: int = 0
    token_spend_estimated: int = 0
    # OpenAI-compatible prompt cache stats; cumulative across iterations (per-call, not max).
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    last_code: str = ""
    last_message: str = ""
    tool_call_limit: dict[str, Any] = field(default_factory=dict)
    fragmented_read_paths: dict[str, int] = field(default_factory=dict)
    # Counts reads served from disk that the ReadCache flagged as already-read.
    # Increments via record_tool_result when result["_workflow_cache_hit"] is True.
    # Triggers EXECUTOR_STALLED_REDUNDANT_READS at max_wasted_reads (default 8 —
    # bumped from 3 in P1.5 after observing that fresh from-scratch
    # implementations legitimately re-read 5-8 files within one task while the
    # model is composing a multi-file contract). The original cap of 3 fired
    # on healthy refactors before the model could write its first edit.
    # Tightens the no-write threshold via enforce_no_write_progress (Change 6).
    wasted_read_count: int = 0

    def record_token_spend(self, *, reported: int, estimated: int, iteration: int) -> bool:
        self.token_spend_reported = max(self.token_spend_reported, int(reported or 0))
        self.token_spend_estimated = max(self.token_spend_estimated, int(estimated or 0))
        soft_budget = _cap(self.config, "max_token_budget", 200_000)
        if max(self.token_spend_reported, self.token_spend_estimated) <= soft_budget:
            return False
        if not self.budget_pressure:
            self.budget_pressure = True
            self.budget_pressure_iteration = int(iteration)
            return True
        return False

    def record_prompt_cache(self, *, hit: int, miss: int) -> None:
        """Sum-accumulate prompt cache hit/miss tokens reported by the provider."""
        self.prompt_cache_hit_tokens += max(0, int(hit or 0))
        self.prompt_cache_miss_tokens += max(0, int(miss or 0))

    def enforce_hard_budget(self) -> None:
        soft_budget = _cap(self.config, "max_token_budget", 200_000)
        hard_budget = _cap(self.config, "max_hard_token_budget", max(soft_budget * 5, soft_budget + 1))
        if max(self.token_spend_reported, self.token_spend_estimated) > hard_budget:
            self._raise(
                "EXECUTOR_STALLED_BUDGET_PRESSURE",
                "openai_tool_use executor exceeded hard token budget under budget pressure",
            )

    def record_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        normalized = str(arguments.get("path") or "").replace("\\", "/")
        if name in {"read_file", "read_file_partial", "get_file_hash"}:
            offset = int(arguments.get("offset") or 0)
            # Bucket reads by 200-line windows: adjacent offsets map to the same
            # bucket so a +1-byte shift cannot bypass the redundancy counter.
            # Plain offset:limit signature was vulnerable to a sliding-window
            # attack where the LLM walked offset across the file without ever
            # tripping max_redundant_read_count.
            bucket = (name, normalized, offset // 200)
            signature = f"{bucket[0]}:{bucket[1]}:bkt{bucket[2]}"
            count = self._increment(self.read_signatures, signature)
            if count > _cap(self.config, "max_redundant_read_count", 8):
                self._raise(
                    "EXECUTOR_STALLED_REDUNDANT_READS",
                    f"executor repeated the same read pattern too many times: {name} {normalized}",
                )
        if name == "search_file":
            query = str(arguments.get("query") or "")
            query_hash = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()
            signature = f"{normalized}:{query_hash}"
            count = self._increment(self.search_signatures, signature)
            if count > _cap(self.config, "max_repeated_search_count", 6):
                self._raise(
                    "EXECUTOR_STALLED_REPEATED_SEARCH",
                    f"executor repeated the same search query too many times: {normalized}",
                )

    def record_tool_result(self, name: str, result: dict[str, Any]) -> None:
        status = str(result.get("status") or result.get("error_code") or "").strip().upper()
        if name in {"str_replace", "apply_patch_plan_item"} and status in {"PATCH_FAILED", "TOOL_ERROR"}:
            self.patch_apply_failures += 1
            if self.patch_apply_failures > _cap(self.config, "max_patch_apply_failures", 3):
                self._raise("EXECUTOR_STALLED_PATCH_FAILURES", "executor repeatedly failed to apply patch operations")
        # Change 2: ReadCache flags re-reads of already-served ranges. Each such
        # "wasted" read increments a counter that both (a) trips its own stall
        # code at max_wasted_reads, and (b) tightens the no-write threshold
        # (Change 6) so deterministic recovery picks up sooner.
        if name in {"read_file", "read_file_partial"} and bool(result.get("_workflow_cache_hit")):
            self.wasted_read_count += 1
            if self.wasted_read_count > _cap(self.config, "max_wasted_reads", 8):
                self._raise(
                    "EXECUTOR_STALLED_REDUNDANT_READS",
                    f"executor served {self.wasted_read_count} cache-hit reads without writing",
                )

    def record_write_progress(self, iteration: int) -> None:
        self.last_write_iteration = int(iteration)

    def record_observation_progress(self, iteration: int) -> None:
        self.last_observation_iteration = int(iteration)

    def record_fragmented_read(self, *, path: str, window_count: int) -> None:
        """Track per-path window count so enforce_read_discipline can fire."""

        normalized = str(path or "").replace("\\", "/").strip()
        if not normalized:
            return
        self.fragmented_read_paths[normalized] = max(int(window_count or 0), self.fragmented_read_paths.get(normalized, 0))

    def enforce_read_discipline(self) -> None:
        """Hard-stop when one path has been chopped into too many tiny windows.

        Triggered AFTER ``_read_progress`` has already declared the path
        saturated (≥70% coverage or ≥1.5× re-read). Without this guard the
        model could keep producing new windows that no longer count as
        observation_progress, but are still cheap individually — the no-write
        timer would catch it eventually, just expensively. This stops it sooner.
        """

        # P1-#7: bumped 8→12. Refactoring a complex 300-500 line file with
        # multiple thinking passes legitimately needs 8-10 read windows; the
        # old cap fired on healthy refactor flows. 12 still catches actual
        # fragmented-read pathologies.
        cap = _cap(self.config, "max_read_windows_per_path", 12)
        offenders = [(path, count) for path, count in self.fragmented_read_paths.items() if int(count) > cap]
        if not offenders:
            return
        path, count = max(offenders, key=lambda item: item[1])
        self._raise(
            "EXECUTOR_STALLED_FRAGMENTED_READS",
            f"executor split {path} into {count} read windows without writing — "
            "consolidate into a single read or proceed to patch_plan/str_replace",
        )

    def record_tool_call_limit(self, *, tool: str, path: str, count: int) -> None:
        self.tool_call_limit = {
            "tool": str(tool or "").strip(),
            "path": str(path or "").strip().replace("\\", "/"),
            "count": int(count or 0),
        }

    def enforce_no_write_progress(self, iteration: int) -> None:
        threshold = _cap(self.config, "max_no_write_iterations", 12)
        if self.budget_pressure:
            threshold = min(threshold, _cap(self.config, "max_no_write_iterations_under_budget_pressure", 2))
        elif int(self.last_write_iteration or 0) <= 0 and self._recent_observation_progress(iteration):
            # Bumped down from 20 → 4 in the read-discipline pass: 20 iterations
            # of "look at things" was enough rope for sliding-window read theatrics
            # to burn the full 200k token budget before this guard ever fired.
            threshold = max(threshold, _cap(self.config, "max_no_write_iterations_with_observation", 4))
        # NOTE: v3.1 Change 6 (collapse threshold on wasted_read_count > 0) was
        # rolled back during implementation: it collided with NO_PROGRESS_ABORTED
        # and EXECUTOR_STALLED_PATCH_PLAN_REQUIRED tests that rely on the model
        # being allowed to spend its full no-write budget before stall fires.
        # The wasted-read accountancy still triggers EXECUTOR_STALLED_REDUNDANT_READS
        # at max_wasted_reads (default 8 — bumped from 3 in P1.5) via record_tool_result, which gives
        # the desired "give up sooner once cache hits appear" behaviour without
        # competing against the existing NO_WRITE / PATCH_PLAN_REQUIRED ladder.
        if int(iteration) - int(self.last_write_iteration) > threshold:
            code = "EXECUTOR_STALLED_BUDGET_PRESSURE" if self.budget_pressure else "EXECUTOR_STALLED_NO_WRITE_PROGRESS"
            self._raise(code, "executor made no write/patch/finish progress within the configured stall window")

    def _recent_observation_progress(self, iteration: int) -> bool:
        if int(self.last_observation_iteration or 0) <= int(self.last_write_iteration or 0):
            return False
        grace = max(0, _cap(self.config, "max_no_write_observation_grace_iterations", 2))
        return int(iteration) - int(self.last_observation_iteration or 0) <= grace

    def snapshot(self, *, runtime: Any, iteration: int, reason: str) -> dict[str, Any]:
        reported = int(self.token_spend_reported or 0)
        estimated = int(self.token_spend_estimated or 0)
        payload = {
            "schema_version": "execution.stall_report.v1",
            "run_id": runtime.run_id,
            "task_id": str(runtime.request_payload.get("task_id") or runtime.request_payload.get("task") or ""),
            "reason": str(reason or self.last_code or "EXECUTOR_STALLED"),
            "error_code": str(self.last_code or reason or "EXECUTOR_STALLED"),
            "error_message": redact_secret_text(self.last_message),
            "budget_pressure": bool(self.budget_pressure),
            "token_spend_reported": reported,
            "token_spend_estimated": estimated,
            "token_spend_effective": max(reported, estimated),
            "prompt_cache_hit_tokens": int(self.prompt_cache_hit_tokens or 0),
            "prompt_cache_miss_tokens": int(self.prompt_cache_miss_tokens or 0),
            "iterations": int(iteration),
            "counters": {
                "redundant_read_count": max(self.read_signatures.values(), default=0),
                "repeated_search_count": max(self.search_signatures.values(), default=0),
                "patch_apply_failures": int(self.patch_apply_failures),
                "no_write_iterations": max(0, int(iteration) - int(self.last_write_iteration)),
                "last_observation_iteration": int(self.last_observation_iteration or 0),
                "read_scope_widenings": len(list(getattr(runtime, "read_scope_widenings", []) or [])),
            },
            "read_scope_exhausted": bool(getattr(runtime, "read_scope_exhausted", False)),
            "read_scope_widenings": list(getattr(runtime, "read_scope_widenings", []) or []),
            "patch_plan": runtime.patch_plan_status(),
            "recent_tool_calls": recent_tool_calls(runtime.tool_log_path(), limit=20, run_id=runtime.run_id),
            "artifacts": [TOOL_CALL_LOG_FILENAME, PATCH_ATTEMPTS_FILENAME],
        }
        if bool(getattr(runtime, "action_only_mode", False)) or int(getattr(runtime, "action_only_checkpoint_attempts", 0) or 0) > 0:
            payload["decision_checkpoint"] = {
                "mode": "action_only",
                "attempts": int(getattr(runtime, "action_only_checkpoint_attempts", 0) or 0),
                "reason": redact_secret_text(str(getattr(runtime, "action_only_reason", "") or "")),
                "error_code": str(getattr(runtime, "action_only_error_code", "") or ""),
            }
        if self.tool_call_limit:
            payload["tool_call_limit"] = dict(self.tool_call_limit)
        if self.fragmented_read_paths:
            payload["fragmented_read_paths"] = dict(self.fragmented_read_paths)
        return payload

    @staticmethod
    def _increment(target: dict[str, int], key: str) -> int:
        target[key] = int(target.get(key, 0) or 0) + 1
        return target[key]

    def _raise(self, code: str, message: str) -> None:
        self.last_code = code
        self.last_message = message
        raise OpenAIToolUseExecutionError(code, message)


def recent_tool_calls(path: Path, *, limit: int, run_id: str = "") -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    items: list[dict[str, Any]] = []
    max_items = max(1, int(limit or 1))
    wanted_run_id = str(run_id or "").strip()
    for line in reversed(lines):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            if wanted_run_id and str(payload.get("run_id") or "").strip() != wanted_run_id:
                continue
            items.append(redact_jsonable(payload))
            if len(items) >= max_items:
                break
    return list(reversed(items))


def _cap(config: Any, key: str, default: int) -> int:
    caps = getattr(config, "runtime_caps", None)
    if isinstance(caps, dict):
        try:
            return int(caps.get(key) or default)
        except (TypeError, ValueError):
            return default
    return default

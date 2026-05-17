"""Patch-plan helpers for guarded tool-use execution."""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.execution import tool_use_prompt as _tool_use_prompt
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError


EXACT_STR_REPLACE_PROTOCOL = _tool_use_prompt.EXACT_STR_REPLACE_PROTOCOL


def patch_plan(runtime: Any) -> list[dict[str, Any]]:
    task_card = runtime.request_payload.get("task_card")
    if not isinstance(task_card, dict):
        return []
    items = task_card.get("patch_plan")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def list_patch_plan(runtime: Any) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, item in enumerate(runtime._patch_plan()):
        plan_id = str(item.get("id") or f"patch_{index + 1}")
        operation = str(item.get("operation") or item.get("op") or "str_replace")
        path = str(item.get("path") or "")
        items.append(
            {
                "id": plan_id,
                "index": index,
                "operation": operation,
                "path": path,
                "old_text_bytes": len(str(item.get("old_text") or "").encode("utf-8", errors="replace")),
                "new_text_bytes": len(str(item.get("new_text") or item.get("content") or "").encode("utf-8", errors="replace")),
                "expected_occurrences": int(item.get("expected_occurrences") or 1),
                "applied": plan_id in runtime.applied_patch_plan_items,
            }
        )
    return {"ok": True, "patch_plan": items, "count": len(items)}


def resolve_patch_item(
    plan: list[dict[str, Any]],
    plan_id: str,
    raw_index: Any,
) -> tuple[dict[str, Any], int]:
    if str(plan_id or "").strip():
        wanted = str(plan_id).strip()
        for index, candidate in enumerate(plan):
            if str(candidate.get("id") or f"patch_{index + 1}") == wanted:
                return candidate, index
    if raw_index is not None:
        try:
            candidate_index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise OpenAIToolUseExecutionError("PATCH_PLAN_ITEM_INVALID", "patch plan index must be an integer") from exc
        if 0 <= candidate_index < len(plan):
            return plan[candidate_index], candidate_index
    raise OpenAIToolUseExecutionError("PATCH_PLAN_ITEM_MISSING", "patch plan item not found")


def apply_patch_plan_item(runtime: Any, plan_id: str, raw_index: Any) -> dict[str, Any]:
    plan = runtime._patch_plan()
    if not plan:
        raise OpenAIToolUseExecutionError("PATCH_PLAN_MISSING", "task_card.patch_plan is missing")
    item, resolved_index = runtime._resolve_patch_item(plan, plan_id, raw_index)
    item_id = str(item.get("id") or f"patch_{resolved_index + 1}")
    if item_id in runtime.applied_patch_plan_items:
        return {"ok": True, "id": item_id, "already_applied": True}
    operation = str(item.get("operation") or item.get("op") or "str_replace").strip().lower()
    path = str(item.get("path") or "")
    # Invalidate ReadCache on attempt — a partial patch can still mutate the file
    if path and hasattr(runtime, "read_cache"):
        runtime.read_cache.invalidate(path.replace("\\", "/"))
    result = apply_patch_item_operation(runtime, item, operation, path)
    if bool(result.get("ok")):
        runtime.applied_patch_plan_items.add(item_id)
    result = dict(result)
    result["id"] = item_id
    result["operation"] = operation
    return result


def apply_patch_item_operation(
    runtime: Any,
    item: dict[str, Any],
    operation: str,
    path: str,
) -> dict[str, Any]:
    if operation in {"str_replace", "replace"}:
        return apply_str_replace_patch_item(runtime, item, path)
    if operation in {"write_new_file", "write_file"}:
        return apply_write_patch_item(runtime, item, path)
    if operation == "delete_file":
        return runtime._delete_file(path)
    raise OpenAIToolUseExecutionError("PATCH_PLAN_OPERATION_UNSUPPORTED", f"unsupported patch plan operation: {operation}")


def apply_str_replace_patch_item(runtime: Any, item: dict[str, Any], path: str) -> dict[str, Any]:
    return runtime._str_replace(
        path=path,
        old_text=str(item.get("old_text") or ""),
        new_text=str(item.get("new_text") or ""),
        precondition_sha256=str(item.get("precondition_sha256") or ""),
        expected_occurrences=int(item.get("expected_occurrences") or 1),
    )


def apply_write_patch_item(runtime: Any, item: dict[str, Any], path: str) -> dict[str, Any]:
    return runtime._write_file(
        path,
        str(item.get("content") or item.get("new_text") or ""),
        require_missing=runtime.execution_protocol() == EXACT_STR_REPLACE_PROTOCOL,
    )


def apply_recovery_patch_plan(runtime: Any) -> dict[str, Any]:
    plan = runtime._patch_plan()
    if not plan:
        return {"ok": True, "applied": [], "count": 0}
    applied: list[str] = []
    failures: list[dict[str, Any]] = []
    for index, item in enumerate(plan):
        item_id = str(item.get("id") or f"patch_{index + 1}")
        arguments = {"id": item_id, "index": index, "auto_apply": True}
        try:
            result = runtime._apply_patch_plan_item(item_id, index)
        except OpenAIToolUseExecutionError as exc:
            runtime.log_tool_call(
                iteration=0,
                tool_call_id=f"runtime_patch_{index + 1}",
                name="apply_patch_plan_item",
                arguments=arguments,
                error_code=exc.code,
                error_message=exc.message,
            )
            raise
        runtime.log_tool_call(
            iteration=0,
            tool_call_id=f"runtime_patch_{index + 1}",
            name="apply_patch_plan_item",
            arguments=arguments,
            result=result,
        )
        if not bool(result.get("ok")):
            failures.append(
                {
                    "id": item_id,
                    "path": str(item.get("path") or ""),
                    "error_code": str(result.get("error_code") or result.get("status") or "PATCH_PLAN_APPLY_FAILED"),
                    "error": str(result.get("error") or result.get("message") or "runtime failed to apply recovery patch plan item"),
                }
            )
            continue
        applied.append(item_id)
    return {"ok": not failures, "applied": applied, "failed": failures, "count": len(applied)}


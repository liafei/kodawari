"""Hook event helpers for the recovered autopilot engine."""

from __future__ import annotations

import time
from typing import Any, Callable

from kodawari.autopilot.engine.hook_lifecycle import build_lifecycle_event


def _build_hook_payload(
    *,
    event: str,
    task_id: str,
    task_label: str,
    task_scope: str | None,
    action_name: str | None,
    role_name: str | None,
    cycle: int,
    hook_index: int,
    details: dict[str, Any] | None,
) -> dict[str, Any]:
    payload = build_lifecycle_event(
        event=event,
        task_id=task_id,
        task_label=task_label,
        scope=str(task_scope or ""),
        cycle=cycle,
        action=action_name,
        role=role_name,
        details=details,
    )
    payload["hook_index"] = int(hook_index)
    payload["event_sequence_key"] = f"{task_id}:{int(hook_index):03d}:{event}"
    payload["timestamp"] = time.time()
    return payload


def _merge_hook_callback_response(
    payload: dict[str, Any],
    adapter_response: Any,
) -> None:
    if not isinstance(adapter_response, dict):
        return
    if "status" in adapter_response:
        payload["status"] = str(adapter_response["status"])
    adapter_details = adapter_response.get("details")
    if isinstance(adapter_details, dict):
        existing = dict(payload.get("adapter_details", {}))
        existing.update(adapter_details)
        payload["adapter_details"] = existing


def emit_hook_event(
    hook_events: list[dict[str, Any]],
    *,
    hook_events_enabled: bool,
    adapter: Any,
    build_context: Callable[[str, str | None], dict[str, Any]],
    event: str,
    task_id: str,
    task_label: str,
    task_scope: str | None,
    action_name: str | None,
    role_name: str | None,
    cycle: int,
    details: dict[str, Any] | None,
) -> None:
    if not hook_events_enabled:
        return

    payload = _build_hook_payload(
        event=event,
        task_id=task_id,
        task_label=task_label,
        task_scope=task_scope,
        action_name=action_name,
        role_name=role_name,
        cycle=cycle,
        hook_index=len(hook_events) + 1,
        details=details,
    )
    callback = getattr(adapter, "on_hook_event", None)
    if callable(callback):
        try:
            adapter_response = callback(
                event=event,
                task=task_label,
                context=build_context(task_label, task_scope),
                payload=dict(payload),
            )
            _merge_hook_callback_response(payload, adapter_response)
        except Exception as exc:
            payload["status"] = "error"
            payload["error"] = str(exc)
    hook_events.append(payload)


__all__ = ["emit_hook_event"]


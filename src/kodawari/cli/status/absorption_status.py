"""Shared absorption status snapshot for CLI/report outputs."""

from __future__ import annotations


ABSORPTION_STATUS_SNAPSHOT: dict[str, dict[str, str]] = {
    "planning_summary": {
        "status": "absorbed",
        "status_judgment": "已吸收",
        "level": "helper",
        "note": "summarize_plan helper is restored; artifact generation remains an explicit parallel capability.",
    },
    "context_compact": {
        "status": "partial",
        "status_judgment": "部分吸收",
        "level": "runtime_and_compat",
        "note": "Runtime trigger exists in autopilot loop; kodawari compact remains compatibility shim.",
    },
    "instincts": {
        "status": "partial",
        "status_judgment": "部分吸收",
        "level": "minimal_engine_and_compact_link",
        "note": "Minimal learn/list/select + store semantics restored; not fully wired into all orchestration decisions.",
    },
}


def absorption_status_snapshot() -> dict[str, dict[str, str]]:
    return {name: dict(payload) for name, payload in ABSORPTION_STATUS_SNAPSHOT.items()}

"""Selection and artifact loading helpers for automation stability reports."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import re
from typing import Any

from kodawari.cli.runtime.runtime_metrics import count_peer_review_rounds
from kodawari.cli.delivery.workflow_chain import bind_effective_gate_result, load_workflow_chain_snapshot


TASK_HEADING_RE = re.compile(r"^#{2,6}\s*(T\d+)\s*:\s*(.+?)\s*$")
TASK_CHECKLIST_HEADING_RE = re.compile(r"^- \[( |x|X)\]\s*(T\d+)\s*:\s*(.+?)\s*$")
STATE_TIMESTAMP_KEYS = ("updated_at", "started_at")


def serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def resolve_cli_selection(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "run_ids": list(getattr(args, "run_id", []) or []),
        "explicit_planning_dirs": list(getattr(args, "planning_dir", []) or []),
        "all_runs": bool(getattr(args, "all_runs", False)),
        "updated_since": parse_datetime_filter(getattr(args, "updated_since", None), end_of_day=False),
        "updated_until": parse_datetime_filter(getattr(args, "updated_until", None), end_of_day=True),
    }


def selection_payload(selection: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_ids": [str(item) for item in selection["run_ids"]],
        "planning_dirs": [str(Path(item).resolve()) for item in selection["explicit_planning_dirs"]],
        "all_runs": bool(selection["all_runs"]),
        "updated_since": serialize_datetime(selection["updated_since"]),
        "updated_until": serialize_datetime(selection["updated_until"]),
    }


def build_command_output_payload(
    *,
    project_root: Path,
    runs: list[dict[str, Any]],
    warnings: list[str],
    output_path: Path,
    selection: dict[str, Any],
    planning_dirs: list[Path],
    report_data: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "ok",
        "total_runs": len(runs),
        "skipped_runs": len(warnings),
        "output_path": str(output_path),
        "run_ids": [item["run_id"] for item in runs],
        "warnings": warnings,
        "project_root": str(project_root),
        "selection": selection_payload(selection),
        "resolved_planning_dirs": [str(path.resolve()) for path in planning_dirs],
        "round_outcome_counts": dict(report_data.get("round_outcome_counts", {})),
        "run_outcome_counts": dict(report_data.get("run_outcome_counts", {})),
        "root_cause_bucket_counts": dict(report_data.get("root_cause_bucket_counts", {})),
        "top_root_causes": list(report_data.get("top_root_causes", [])),
        "provenance": provenance,
    }


def build_report_options(
    args: argparse.Namespace,
    warnings: list[str],
    *,
    project_root: Path,
    planning_dirs: list[Path],
) -> dict[str, Any]:
    return {
        "task_max_cycles": getattr(args, "task_max_cycles", None),
        "task_auto_runs": getattr(args, "task_auto_runs", None),
        "timeout_per_round": getattr(args, "timeout_per_round", None),
        "token_budget_target": getattr(args, "token_budget_target", None),
        "warnings": warnings,
        "project_root": str(project_root),
        "resolved_planning_dirs": [str(path.resolve()) for path in planning_dirs],
    }


def resolve_report_output_path(project_root: Path, output: str | None) -> Path:
    if output:
        candidate = Path(output)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        return candidate.resolve()
    return (project_root / "AUTOMATION_STABILITY_REPORT.md").resolve()


def load_run_summaries(planning_dirs: list[Path]) -> tuple[list[dict[str, Any]], list[str]]:
    runs: list[dict[str, Any]] = []
    warnings: list[str] = []
    for path in planning_dirs:
        try:
            runs.append(load_run_summary(path))
        except (FileNotFoundError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            warnings.append(f"{path.name}: skipped invalid artifacts ({type(exc).__name__})")
    return runs, warnings


def _try_parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_datetime_filter(raw: str | None, *, end_of_day: bool) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    parsed_date = _try_parse_date(text)
    if parsed_date is not None:
        clock = time.max if end_of_day else time.min
        return datetime.combine(parsed_date, clock, tzinfo=timezone.utc)
    normalized = text.replace("Z", "+00:00")
    return _ensure_utc(datetime.fromisoformat(normalized))


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"required file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in: {path}")
    return data


def _json_dict_from_line(raw: str) -> dict[str, Any] | None:
    line = raw.strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _load_rounds(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        payload = _json_dict_from_line(raw)
        if payload is not None:
            rows.append(payload)
    return rows


def _load_optional_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return _load_json_dict(path)
    except (FileNotFoundError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _load_compact_context(planning_dir: Path) -> dict[str, Any] | None:
    return _load_optional_json_dict(planning_dir / "compact_context.json")


def _load_semantic_compact(planning_dir: Path) -> dict[str, Any] | None:
    return _load_optional_json_dict(planning_dir / "semantic_compact.json")


def _load_tasks(path: Path) -> list[str]:
    if not path.exists():
        return []
    tasks: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        heading = TASK_HEADING_RE.match(line)
        if heading:
            tasks.append(f"{heading.group(1).upper()}: {heading.group(2).strip()}")
            continue
        checklist = TASK_CHECKLIST_HEADING_RE.match(line)
        if checklist:
            tasks.append(f"{checklist.group(2).upper()}: {checklist.group(3).strip()}")
    return tasks


def _mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def _state_timestamp_from_payload(state: dict[str, Any]) -> datetime | None:
    for key in STATE_TIMESTAMP_KEYS:
        if state.get(key):
            return parse_datetime_filter(str(state[key]), end_of_day=False)
    return None


def _planning_dir_timestamp(planning_dir: Path) -> datetime | None:
    state_path = planning_dir / ".autopilot_state.json"
    if state_path.exists():
        parsed = _read_state_timestamp(state_path)
        if parsed is not None:
            return parsed
        return _mtime_utc(state_path)
    rounds_path = planning_dir / ".autopilot_rounds.jsonl"
    if rounds_path.exists():
        return _mtime_utc(rounds_path)
    return None


def _read_state_timestamp(state_path: Path) -> datetime | None:
    try:
        state = _load_json_dict(state_path)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return _state_timestamp_from_payload(state)


def _is_outside_window(
    observed: datetime,
    *,
    updated_since: datetime | None,
    updated_until: datetime | None,
) -> bool:
    if updated_since is not None and observed < updated_since:
        return True
    if updated_until is not None and observed > updated_until:
        return True
    return False


def _is_in_window(
    candidate: Path,
    *,
    updated_since: datetime | None,
    updated_until: datetime | None,
) -> bool:
    observed = _planning_dir_timestamp(candidate)
    if observed is None:
        return updated_since is None and updated_until is None
    return not _is_outside_window(
        observed,
        updated_since=updated_since,
        updated_until=updated_until,
    )


def _add_unique_candidate(
    *,
    candidate: Path,
    resolved: list[Path],
    seen: set[str],
    updated_since: datetime | None,
    updated_until: datetime | None,
) -> None:
    key = str(candidate).lower()
    if key in seen:
        return
    if not _is_in_window(candidate, updated_since=updated_since, updated_until=updated_until):
        return
    seen.add(key)
    resolved.append(candidate)


def _resolve_run_id_candidate(project_root: Path, run_id: str) -> Path:
    candidate = (project_root / "planning" / str(run_id)).resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"planning dir not found for run id: {candidate}")
    return candidate


def _resolve_explicit_planning_candidate(raw: str) -> Path:
    candidate = Path(raw).resolve()
    if not candidate.exists():
        raise FileNotFoundError(f"planning dir not found: {candidate}")
    return candidate


def _discover_scannable_planning_dirs(project_root: Path) -> list[Path]:
    planning_root = (project_root / "planning").resolve()
    if not planning_root.exists():
        return []
    found: list[Path] = []
    for candidate in sorted(path for path in planning_root.iterdir() if path.is_dir()):
        has_state = (candidate / ".autopilot_state.json").exists()
        has_rounds = (candidate / ".autopilot_rounds.jsonl").exists()
        if has_state or has_rounds:
            found.append(candidate)
    return found


def _needs_scan_all(
    *,
    scan_all: bool,
    updated_since: datetime | None,
    updated_until: datetime | None,
    run_ids: list[str],
    planning_dirs: list[str],
) -> bool:
    has_window = updated_since is not None or updated_until is not None
    has_explicit_targets = bool(run_ids or planning_dirs)
    return scan_all or (has_window and not has_explicit_targets)


def resolve_planning_dirs(
    *,
    project_root: Path,
    run_ids: list[str],
    planning_dirs: list[str],
    scan_all: bool,
    updated_since: datetime | None,
    updated_until: datetime | None,
) -> list[Path]:
    resolved: list[Path] = []
    seen: set[str] = set()
    for raw in run_ids:
        _add_unique_candidate(
            candidate=_resolve_run_id_candidate(project_root, raw),
            resolved=resolved,
            seen=seen,
            updated_since=updated_since,
            updated_until=updated_until,
        )
    for raw in planning_dirs:
        _add_unique_candidate(
            candidate=_resolve_explicit_planning_candidate(raw),
            resolved=resolved,
            seen=seen,
            updated_since=updated_since,
            updated_until=updated_until,
        )
    if _needs_scan_all(
        scan_all=scan_all,
        updated_since=updated_since,
        updated_until=updated_until,
        run_ids=run_ids,
        planning_dirs=planning_dirs,
    ):
        for candidate in _discover_scannable_planning_dirs(project_root):
            _add_unique_candidate(
                candidate=candidate,
                resolved=resolved,
                seen=seen,
                updated_since=updated_since,
                updated_until=updated_until,
            )
    return resolved


def load_run_summary(planning_dir: Path) -> dict[str, Any]:
    state = _load_json_dict(planning_dir / ".autopilot_state.json")
    rounds = _load_rounds(planning_dir / ".autopilot_rounds.jsonl")
    tasks = _load_tasks(planning_dir / "TASKS.md")
    gate_result = _load_optional_json_dict(planning_dir / ".gate_result.json")
    compact_context = _load_compact_context(planning_dir)
    semantic_compact = _load_semantic_compact(planning_dir)
    workflow_chain = bind_effective_gate_result(
        load_workflow_chain_snapshot(planning_dir),
        gate_result,
        state_payload=state,
    )
    subtasks = dict(state.get("subtasks", {}))
    completed_tasks = state.get("completed_tasks", [])
    return {
        "run_id": planning_dir.name,
        "planning_dir": planning_dir,
        "state": state,
        "rounds": rounds,
        "tasks": tasks,
        "gate_result": gate_result,
        "compact_context": compact_context,
        "semantic_compact": semantic_compact,
        "workflow_chain": workflow_chain,
        "tasks_total": len(tasks),
        "tasks_completed": len(completed_tasks) if isinstance(completed_tasks, list) else 0,
        "subtasks_total": len(subtasks),
        "subtasks_done": _count_subtasks_with_status(subtasks, "DONE"),
        "subtasks_failed": _count_subtasks_with_status(subtasks, "FAILED"),
        "review_rounds_used": count_peer_review_rounds(rounds),
    }


def _count_subtasks_with_status(subtasks: dict[str, Any], status: str) -> int:
    return sum(
        1
        for item in subtasks.values()
        if isinstance(item, dict) and str(item.get("status", "")).upper() == status
    )


__all__ = [
    "build_command_output_payload",
    "build_report_options",
    "load_run_summaries",
    "load_run_summary",
    "parse_datetime_filter",
    "resolve_cli_selection",
    "resolve_planning_dirs",
    "resolve_report_output_path",
    "selection_payload",
    "serialize_datetime",
]


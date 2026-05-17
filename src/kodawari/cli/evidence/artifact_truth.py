"""Authoritative artifact truth and stale-artifact helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from kodawari.cli.evidence.changed_files_truth import existing_paths, filter_project_root_paths

RUN_TRUTH_FILENAME = ".run_truth.json"
RUN_TRUTH_SCHEMA_VERSION = "run.truth.v1"


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _normalize_items(project_root: Path, values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return _dedupe(filter_project_root_paths(project_root, values))


def _payload_changed_files(payload: dict[str, Any] | None, *, project_root: Path) -> tuple[list[str], str]:
    changed = dict((payload or {}).get("changed_files") or {})
    items = _normalize_items(project_root, list(changed.get("items") or []))
    source = str(changed.get("source") or "").strip()
    return items, source


def _casefold_set(values: list[str]) -> set[str]:
    return {item.lower() for item in values}


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _string(value: Any) -> str:
    return str(value or "").strip()


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _planning_rounds(planning_dir: Path) -> int:
    for name in ("PLANNING_CONVERSATION.json", ".planning_failure.json", ".planning_in_progress.json"):
        payload = _load_json_dict(planning_dir / name)
        rounds = _list_of_dicts(payload.get("rounds"))
        if rounds:
            return len(rounds)
        count = _int_value(payload.get("round_count"))
        if count:
            return count
    return 0


def _runtime_stage_count(rounds: list[dict[str, Any]], *stages: str) -> int:
    expected = {stage.upper() for stage in stages}
    return sum(1 for item in rounds if _string(item.get("stage")).upper() in expected)


def _runtime_review_stats(rounds: list[dict[str, Any]], peer_summary: dict[str, Any]) -> tuple[int, int]:
    review_rounds = _int_value(peer_summary.get("review_round") or peer_summary.get("max_review_iteration"))
    must_fix_max = 0
    for item in rounds:
        if _string(item.get("stage")).upper() != "PEER_REVIEW":
            continue
        details = _dict(item.get("details"))
        review_rounds = max(review_rounds, _int_value(details.get("review_iteration") or item.get("review_round")))
        must_fix_max = max(must_fix_max, len(list(details.get("must_fix") or [])))
    return review_rounds, must_fix_max


def _recovery_decisions(planning_dir: Path, run_result: dict[str, Any]) -> list[dict[str, Any]]:
    decisions = _list_of_dicts(run_result.get("recovery_decisions"))
    if decisions:
        return decisions
    payload = _load_json_dict(planning_dir / ".execution_recovery_decision.json")
    return [payload] if payload else []


def _verify_status(payload: dict[str, Any], run_result: dict[str, Any]) -> str:
    verify = _dict(payload.get("verify_check") or run_result.get("verify_check"))
    if verify:
        return _string(verify.get("status")).upper()
    execution = _dict(payload.get("execution_result") or run_result.get("execution_result"))
    summary = _dict(execution.get("verify_summary"))
    return _string(summary.get("status")).upper()


def _gate_status(payload: dict[str, Any], run_result: dict[str, Any]) -> str:
    gate = _dict(payload.get("gate_check") or run_result.get("gate_check"))
    return _string(gate.get("total_status") or gate.get("status")).upper()


def _final_stage(payload: dict[str, Any], run_result: dict[str, Any]) -> str:
    unified = _dict(payload.get("unified_status") or run_result.get("unified_status"))
    final = _string(unified.get("stage_status") or unified.get("final_status"))
    if final:
        return final
    return _string(run_result.get("reason") or payload.get("run_reason"))


def resolve_authoritative_changed_files(
    *,
    project_root: Path,
    planning_dir: Path,
    state_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    execution_result = _load_json_dict(planning_dir / ".execution_result.json")
    task_run_result = _load_json_dict(planning_dir / ".task_run_result.json")
    state = dict(state_payload or {})
    candidates = (
        (".execution_result.json.changed_files", execution_result.get("changed_files") or []),
        ("task_run_result.task_delta_changed_files", task_run_result.get("task_delta_changed_files") or task_run_result.get("changed_files") or []),
        ("state.task_delta_changed_files", state.get("task_delta_changed_files") or []),
        ("state.changed_files", state.get("changed_files") or []),
    )
    for source, values in candidates:
        items = _normalize_items(project_root, values)
        existing = existing_paths(project_root, items)
        if existing:
            return {"items": existing, "source": source}
        if items and source in {".execution_result.json.changed_files", "state.task_delta_changed_files"}:
            return {"items": items, "source": source}
    return {"items": [], "source": "none"}


def resolve_review_artifact_truth(
    *,
    project_root: Path,
    planning_dir: Path,
    authoritative_changed_files: dict[str, Any],
) -> dict[str, Any]:
    payload = _load_json_dict(planning_dir / ".review_result.json")
    if not payload:
        return {
            "exists": False,
            "usable": False,
            "stale": False,
            "truth_source": "none",
            "changed_files": [],
            "changed_files_source": "",
            "stale_reasons": [],
        }
    changed_files, changed_source = _payload_changed_files(payload, project_root=project_root)
    stale_reasons: list[str] = []
    expected = list(authoritative_changed_files.get("items") or [])
    if expected:
        if not changed_files:
            stale_reasons.append("review_changed_files_missing")
        elif _casefold_set(changed_files) != _casefold_set(expected):
            stale_reasons.append("review_changed_files_mismatch")
    return {
        "exists": True,
        "usable": not stale_reasons,
        "stale": bool(stale_reasons),
        "truth_source": ".review_result.json" if not stale_reasons else "stale:.review_result.json",
        "changed_files": changed_files,
        "changed_files_source": changed_source,
        "stale_reasons": stale_reasons,
    }


def resolve_review_evidence_truth(
    *,
    planning_dir: Path,
    review_result_truth: dict[str, Any],
) -> dict[str, Any]:
    path = planning_dir / ".review_evidence.json"
    exists = path.exists()
    payload = _load_json_dict(path) if exists else {}
    stale_reasons: list[str] = []
    if exists and bool(review_result_truth.get("stale")):
        stale_reasons.append("review_result_stale")
    checks = dict(payload.get("checks") or {})
    if exists and str(payload.get("status") or "").strip().upper() != "PASS" and (
        "required_self_review" not in checks
        or "required_peer_review" not in checks
    ):
        stale_reasons.append("review_evidence_legacy_contract")
    return {
        "exists": exists,
        "usable": exists and not stale_reasons,
        "stale": bool(stale_reasons),
        "truth_source": ".review_evidence.json" if exists and not stale_reasons else "stale:.review_evidence.json" if exists else "none",
        "stale_reasons": stale_reasons,
    }


def resolve_verify_artifact_truth(
    *,
    project_root: Path,
    planning_dir: Path,
    authoritative_changed_files: dict[str, Any],
    review_result_truth: dict[str, Any],
) -> dict[str, Any]:
    payload = _load_json_dict(planning_dir / ".verify_report.json")
    if not payload:
        return {
            "exists": False,
            "usable": False,
            "stale": False,
            "truth_source": "none",
            "changed_files": [],
            "changed_files_source": "",
            "stale_reasons": [],
        }
    changed_files, changed_source = _payload_changed_files(payload, project_root=project_root)
    stale_reasons: list[str] = []
    expected = list(authoritative_changed_files.get("items") or [])
    if expected:
        if not changed_files:
            stale_reasons.append("verify_changed_files_missing")
        elif _casefold_set(changed_files) != _casefold_set(expected):
            stale_reasons.append("verify_changed_files_mismatch")
    if changed_source.startswith(".review_result.json") and bool(review_result_truth.get("stale")):
        stale_reasons.append("verify_depends_on_stale_review_result")
    return {
        "exists": True,
        "usable": not stale_reasons,
        "stale": bool(stale_reasons),
        "truth_source": ".verify_report.json" if not stale_reasons else "stale:.verify_report.json",
        "changed_files": changed_files,
        "changed_files_source": changed_source,
        "stale_reasons": stale_reasons,
    }


def resolve_effective_review_truth_source(
    *,
    planning_dir: Path,
    review_result_truth: dict[str, Any],
    review_evidence_truth: dict[str, Any],
) -> str:
    if bool(review_evidence_truth.get("usable")):
        return ".review_evidence.json"
    if bool(review_result_truth.get("usable")):
        return ".review_result.json"
    if (planning_dir / "REVIEW.md").exists():
        return "REVIEW.md"
    if bool(review_evidence_truth.get("exists")) or bool(review_result_truth.get("exists")):
        return "none(stale_review_artifacts)"
    return "none"


def resolve_effective_verify_truth_source(verify_truth: dict[str, Any]) -> str:
    if bool(verify_truth.get("usable")):
        return ".verify_report.json"
    if bool(verify_truth.get("exists")):
        return "none(stale_verify_artifacts)"
    return "none"


def build_run_truth(
    *,
    project_root: Path,
    planning_dir: Path,
    feature: str,
    payload: dict[str, Any],
    run_result: dict[str, Any],
    rounds: list[dict[str, Any]],
    state_payload: dict[str, Any] | None = None,
    reliable_changed_files: list[str] | tuple[str, ...] | None = None,
    changed_files_source: str = "",
) -> dict[str, Any]:
    """Build the single post-run truth artifact consumed by telemetry/reporting."""

    state = dict(state_payload or {})
    authoritative_changed = resolve_authoritative_changed_files(
        project_root=project_root,
        planning_dir=planning_dir,
        state_payload=state,
    )
    changed_items = _dedupe([str(item) for item in list(reliable_changed_files or []) if _string(item)])
    if not changed_items:
        changed_items = list(authoritative_changed.get("items") or [])
    review_result_truth = resolve_review_artifact_truth(
        project_root=project_root,
        planning_dir=planning_dir,
        authoritative_changed_files=authoritative_changed,
    )
    review_evidence_truth = resolve_review_evidence_truth(
        planning_dir=planning_dir,
        review_result_truth=review_result_truth,
    )
    verify_truth = resolve_verify_artifact_truth(
        project_root=project_root,
        planning_dir=planning_dir,
        authoritative_changed_files=authoritative_changed,
        review_result_truth=review_result_truth,
    )
    peer_summary = _dict(run_result.get("peer_review_summary") or payload.get("peer_review_summary"))
    # Fallback: HTTP reviewer adapters (mimo / opus gateway) sometimes do not
    # populate run_result.peer_review_summary, leaving peer_summary["approved"]
    # = None which bool()-casts to False. When that happens but the canonical
    # .review_evidence.json already shows status=PASS with 0 must_fix, treat
    # the review as approved. Keeps review_evidence (the authoritative reviewer
    # output) and run_truth.review_approved in sync.
    if not peer_summary or peer_summary.get("approved") is None:
        evidence_path = planning_dir / ".review_evidence.json"
        evidence_doc = _load_json_dict(evidence_path)
        if evidence_doc:
            ev_status = str(evidence_doc.get("status") or "").strip().upper()
            ev_must_fix = list(evidence_doc.get("must_fix") or [])
            ev_blocking = int(evidence_doc.get("blocking_findings") or 0)
            if ev_status == "PASS" and not ev_must_fix and ev_blocking == 0:
                inferred = dict(peer_summary)
                inferred["approved"] = True
                inferred.setdefault("review_count", 1)
                inferred.setdefault("approved_count", 1)
                inferred.setdefault("inferred_from", "review_evidence.json")
                peer_summary = inferred
    review_rounds, review_must_fix_max = _runtime_review_stats(rounds, peer_summary)
    decisions = _recovery_decisions(planning_dir, run_result)
    deterministic_recovery_hits = sum(
        1 for item in decisions if _string(item.get("role")) == "deterministic_recovery"
    )
    synthesizer_calls = sum(
        1
        for item in decisions
        if _string(item.get("role")) and _string(item.get("role")) != "deterministic_recovery"
    )
    recovery_lessons = _recovery_lessons_view(decisions=decisions, run_result=run_result, payload=payload)
    # Mirror engine_session_mixin: FIX_ROUND / CODEX_FIX are also executor invocations.
    executor_attempts = _int_value(run_result.get("executor_attempts")) or _runtime_stage_count(
        rounds, "IMPLEMENT", "FIX_ROUND", "CODEX_FIX"
    )
    planning_rounds = _planning_rounds(planning_dir)
    runtime_rounds = len(rounds)
    execution_result = _dict(run_result.get("execution_result") or payload.get("execution_result"))
    blocking_reason = _string(
        payload.get("blocking_reason")
        or run_result.get("blocking_reason")
        or execution_result.get("blocking_reason")
    )
    unexecuted_task_ids = _resolve_unexecuted_task_ids(planning_dir, state)
    return {
        "schema_version": RUN_TRUTH_SCHEMA_VERSION,
        "feature": str(feature or planning_dir.name),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "planning_dir": str(planning_dir),
        "final_status": _final_stage(payload, run_result),
        "run_reason": _string(run_result.get("reason") or payload.get("run_reason")),
        "blocking_reason": blocking_reason,
        "planning_rounds": planning_rounds,
        "runtime_rounds": runtime_rounds,
        "executor_attempts": executor_attempts,
        "execution_rounds": executor_attempts,
        "review_rounds": review_rounds,
        "review_must_fix_max": review_must_fix_max,
        "deterministic_recovery_hits": deterministic_recovery_hits,
        "synthesizer_calls": synthesizer_calls,
        "recovery_pressure": deterministic_recovery_hits + synthesizer_calls,
        "blocking_findings": sum(_int_value(item.get("blocking_findings_count")) for item in rounds),
        "verify_status": _verify_status(payload, run_result),
        "gate_status": _gate_status(payload, run_result),
        "review_approved": bool(peer_summary.get("approved", False)),
        "changed_files": changed_items,
        "changed_files_source": str(changed_files_source or authoritative_changed.get("source") or "none"),
        "truth_sources": {
            "changed_files": authoritative_changed,
            "review": resolve_effective_review_truth_source(
                planning_dir=planning_dir,
                review_result_truth=review_result_truth,
                review_evidence_truth=review_evidence_truth,
            ),
            "verify": resolve_effective_verify_truth_source(verify_truth),
        },
        "stale_artifacts": {
            "review_result": list(review_result_truth.get("stale_reasons") or []),
            "review_evidence": list(review_evidence_truth.get("stale_reasons") or []),
            "verify_report": list(verify_truth.get("stale_reasons") or []),
        },
        "artifact_paths": _artifact_path_manifest(planning_dir),
        "recovery_lessons": recovery_lessons,
        "unexecuted_task_ids": unexecuted_task_ids,
    }


def _recovery_lessons_view(
    *,
    decisions: list[dict[str, Any]],
    run_result: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Derive a stable lessons view from already-tracked recovery decisions.

    Avoids a parallel ``recovery_lessons.json`` file: anything a downstream
    consumer (planner prompts, instincts, dashboards) would want to learn
    from a recovery is already in the recovery decision payload. We just
    project the relevant slice and stamp whether the run ended successful
    enough that the lesson is worth replaying.
    """

    final_status = _string(payload.get("final_status") or run_result.get("final_status")).upper()
    run_succeeded = final_status in {"OK", "PASS", "PROCEED_TO_GATE", "PROCEED_TO_RELEASE"}

    lessons: list[dict[str, Any]] = []
    for item in decisions:
        role = _string(item.get("role"))
        action = _string(item.get("action"))
        if not role and not action:
            continue
        lessons.append(
            {
                "role": role,
                "action": action,
                "detector_name": _string(item.get("detector_name")),
                "reason": _string(item.get("reason") or item.get("diagnosis")),
                "source": _string(item.get("source")),
                "produced_card": bool(item.get("detector_evidence") or action == "narrow_patch_plan"),
                "applies_to_future_runs": run_succeeded and role == "deterministic_recovery",
            }
        )
    return lessons


_ARTIFACT_MANIFEST_NAMES: tuple[str, ...] = (
    ".execution_result.json",
    ".execution_readiness.json",
    ".execution_recovery_card.json",
    ".execution_recovery_decision.json",
    ".execution_failure_snapshot.json",
    ".execution_stall_report.json",
    ".review_result.json",
    ".review_evidence.json",
    ".review_bundle.json",
    ".verify_report.json",
    ".task_run_result.json",
    ".autopilot_state.json",
    ".lane_observation.json",
    ".workflow_self_repair.json",
    ".workflow_chain.json",
    ".planning_failure.json",
    ".planning_in_progress.json",
    "PLANNING_CONVERSATION.json",
    "PRD_INTAKE.json",
    "TASK_GRAPH.json",
    "TASK_CARD_ACTIVE.json",
    "REVIEW.md",
    "DELIVERY_REPORT.md",
    "SELF_REPAIR.md",
)


def _completed_task_id_from_label(label: str) -> str:
    """state.completed_tasks stores entries shaped like 'T1: <title>'. Return the
    bare TASK_ID prefix (uppercased), or empty string if the shape is unexpected."""
    text = str(label or "").strip()
    if not text:
        return ""
    head = text.split(":", 1)[0].strip()
    return head.upper()


def _resolve_unexecuted_task_ids(planning_dir: Path, state_payload: dict[str, Any]) -> list[str]:
    """Return task_ids present in TASK_GRAPH.json that are not in state.completed_tasks.

    Used to surface 'plan had N tasks but only K ran' when task_cycle is off
    or the loop bailed mid-graph. Returns [] when TASK_GRAPH absent / unreadable
    or when every task is accounted for.
    """
    graph_path = Path(planning_dir) / "TASK_GRAPH.json"
    if not graph_path.exists():
        return []
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    raw_tasks = graph.get("tasks") if isinstance(graph, dict) else None
    if not isinstance(raw_tasks, list):
        return []
    completed_labels = state_payload.get("completed_tasks") if isinstance(state_payload, dict) else None
    completed_ids: set[str] = set()
    if isinstance(completed_labels, list):
        for label in completed_labels:
            normalized = _completed_task_id_from_label(str(label))
            if normalized:
                completed_ids.add(normalized)
    unexecuted: list[str] = []
    seen: set[str] = set()
    for entry in raw_tasks:
        if not isinstance(entry, dict):
            continue
        raw_id = str(entry.get("task_id") or entry.get("id") or "").strip().upper()
        if not raw_id or raw_id in seen:
            continue
        seen.add(raw_id)
        if raw_id not in completed_ids:
            unexecuted.append(raw_id)
    return unexecuted


def _artifact_path_manifest(planning_dir: Path) -> dict[str, str]:
    """Return a manifest of {name: relative_path} for artifacts present in planning_dir.

    The manifest gives downstream consumers (delivery report, lane observation,
    instincts ingestion) a single index of which truth files actually exist for
    this run, so they stop guessing or globbing.
    """

    root = Path(planning_dir).resolve()
    manifest: dict[str, str] = {}
    for name in _ARTIFACT_MANIFEST_NAMES:
        candidate = root / name
        if candidate.exists() and candidate.is_file():
            manifest[name] = name
    return manifest


def write_run_truth(planning_dir: Path, payload: dict[str, Any]) -> Path:
    path = (Path(planning_dir) / RUN_TRUTH_FILENAME).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_run_truth(planning_dir: Path) -> dict[str, Any]:
    payload = _load_json_dict(Path(planning_dir) / RUN_TRUTH_FILENAME)
    return payload if _string(payload.get("schema_version")) == RUN_TRUTH_SCHEMA_VERSION else {}


__all__ = [
    "RUN_TRUTH_FILENAME",
    "RUN_TRUTH_SCHEMA_VERSION",
    "build_run_truth",
    "load_run_truth",
    "resolve_authoritative_changed_files",
    "resolve_effective_review_truth_source",
    "resolve_effective_verify_truth_source",
    "resolve_review_artifact_truth",
    "resolve_review_evidence_truth",
    "resolve_verify_artifact_truth",
    "write_run_truth",
]

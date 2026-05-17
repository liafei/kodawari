"""Resume logic for escalation decisions.

When autopilot starts, it checks for pending ``.{phase}_decision_response.json``
files (written by ``kodawari decide``) and applies the user's choice
*before* the normal planning/execution loop runs.

Two main resume paths:

1. **Planning split** (``.planning_split_proposal.json`` from PLANNING_DEADLOCK
   accept): autopilot spawns sub-features in topological order. Parent
   feature is marked SUPERSEDED_BY_SPLIT (read-only audit).

2. **In-place response** (``.{phase}_decision_response.json``):
   - planning skip → mark feature aborted
   - executor skip → drop current task, advance cursor
   - executor accept/custom → inject must_fix (handled by existing sticky-decision)
   - gate accept → mirror to legacy recovery card (already done by legacy compat)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


SPLIT_PROPOSAL_FILENAME = ".planning_split_proposal.json"
SUPERSEDED_MARKER_FILENAME = "SUPERSEDED_BY_SPLIT.md"


def detect_pending_resume(planning_dir: Path) -> dict[str, Any] | None:
    """Detect any pending escalation resume action.

    Returns the first found pending resume, or ``None`` if nothing pending.

    Resume priority:
        1. split proposal (planning deadlock accept)
        2. planning decision response (skip/custom)
        3. executor decision response (skip/custom)
        4. gate decision response
    """
    planning_dir = Path(planning_dir)

    split_path = planning_dir / SPLIT_PROPOSAL_FILENAME
    if split_path.exists():
        try:
            data = json.loads(split_path.read_text(encoding="utf-8"))
            if not data.get("applied_at"):
                return {"kind": "split_proposal", "path": split_path, "data": data}
        except (OSError, json.JSONDecodeError):
            pass

    for phase in ("planning", "executor", "gate"):
        resp_path = planning_dir / f".{phase}_decision_response.json"
        if not resp_path.exists():
            continue
        try:
            data = json.loads(resp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # consumed_at is written by decide; we use applied_at as our own marker
        if data.get("applied_at"):
            continue
        return {"kind": "decision_response", "phase": phase, "path": resp_path, "data": data}

    return None


def apply_pending_resume(
    *,
    planning_dir: Path,
    feature: str,
    project_root: Path,
    autopilot_args: list[str] | None = None,
) -> dict[str, Any]:
    """Apply the next pending resume action.

    Returns a dict describing what was done:
        - status: applied / skipped / aborted / failed
        - kind: split_proposal / decision_response
        - phase: planning / executor / gate
        - details: ...

    For ``split_proposal``, this spawns sub-features (autopilot subprocess
    per sub-feature) in topological order. Parent is marked SUPERSEDED.
    """
    pending = detect_pending_resume(planning_dir)
    if pending is None:
        return {"status": "no_pending", "kind": None}

    if pending["kind"] == "split_proposal":
        return _apply_split_proposal(
            planning_dir=planning_dir,
            feature=feature,
            project_root=project_root,
            split_data=pending["data"],
            split_path=pending["path"],
            autopilot_args=autopilot_args or [],
        )

    if pending["kind"] == "decision_response":
        return _apply_decision_response(
            planning_dir=planning_dir,
            feature=feature,
            phase=pending["phase"],
            response_data=pending["data"],
            response_path=pending["path"],
        )

    return {"status": "skipped", "kind": pending["kind"]}


# ---------------------------------------------------------------------------
# Split proposal handler
# ---------------------------------------------------------------------------


def _apply_split_proposal(
    *,
    planning_dir: Path,
    feature: str,
    project_root: Path,
    split_data: dict[str, Any],
    split_path: Path,
    autopilot_args: list[str],
) -> dict[str, Any]:
    """Spawn sub-features in topological order; mark parent SUPERSEDED."""
    sub_features = list(split_data.get("sub_features") or [])
    if not sub_features:
        return {"status": "failed", "kind": "split_proposal", "error": "no sub_features in proposal"}

    parent_split_depth = int(split_data.get("parent_split_depth") or 0)
    max_depth = int(split_data.get("max_depth_check", {}).get("limit", 2))
    if parent_split_depth >= max_depth:
        return {
            "status": "failed",
            "kind": "split_proposal",
            "error": f"max_split_depth ({max_depth}) reached",
        }

    # Topological sort sub_features by depends_on
    sorted_subs, cycle = _topological_sort(sub_features)
    if cycle:
        return {
            "status": "failed",
            "kind": "split_proposal",
            "error": f"depends_on cycle detected: {cycle}",
        }

    parent_dir = planning_dir
    parent_dir.parent.mkdir(parents=True, exist_ok=True)

    # Spawn each sub-feature as a subprocess
    results: list[dict[str, Any]] = []
    for sub in sorted_subs:
        sub_name = str(sub.get("name") or "").strip()
        if not sub_name:
            continue
        prd_excerpt = str(sub.get("prd_excerpt") or sub.get("task_summary") or "")
        task_summary = str(sub.get("task_summary") or sub_name)

        # Write sub-PRD to a temp location under parent dir
        sub_prd_path = parent_dir / f".split_prd_{sub_name}.md"
        sub_prd_path.write_text(
            f"# Sub-feature: {sub_name}\n\n"
            f"## Origin\nSplit from parent feature `{feature}` "
            f"(split_depth={parent_split_depth + 1}/{max_depth}).\n\n"
            f"## Task Summary\n{task_summary}\n\n"
            f"## PRD\n{prd_excerpt}\n",
            encoding="utf-8",
        )

        cmd = [sys.executable, "-m", "kodawari.cli.main", "autopilot"]
        cmd.extend(["--feature", sub_name])
        cmd.extend(["--prd", str(sub_prd_path)])
        cmd.extend(["--task", task_summary])
        # Inherit autopilot args from parent (cycles, gate-profile, etc.)
        cmd.extend(autopilot_args)

        logger.info(f"Spawning sub-feature: {sub_name}")
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(project_root),
                timeout=7200,  # 2 hour ceiling per sub-feature
                check=False,
            )
            results.append({
                "name": sub_name,
                "exit_code": proc.returncode,
                "status": "completed" if proc.returncode == 0 else "blocked",
            })
            if proc.returncode != 0:
                logger.warning(f"Sub-feature {sub_name} exited {proc.returncode}; halting chain")
                break
        except subprocess.TimeoutExpired:
            results.append({"name": sub_name, "status": "timeout"})
            break
        except OSError as exc:
            results.append({"name": sub_name, "status": "failed", "error": str(exc)})
            break

    # Mark parent SUPERSEDED
    _mark_parent_superseded(parent_dir, feature, sub_features, results)

    # Mark split proposal as applied
    split_data["applied_at"] = datetime.now(timezone.utc).isoformat()
    split_data["sub_results"] = results
    split_path.write_text(json.dumps(split_data, indent=2, ensure_ascii=False), encoding="utf-8")

    success_count = sum(1 for r in results if r.get("exit_code") == 0)
    return {
        "status": "applied" if success_count == len(sorted_subs) else "partial",
        "kind": "split_proposal",
        "sub_features": [r["name"] for r in results],
        "sub_results": results,
        "success_count": success_count,
        "total_count": len(sorted_subs),
    }


def _topological_sort(
    sub_features: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Topological sort sub_features by depends_on.

    Returns (sorted_list, cycle_path). cycle_path is empty if no cycle.
    """
    name_to_sub = {str(s.get("name") or ""): s for s in sub_features if s.get("name")}
    visited: dict[str, int] = {}  # 0=unvisited, 1=on_stack, 2=done
    order: list[dict[str, Any]] = []
    cycle: list[str] = []

    def dfs(name: str, stack: list[str]) -> bool:
        if visited.get(name) == 2:
            return True
        if visited.get(name) == 1:
            cycle.extend(stack[stack.index(name):] + [name])
            return False
        visited[name] = 1
        stack.append(name)
        sub = name_to_sub.get(name, {})
        for dep in sub.get("depends_on") or []:
            dep_name = str(dep).strip()
            if dep_name and dep_name in name_to_sub:
                if not dfs(dep_name, stack):
                    return False
        stack.pop()
        visited[name] = 2
        order.append(sub)
        return True

    for sub in sub_features:
        name = str(sub.get("name") or "")
        if not name:
            continue
        if not dfs(name, []):
            return [], cycle
    return order, []


def _mark_parent_superseded(
    parent_dir: Path,
    parent_feature: str,
    sub_features: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    marker = parent_dir / SUPERSEDED_MARKER_FILENAME
    lines = [
        f"# Feature `{parent_feature}` SUPERSEDED by split",
        "",
        f"Superseded at: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"This feature's planning deadlocked at the reviewer loop. The user accepted",
        f"a split via `kodawari decide`. The feature was decomposed into:",
        "",
    ]
    for sub, res in zip(sub_features, results + [{}] * (len(sub_features) - len(results))):
        name = str(sub.get("name") or "?")
        status = res.get("status", "not_started")
        depends_on = sub.get("depends_on") or []
        lines.append(f"- **{name}** (depends_on={depends_on}) — {status}")
    lines.append("")
    lines.append("Original artifacts (PLANNING_CONVERSATION.json, .planning_failure.json, .planning_decision_*.json) retained in this directory for audit.")
    marker.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Decision response handler (planning / executor / gate skip-or-custom)
# ---------------------------------------------------------------------------


def _apply_decision_response(
    *,
    planning_dir: Path,
    feature: str,
    phase: str,
    response_data: dict[str, Any],
    response_path: Path,
) -> dict[str, Any]:
    """Apply a non-split decision response.

    For planning/executor/gate phases, the response carries action +
    option. We mark applied_at to prevent re-processing.
    """
    action = str(response_data.get("action") or "skip")
    kind = str(response_data.get("escalation_kind") or "")

    outcome: dict[str, Any] = {
        "status": "applied",
        "kind": "decision_response",
        "phase": phase,
        "action": action,
        "escalation_kind": kind,
    }

    if action == "abort":
        outcome["effect"] = "feature_aborted"
        _mark_feature_aborted(planning_dir, feature, kind=kind)
    elif action == "skip":
        if phase == "planning":
            outcome["effect"] = "feature_skipped"
            _mark_feature_aborted(planning_dir, feature, kind=kind)
        elif phase == "executor":
            outcome["effect"] = "task_skipped"
            # Active task will be advanced by engine on next cycle (cursor moves)
        else:
            outcome["effect"] = "gate_skipped"
    elif action == "accept" or action == "custom":
        outcome["effect"] = "approach_applied"
        # Special case: PLANNING_APPROVAL_REQUIRED + accept means user
        # approved the plan as-is. We must finalize the plan artifacts
        # (TASK_GRAPH.json + TASK_CARD_*.json) so autopilot can resume
        # straight into execution without re-running the planner loop.
        if (
            phase == "planning"
            and action == "accept"
            and kind == "PLANNING_APPROVAL_REQUIRED"
        ):
            finalize_outcome = _finalize_approved_plan(planning_dir)
            outcome["plan_finalize"] = finalize_outcome

    # Mark as applied so we don't re-process
    response_data["applied_at"] = datetime.now(timezone.utc).isoformat()
    response_path.write_text(json.dumps(response_data, indent=2, ensure_ascii=False), encoding="utf-8")
    return outcome


def _finalize_approved_plan(planning_dir: Path) -> dict[str, Any]:
    """Write TASK_GRAPH.json + TASK_CARD_*.json from PLANNING_CONVERSATION
    after user accepted the plan.

    The last round's ``plan_payload`` is structurally compatible with
    ``TASK_GRAPH.json``. We promote that payload and emit one
    ``TASK_CARD_<task_id>.json`` per task plus a ``TASK_CARD_ACTIVE.json``
    pointing to the first task.

    Also clears stale ``.planning_failure.json`` so autopilot does not
    re-trigger planner.
    """
    conv_path = planning_dir / "PLANNING_CONVERSATION.json"
    if not conv_path.exists():
        return {"status": "skipped", "reason": "no_planning_conversation"}
    try:
        conv = json.loads(conv_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "failed", "reason": f"conversation_read: {exc}"}

    rounds = conv.get("rounds") or []
    if not rounds:
        return {"status": "skipped", "reason": "no_rounds"}

    # Pick best round (last 0-blocking or final round)
    best = None
    for r in reversed(rounds):
        if int(r.get("blocking_findings_count") or 0) == 0:
            best = r
            break
    if best is None:
        best = rounds[-1]

    plan_payload = dict(best.get("plan_payload") or {})
    tasks = list(plan_payload.get("tasks") or [])
    if not tasks:
        return {"status": "skipped", "reason": "no_tasks_in_plan"}

    # Set schema version and required envelope keys for TASK_GRAPH.json
    plan_payload.setdefault("schema_version", "contract_first.task_graph.v1")
    plan_payload.setdefault("feature", planning_dir.name)
    # Required by task_graph schema — synthesize an "approved by user" stamp.
    # status enum is restricted to PASS/FAIL/WARN.
    if "executability" not in plan_payload:
        plan_payload["executability"] = {
            "status": "PASS",
            "reason": "user accepted plan via kodawari decide PLANNING_APPROVAL_REQUIRED",
            "blocking_findings": 0,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
    # Each task needs: core_files (subset of files_to_change), test_proof, executability
    for task in plan_payload.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        files_to_change = list(task.get("files_to_change") or [])
        if "core_files" not in task:
            # task_card schema caps files_to_change at 3 and build_task_card
            # reads from core_files, not files_to_change. Take all (up to 3).
            task["core_files"] = files_to_change[:3] if files_to_change else []
        if "test_proof" not in task:
            verify = str(task.get("verify_cmd") or "").strip()
            test_plan = str(task.get("test_plan") or "").strip()
            task["test_proof"] = (
                (verify or "no verify_cmd declared") + " | " + (test_plan[:200] if test_plan else "no test_plan")
            )
        if "executability" not in task:
            task["executability"] = {
                "status": "PASS",
                "reason": "Plan approved by user",
                "blocking_findings": 0,
            }
    graph_path = planning_dir / "TASK_GRAPH.json"
    try:
        from kodawari.infra.io_atomic import atomic_write_json
        atomic_write_json(graph_path, plan_payload)
    except Exception as exc:
        return {"status": "failed", "reason": f"task_graph_write: {exc}"}

    # Write per-task TASK_CARD_<id>.json. task_card schema requires:
    #   schema_version, task_id, why_this_layer, files_to_change, invariants,
    #   test_plan, requires
    def _to_task_card(t: dict) -> dict:
        card = dict(t)
        card["schema_version"] = "contract_first.task_card.v1"
        card.setdefault("why_this_layer",
            f"Plan task generated for feature {planning_dir.name}; "
            f"surface={t.get('surface', 'backend')}, layer_owner={t.get('layer_owner', 'unknown')}")
        card.setdefault("test_plan", str(t.get("test_plan") or t.get("verify_cmd") or "pytest"))
        card.setdefault("requires", list(t.get("requires") or []))
        card.setdefault("invariants", list(t.get("invariants") or ["Plan approved by user"]))
        return card

    written_cards: list[str] = []
    for task in tasks:
        task_id = str(task.get("task_id") or task.get("id") or "").strip()
        if not task_id:
            continue
        card = _to_task_card(task)
        card_path = planning_dir / f"TASK_CARD_{task_id}.json"
        try:
            from kodawari.infra.io_atomic import atomic_write_json
            atomic_write_json(card_path, card)
            written_cards.append(task_id)
        except Exception:
            continue

    # Promote first task to ACTIVE
    if tasks:
        first_card = _to_task_card(tasks[0])
        active_path = planning_dir / "TASK_CARD_ACTIVE.json"
        try:
            from kodawari.infra.io_atomic import atomic_write_json
            atomic_write_json(active_path, first_card)
        except Exception:
            pass

    # Clear stale planning_failure so autopilot doesn't redo planner
    for stale in (".planning_failure.json", ".planning_in_progress.json"):
        stale_path = planning_dir / stale
        if stale_path.exists():
            try:
                stale_path.unlink()
            except OSError:
                pass

    return {
        "status": "finalized",
        "tasks": [str(t.get("task_id") or t.get("id") or "?") for t in tasks],
        "task_cards_written": written_cards,
        "task_graph": str(graph_path),
    }


def _mark_feature_aborted(planning_dir: Path, feature: str, *, kind: str) -> None:
    marker = planning_dir / "FEATURE_ABORTED.md"
    marker.write_text(
        f"# Feature `{feature}` aborted\n\n"
        f"Reason: user chose to skip/abort during escalation ({kind}).\n"
        f"Aborted at: {datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )


__all__ = [
    "SPLIT_PROPOSAL_FILENAME",
    "SUPERSEDED_MARKER_FILENAME",
    "apply_pending_resume",
    "detect_pending_resume",
]

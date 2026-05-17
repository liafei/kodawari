"""External self-review helpers for the local adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any


def self_review_input(
    *,
    task: str,
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
) -> dict[str, Any]:
    return {
        "feature": str(context.get("feature") or task).strip(),
        "task": task,
        "task_id": str(context.get("task_id") or "").strip(),
        "review_iteration": int(review_iteration),
        "changed_files": [str(item) for item in changed_files if str(item).strip()],
        "invariants": [str(item) for item in list(context.get("task_invariants") or []) if str(item).strip()],
        "task_scope": str(context.get("task_scope") or "").strip(),
    }


def external_self_review(
    adapter: Any,
    *,
    task: str,
    context: dict[str, Any],
    changed_files: list[str],
    review_iteration: int,
) -> dict[str, Any]:
    command = str(adapter.config.self_review_command or adapter.config.executor_command or "").strip()
    if not command:
        return {
            "status": "BLOCKED",
            "approved": False,
            "summary": "external self-review backend requires a review command",
            "blocking_reason": "SELF_REVIEW_COMMAND_MISSING",
            "reviewer": "codex",
            "source": "kodawari.self_review.external_cli",
        }
    planning_dir = Path(str(context.get("planning_dir") or adapter.config.cwd or Path.cwd())).resolve()
    input_path = planning_dir / ".self_review_input.json"
    input_payload = self_review_input(
        task=task,
        context=context,
        changed_files=changed_files,
        review_iteration=review_iteration,
    )
    input_path.write_text(json.dumps(input_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "WORKFLOW_AUTOMATION_STAGE": "self_review",
            "WORKFLOW_SELF_REVIEW_INPUT_PATH": str(input_path),
            "WORKFLOW_FEATURE": str(input_payload["feature"]),
            "WORKFLOW_TASK": task,
        }
    )
    try:
        run = subprocess.run(
            command,
            shell=True,
            cwd=str(Path(str(context.get("project_root") or adapter.config.cwd or Path.cwd())).resolve()),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=max(30, int(adapter.config.timeout_seconds)),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "BLOCKED",
            "approved": False,
            "summary": "external self-review command timed out",
            "blocking_reason": "SELF_REVIEW_TIMEOUT",
            "reviewer": "codex",
            "source": "kodawari.self_review.external_cli",
        }
    payload = {}
    try:
        payload = json.loads(str(run.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if run.returncode != 0 and not payload:
        return {
            "status": "BLOCKED",
            "approved": False,
            "summary": (run.stderr or run.stdout or "external self-review failed").strip(),
            "blocking_reason": "SELF_REVIEW_COMMAND_FAILED",
            "reviewer": "codex",
            "source": "kodawari.self_review.external_cli",
        }
    payload.setdefault("reviewer", "codex")
    payload.setdefault("source", "kodawari.self_review.external_cli")
    payload.setdefault("summary", "external self-review completed")
    payload.setdefault("approved", bool(payload.get("approved", False)))
    return payload


"""Mechanical task splitting agent for planning payloads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

from kodawari.autopilot.core.json_extractor import extract_json_object
from kodawari.autopilot.core.model_config import WorkflowTransportConfig
from kodawari.autopilot.core.openai_chat_client import call_openai_chat
from kodawari.autopilot.core.subprocess_compat import subprocess_text_kwargs, windows_safe_command
from kodawari.autopilot.planning.planner_errors import classify_chat_result_failure


def _subprocess_env() -> dict[str, str]:
    """Build a clean env — remove CLAUDECODE to allow nested CLI calls."""
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    return env


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _sanitize_model_for_cli(value: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if text.startswith("-"):
        return ""
    if len(text) > 200:
        return ""
    if any(ord(ch) < 32 for ch in text):
        return ""
    return text


def _resolved_executable(configured: str, *, default: str = "claude") -> str:
    text = _clean_text(configured) or default
    candidate = Path(text)
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(text)
    return str(Path(resolved)) if resolved else text


def _driver_for_cli(*, driver: str, executable: str, default: str = "claude_cli") -> str:
    configured = _clean_text(driver).lower().replace("-", "_")
    if configured:
        return configured
    exe = Path(_clean_text(executable)).stem.lower()
    if exe.startswith("claude"):
        return "claude_cli"
    if exe.startswith("codex"):
        return "codex_cli"
    return default


def _build_command(
    *,
    executable: str,
    model: str,
    driver: str = "",
    project_root: Path | None = None,
) -> list[str]:
    resolved_driver = _driver_for_cli(driver=driver, executable=executable, default="claude_cli")
    if resolved_driver == "claude_cli":
        args = ["-p", "--output-format", "json", "--max-turns", "1"]
    else:
        args = ["exec", "--skip-git-repo-check", "--sandbox", "read-only"]
        if project_root is not None:
            args.extend(["--cd", str(project_root.resolve())])
    safe_model = _sanitize_model_for_cli(model)
    if safe_model:
        args.extend(["--model", safe_model])
    return windows_safe_command(executable, *args)


def _build_prompt(
    *,
    plan_payload: dict[str, Any],
    project_root: Path | None = None,
) -> str:
    plan_json = json.dumps(plan_payload, ensure_ascii=False, indent=2)
    workspace_root = str(project_root.resolve()) if project_root is not None else "<current working directory>"
    return (
        "You are a task splitter for kodawari planning payloads.\n"
        "Your role is MECHANICAL: split large tasks into smaller sub-tasks by splitting:\n"
        "- invariants > 2 into groups of ≤2\n"
        "- files_to_change > 3 into groups of ≤3\n\n"
        "Return ONLY JSON and no markdown fences.\n\n"
        "Required JSON shape:\n"
        "{\n"
        '  "tasks": [array of split tasks, preserving all original fields],\n'
        '  "splitter_status": "split_applied" | "no_split_needed",\n'
        '  "splits_applied": number,\n'
        '  "invariants_parity_ok": boolean,\n'
        '  "assessment": string\n'
        "}\n\n"
        f"ACTIVE WORKSPACE ROOT: {workspace_root}\n\n"
        f"Planning payload to split:\n{plan_json}\n"
    )


def _extract_content(stdout: str) -> str:
    text = _clean_text(stdout)
    if not text:
        return ""
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict):
        result = envelope.get("result") or envelope.get("content") or envelope.get("text")
        if isinstance(result, str):
            return result
        if isinstance(result, list):
            joined = "\n".join(
                str(item.get("text") or "")
                for item in result
                if isinstance(item, dict) and _clean_text(item.get("type")).lower() == "text"
            ).strip()
            if joined:
                return joined
    return text


def _parse_response(stdout: str) -> tuple[dict[str, Any] | None, str]:
    payload = extract_json_object(stdout)
    if payload is not None:
        return payload, ""
    text = _extract_content(stdout)
    if not text:
        return None, "task splitter returned empty output"
    return None, "task splitter output is not valid json"


def _noop_split() -> dict[str, Any]:
    return {
        "tasks": [],
        "splitter_status": "no_split_needed",
        "splits_applied": 0,
        "invariants_parity_ok": True,
        "assessment": "no tasks required splitting",
    }


def _check_invariants_parity(
    original_tasks: list[dict[str, Any]],
    split_tasks: list[dict[str, Any]],
) -> bool:
    """Verify that all invariants from input are preserved in output (union parity)."""
    def collect_invariants(tasks: list[dict[str, Any]]) -> set[str]:
        inv_set: set[str] = set()
        for task in tasks:
            invs = task.get("invariants") or []
            if isinstance(invs, list):
                inv_set.update(str(i).strip() for i in invs if i)
        return inv_set

    original = collect_invariants(original_tasks)
    split = collect_invariants(split_tasks)
    return original == split


def split_plan(
    *,
    executable: str,
    plan_payload: dict[str, Any],
    timeout_seconds: int = 180,
    model: str = "",
    driver: str = "",
    transport: WorkflowTransportConfig | None = None,
    project_root: Path | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Split tasks in a plan payload using a non-reasoning model (via CLI or HTTP).

    On any failure, returns (None, error_message). Caller must fall back to original payload.
    """
    if transport is not None:
        resolved_driver = _clean_text(transport.driver).lower().replace("-", "_")
        interface = _clean_text(transport.interface).lower().replace("-", "_")
        kind = _clean_text(transport.kind).lower().replace("-", "_")
    else:
        resolved_driver = _driver_for_cli(driver=driver, executable=executable, default="claude_cli")
        interface = ""
        kind = ""

    prompt = _build_prompt(plan_payload=plan_payload, project_root=project_root)

    if transport is not None and kind == "http" and interface in {"chat", "tool_use"}:
        result = call_openai_chat(
            transport=transport,
            model=model,
            system=(
                "You are a task splitter for planning payloads. "
                "Split large tasks by invariants and files_to_change. "
                "Return JSON only."
            ),
            user=prompt,
            timeout_seconds=timeout_seconds,
            response_format={"type": "json_object"},
        )
        if not result.ok:
            return None, classify_chat_result_failure(kind=result.kind, detail=result.detail).render()
        split_result, parse_error = _parse_response(result.raw_text)
        if split_result is not None:
            return split_result, ""
        return None, parse_error or "task splitter HTTP response did not contain valid JSON"

    if resolved_driver not in {"codex_cli", "claude_cli"}:
        return (
            None,
            f"task splitter transport not supported (kind={kind!r}, interface={interface!r}, driver={resolved_driver!r}); "
            "expected http+chat/tool_use or codex_cli/claude_cli",
        )

    default_executable = "claude" if resolved_driver == "claude_cli" else "codex"
    resolved = _resolved_executable(
        transport.primary_executable() if transport is not None and transport.primary_executable() else executable,
        default=default_executable,
    )
    command = _build_command(executable=resolved, model=model, driver=resolved_driver, project_root=project_root)
    cwd = str(project_root.resolve()) if project_root is not None else None
    try:
        completed = subprocess.run(
            command,
            **subprocess_text_kwargs(
                input=prompt,
                timeout=max(30, int(timeout_seconds or 180)),
                env=_subprocess_env(),
                cwd=cwd,
            ),
        )
    except subprocess.TimeoutExpired:
        return None, "task splitter timed out"
    except OSError as exc:
        return None, f"task splitter failed to start: {exc}"

    if completed.returncode != 0:
        stderr = _clean_text(completed.stderr)
        return None, f"task splitter exited with code {completed.returncode}: {stderr}"

    return _parse_response(completed.stdout)


__all__ = [
    "split_plan",
    "_build_command",
    "_build_prompt",
    "_check_invariants_parity",
]

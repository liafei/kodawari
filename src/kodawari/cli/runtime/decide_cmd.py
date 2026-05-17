"""CLI command for handling executor redesign decisions.

Flow:
1. Read .executor_redesign_request.json (written by engine on gate_complexity exhaustion)
2. Read the violating function source code + task card context
3. Call the Planner (claude CLI) to generate 2-3 real redesign options
4. Show GUI/CLI decision dialog for user choice
5. Write user's selection into a new .execution_recovery_card.json so the
   engine picks up the chosen refactor approach on the next autopilot run
6. Clear the "executor_recovery_escalated" state so autopilot can resume
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PLANNER_TIMEOUT_SECONDS = 180


def _resolve_planner_http_transport(project_root: Path) -> tuple[Any, Any] | None:
    """Return (planner_role, transport) when the planner is wired to HTTP.

    Reads models.yaml in *project_root*. Returns None if config is missing,
    the planner role is unset, or the planner's transport isn't ``kind=http``.
    """
    try:
        from kodawari.autopilot.core.model_config import load_model_config
    except ImportError:
        return None
    try:
        config = load_model_config(project_root)
    except Exception as exc:  # noqa: BLE001 — best-effort fallback
        logger.warning("load_model_config failed: %s", exc)
        return None
    planner = config.get_role("planner", fallback=False)
    if planner is None or not planner.model:
        return None
    transport = config.transports.get(planner.transport)
    if transport is None or transport.kind != "http":
        return None
    return planner, transport


def _planner_http_credentials(transport: Any) -> tuple[str, str] | None:
    """Pull (base_url, api_key) for a planner HTTP transport, or None if missing."""
    base_url = transport.base_url
    if not base_url and transport.base_url_env:
        base_url = os.environ.get(transport.base_url_env, "")
    if not base_url:
        return None
    api_key_env = transport.api_key_env or ""
    api_key = os.environ.get(api_key_env, "") if api_key_env else ""
    if not api_key:
        return None
    return base_url, api_key


def _post_planner_chat(
    *, base_url: str, api_key: str, model: str, prompt: str
) -> str | None:
    """POST an OpenAI-style chat completion to *base_url* and return the text.

    Returns None on transport / parse failure so the caller falls back cleanly.
    """
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
    }
    req = urllib.request.Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=_PLANNER_TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        logger.warning("HTTP planner call failed: %s", exc)
        return None
    try:
        data = json.loads(body)
        return str(data["choices"][0]["message"]["content"] or "")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        logger.warning("HTTP planner returned unparseable response: %s; body=%s", exc, body[:300])
        return None


def _call_planner_via_role(prompt: str, *, project_root: Path | None) -> str | None:
    """Invoke the configured planner role via its HTTP transport.

    Resolves models.yaml in *project_root*, picks the planner role's transport,
    and POSTs an OpenAI-style chat completion. Returns the assistant text on
    success, or None on any failure so the caller can fall back to ``claude -p``.

    Added in T-fix-decide-http: the legacy ``claude -p`` subprocess path hangs in
    headless / non-tty environments and can't honour the project's planner model
    selection (gpt-5.5 etc.). The HTTP path uses whatever transport is wired to
    the ``planner`` role in models.yaml.
    """
    if project_root is None:
        return None
    resolved = _resolve_planner_http_transport(project_root)
    if resolved is None:
        return None
    planner, transport = resolved
    creds = _planner_http_credentials(transport)
    if creds is None:
        return None
    base_url, api_key = creds
    return _post_planner_chat(base_url=base_url, api_key=api_key, model=planner.model, prompt=prompt)


def _claude_cli_helpers() -> tuple[Any, Any, Any]:
    """Resolve subprocess helpers; provide passthrough fallbacks on ImportError."""
    try:
        from kodawari.autopilot.core.subprocess_compat import subprocess_text_kwargs, windows_safe_command
        from kodawari.autopilot.planning.planning_agent import _resolved_executable
        return windows_safe_command, subprocess_text_kwargs, _resolved_executable
    except ImportError:
        return (
            lambda *args: list(args),
            lambda **kw: kw,
            lambda name, default="claude": name,
        )


def _run_claude_subprocess(prompt: str, *, project_root: Path | None):
    """Spawn ``claude -p`` once and return the CompletedProcess, or None on failure."""
    windows_safe_command, subprocess_text_kwargs, _resolved_executable = _claude_cli_helpers()
    claude_path = _resolved_executable("claude", default="claude")
    cmd = windows_safe_command(claude_path, "-p", "--output-format", "json", "--max-turns", "5")
    cwd = str(project_root.resolve()) if project_root else None
    try:
        return subprocess.run(
            cmd,
            **subprocess_text_kwargs(input=prompt, timeout=_PLANNER_TIMEOUT_SECONDS, cwd=cwd),
        )
    except subprocess.TimeoutExpired:
        logger.error("claude -p subprocess timed out after %ss", _PLANNER_TIMEOUT_SECONDS)
        return None
    except (FileNotFoundError, OSError) as exc:
        logger.error("Failed to invoke claude CLI: %s", exc)
        return None


def _planner_text_via_claude_subprocess(prompt: str, *, project_root: Path | None) -> str | None:
    """Legacy ``claude -p`` subprocess invocation. Kept as fallback only."""
    completed = _run_claude_subprocess(prompt, project_root=project_root)
    if completed is None:
        return None
    if completed.returncode != 0:
        logger.error("claude -p exited %s; stderr=%s", completed.returncode, completed.stderr[:300])
        return None
    try:
        wrapped = json.loads(completed.stdout)
        return str(wrapped.get("result") or "")
    except json.JSONDecodeError:
        return completed.stdout or ""


def run_decide_command(args: argparse.Namespace) -> int:
    planning_dir = Path(args.planning_dir) if hasattr(args, "planning_dir") and args.planning_dir else None
    if not planning_dir:
        logger.error("--planning-dir is required")
        return 1

    planning_dir = planning_dir.resolve()

    # --abort flag: write a marker so resume detects the abort and stops
    if getattr(args, "abort", False):
        return _handle_abort(planning_dir)
    # --status flag: list pending decision requests
    if getattr(args, "status", False):
        return _handle_status(planning_dir)

    # New unified path: detect any pending .{phase}_decision_request.json
    # and dispatch by EscalationKind. Returns 0/1 if handled here. Falls
    # through to the legacy gate_complexity-specific path below if no
    # unified request file is present (back-compat with older callers).
    handled = _try_unified_decide(planning_dir)
    if handled is not None:
        return handled

    if not planning_dir.exists():
        logger.error(f"Planning directory does not exist: {planning_dir}")
        return 1

    project_root = _detect_project_root(planning_dir)

    request_file = planning_dir / ".executor_redesign_request.json"
    if not request_file.exists():
        logger.error(f"No redesign request found at {request_file}")
        return 1

    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"Failed to read redesign request: {e}")
        return 1

    task_id = str(request.get("task_id", "")).strip()
    failure_summary = str(request.get("failure_summary", "")).strip()
    detector_hint = str(request.get("detector_hint", "")).strip()
    escalation_count = int(request.get("escalation_count", 0))

    print(f"\n=== Redesign Decision for {task_id} (escalation #{escalation_count}) ===")
    print(f"Detector: {detector_hint}")
    print(f"Failure: {failure_summary[:200]}...\n" if len(failure_summary) > 200 else f"Failure: {failure_summary}\n")

    # Pull task card context
    task_card = _load_task_card(planning_dir)
    files_to_change = list(task_card.get("files_to_change") or []) if task_card else []
    invariants = list(task_card.get("invariants") or []) if task_card else []

    # Extract violating function info from failure summary
    violation_info = _parse_complexity_violation(failure_summary)
    function_source = ""
    if violation_info and project_root:
        function_source = _read_function_source(
            project_root=project_root,
            file_path=violation_info.get("path", ""),
            function_name=violation_info.get("symbol", ""),
            line_hint=violation_info.get("line", 0),
        )

    # Call Planner to generate real options
    print("Calling Planner for redesign options (this may take 30-90s)...\n")
    options = _call_planner_for_options(
        task_id=task_id,
        detector_hint=detector_hint,
        failure_summary=failure_summary,
        violation_info=violation_info,
        function_source=function_source,
        invariants=invariants,
        files_to_change=files_to_change,
        project_root=project_root,
    )

    if not options:
        logger.warning("Planner returned no options, falling back to generic templates")
        options = _fallback_options(detector_hint)

    print(f"Planner generated {len(options)} option(s):\n")

    # Show dialog
    from kodawari.gui.redesign_chooser import show_redesign_dialog
    choice = show_redesign_dialog(options)
    print(f"\nUser selected: {choice.action}")

    # Build response payload
    response = {
        "schema_version": "execution.redesign_response.v1",
        "task_id": task_id,
        "action": choice.action,
    }
    chosen_description = ""
    chosen_title = ""

    if choice.action == "accept" and choice.option_index is not None:
        idx = choice.option_index
        if 0 <= idx < len(options):
            response["option_index"] = idx
            response["option"] = options[idx]
            chosen_description = str(options[idx].get("description") or "")
            chosen_title = str(options[idx].get("title") or "")
    elif choice.action == "custom":
        response["description"] = choice.custom_text
        chosen_description = choice.custom_text
        chosen_title = "Custom user-provided approach"
    elif choice.action == "skip":
        chosen_description = "User requested to skip this task."
        chosen_title = "Skip task"

    # Write response file (audit trail)
    response_file = planning_dir / ".executor_redesign_response.json"
    response_file.write_text(json.dumps(response, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote response: {response_file.name}")

    # If user chose to skip, do NOT generate a recovery card.
    # The autopilot resume logic will handle skip semantics separately.
    if choice.action == "skip":
        _mark_task_skipped(planning_dir, task_id)
        print(f"\nTask {task_id} marked for skip on next autopilot run.")
        return 0

    # Find the original task card for this task_id (e.g. TASK_CARD_T2.json).
    # We need the *original* card to build the recovery wrapper because
    # TASK_CARD_ACTIVE may have advanced to a later task by the time the user
    # decides — autopilot's cycle may have rotated the cursor forward.
    original_task_card = _find_original_task_card(planning_dir, task_id)
    base_card = original_task_card or task_card or {}

    # Build a recovery-wrapped task card injecting the chosen approach into
    # the recovery.must_fix and recovery.instructions fields. The card keeps
    # the original task_id so engine state stays consistent.
    new_active_card = _build_user_chosen_recovery_card(
        task_card=base_card,
        task_id=task_id,
        chosen_title=chosen_title,
        chosen_description=chosen_description,
        failure_summary=failure_summary,
        violation_info=violation_info,
    )

    # Write to three locations for robustness:
    # 1. .user_redesign_decision.json — primary sticky bridge; engine reads
    #    this on every startup to inject must_fix even if autopilot rewrites
    #    TASK_CARD_ACTIVE between rounds
    # 2. .execution_recovery_card.json — for the _resume_pending_executor_recovery_card path
    # 3. TASK_CARD_ACTIVE.json — initial ACTIVE card; autopilot may overwrite,
    #    which is why (1) exists as a sticky fallback
    decision_file = planning_dir / ".user_redesign_decision.json"
    decision_payload = {
        "schema_version": "execution.user_redesign_decision.v1",
        "task_id": task_id,
        "chosen_title": chosen_title,
        "chosen_description": chosen_description,
        "must_fix": list((new_active_card.get("recovery") or {}).get("must_fix") or []),
        "consumed_at": None,
    }
    decision_file.write_text(json.dumps(decision_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote sticky decision: {decision_file.name}")

    recovery_card_file = planning_dir / ".execution_recovery_card.json"
    recovery_card_file.write_text(json.dumps(new_active_card, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote recovery card: {recovery_card_file.name}")

    active_card_file = planning_dir / "TASK_CARD_ACTIVE.json"
    # Back up the previous ACTIVE card if it exists and points to a different task
    if active_card_file.exists():
        prev_active = json.loads(active_card_file.read_text(encoding="utf-8"))
        prev_task_id = str(prev_active.get("task_id") or "").strip()
        if prev_task_id and prev_task_id != task_id and not prev_task_id.startswith(task_id + "_"):
            backup = planning_dir / f"TASK_CARD_ACTIVE.before_redesign_{prev_task_id}.json"
            backup.write_text(json.dumps(prev_active, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Backed up previous ACTIVE card ({prev_task_id}) to: {backup.name}")
    active_card_file.write_text(json.dumps(new_active_card, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Updated TASK_CARD_ACTIVE.json → task {task_id} with chosen refactor approach")

    # Clear escalated state and rewind autopilot active_task to target this task
    _rewind_state_to_task(planning_dir, task_id)
    print("\nDecision applied. Run autopilot to execute the chosen refactor:")
    print(f"  kodawari autopilot --feature <name> --task-cycle ...")

    # Cleanup: remove the redesign_request (it's been consumed)
    request_file.unlink(missing_ok=True)

    return 0


def _find_original_task_card(planning_dir: Path, task_id: str) -> dict[str, Any] | None:
    """Find the original TASK_CARD_T{N}.json for the given task_id."""
    if not task_id:
        return None
    candidate = planning_dir / f"TASK_CARD_{task_id}.json"
    if candidate.exists():
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _detect_project_root(planning_dir: Path) -> Path | None:
    """Walk up from planning_dir to find project root (parent of 'planning/')."""
    cur = planning_dir
    for _ in range(5):
        if cur.parent.name == "planning":
            return cur.parent.parent
        if (cur / ".git").exists() or (cur / "pyproject.toml").exists():
            return cur
        cur = cur.parent
    return None


def _load_task_card(planning_dir: Path) -> dict[str, Any] | None:
    """Try TASK_CARD_ACTIVE.json first, fall back to .execution_recovery_card.json."""
    for fname in ["TASK_CARD_ACTIVE.json", ".execution_recovery_card.json"]:
        path = planning_dir / fname
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return None


def _parse_complexity_violation(failure_summary: str) -> dict[str, Any]:
    """Extract path, function name, complexity from a gate violation message.

    Example match:
      "backend/api/v1/services/gpt_enrichment_pipeline.py:
       Function tag_channel_content complexity 11 exceeds 10."
    """
    info: dict[str, Any] = {}
    # path: anything ending in .py before ":"
    path_match = re.search(r"([\w./\\-]+\.py)\s*:", failure_summary)
    if path_match:
        info["path"] = path_match.group(1).replace("\\", "/")
    # function name + complexity
    func_match = re.search(r"Function\s+(\w+)\s+complexity\s+(\d+)\s+exceeds\s+(\d+)", failure_summary)
    if func_match:
        info["symbol"] = func_match.group(1)
        info["actual"] = int(func_match.group(2))
        info["limit"] = int(func_match.group(3))
    return info


def _read_function_source(
    project_root: Path,
    file_path: str,
    function_name: str,
    line_hint: int = 0,
    context_lines: int = 80,
) -> str:
    """Read a window of source code around the violating function."""
    if not file_path or not function_name:
        return ""
    full_path = project_root / file_path
    if not full_path.exists():
        return ""
    try:
        all_lines = full_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    # Find def line for the function
    def_pattern = re.compile(rf"^\s*def\s+{re.escape(function_name)}\s*\(")
    def_line = -1
    for i, line in enumerate(all_lines):
        if def_pattern.match(line):
            def_line = i
            break
    if def_line < 0:
        return ""

    start = def_line
    end = min(len(all_lines), def_line + context_lines)
    return "\n".join(all_lines[start:end])


def _call_planner_for_options(
    *,
    task_id: str,
    detector_hint: str,
    failure_summary: str,
    violation_info: dict[str, Any],
    function_source: str,
    invariants: list[str],
    files_to_change: list[str],
    project_root: Path | None,
) -> list[dict[str, Any]]:
    """Invoke Planner (claude -p) to generate 2-3 redesign options.

    Returns a list of {title, description} dicts, or empty list on failure.
    """
    prompt = _build_planner_prompt(
        task_id=task_id,
        detector_hint=detector_hint,
        failure_summary=failure_summary,
        violation_info=violation_info,
        function_source=function_source,
        invariants=invariants,
        files_to_change=files_to_change,
    )

    # Primary: HTTP planner role (gpt-5.5 / mimo / deepseek, whatever models.yaml
    # wires to the ``planner`` role). Bypasses the claude -p subprocess that
    # hangs in headless harnesses.
    logger.info("Invoking Planner via HTTP role transport...")
    text = _call_planner_via_role(prompt, project_root=project_root)
    if text is None:
        # Fallback: legacy claude -p subprocess. Only useful when the operator
        # has not configured a HTTP planner transport.
        logger.info("HTTP planner unavailable; falling back to claude -p subprocess")
        text = _planner_text_via_claude_subprocess(prompt, project_root=project_root)
    if text is None:
        return []

    options = _extract_options_from_text(text)
    return options


def _build_planner_prompt(
    *,
    task_id: str,
    detector_hint: str,
    failure_summary: str,
    violation_info: dict[str, Any],
    function_source: str,
    invariants: list[str],
    files_to_change: list[str],
) -> str:
    symbol = violation_info.get("symbol", "")
    actual = violation_info.get("actual", "?")
    limit = violation_info.get("limit", "?")
    path = violation_info.get("path", "")
    limit_minus_2 = _planner_complexity_target(limit)

    inv_text = "\n".join(f"  - {item}" for item in invariants[:8]) if invariants else "  (none provided)"
    files_text = ", ".join(files_to_change) if files_to_change else "(none provided)"

    return f"""You are designing the refactor of a function that exceeds the cyclomatic-complexity gate. The executor (an LLM that writes Python code) has already failed to fix it through generic "extract a few helpers" hints — it kept ADDING helpers without REDUCING the main function. Your job is to give the executor a plan so specific it cannot misinterpret.

## Failure Context
- Task: {task_id}
- Detector: {detector_hint}
- File: {path}
- Function: {symbol}
- Current complexity: {actual}, gate limit: {limit}

## Invariants (must be preserved)
{inv_text}

## Files in scope
{files_text}

## Function source (excerpt)
```python
{function_source[:2500] if function_source else "(source not available)"}
```

## What the executor needs from you

Each option's `description` must include ALL FIVE of:

1. **Concrete helper list** — name each helper, its 1-line purpose, target complexity (each ≤ 5), and which lines/branches of the original it absorbs.
2. **New body sketch for the violating function** — pseudocode of the REPLACEMENT body, ≤ 12 lines, target complexity ≤ {limit_minus_2}. This is the WHOLE body — old branches must disappear, not coexist.
3. **Replace-don't-add rule** — explicitly say "remove the original conditional chain; do not keep it as a fallback path". The executor's failure mode is preserving old logic + adding helpers in parallel.
4. **Self-check step** — tell the executor to run an AST complexity check (or grep for nesting depth) on the rewritten file and verify every function ≤ {limit} before declaring done.
5. **Test command** — the exact pytest invocation that proves the refactor preserves behavior.

## Output

Output **JSON ONLY** (no prose, no markdown fences) matching this exact schema:

{{
  "options": [
    {{
      "title": "<short label, 5-10 words>",
      "description": "<multi-line refactor plan covering points 1-5 above. Be explicit — name functions, give pseudocode for the replacement body, state the hard complexity cap. Aim for 200-500 words.>"
    }}
  ]
}}

Provide 2-3 options. Each option must be a genuinely different refactor strategy (not paraphrases). Every option must drive every function in the file to complexity ≤ {limit}, with the main violator at ≤ {limit_minus_2}, while preserving the invariants above."""


def _planner_complexity_target(limit: Any) -> int:
    """Pick a stricter target than the gate so executor has headroom."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 8
    return max(4, n - 2)


def _extract_options_from_text(text: str) -> list[dict[str, Any]]:
    """Find and parse the {"options": [...]} JSON block in planner output."""
    if not text:
        return []

    # Try direct parse first
    candidates = [text]

    # Find {...} blocks
    brace_matches = re.findall(r"\{[^{}]*\"options\"[^{}]*\[[\s\S]*?\][^{}]*\}", text)
    candidates.extend(brace_matches)

    # Find ```json ... ``` fenced blocks
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)```", text)
    candidates.extend(fenced)

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("options"), list):
            options = []
            for item in data["options"]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                desc = str(item.get("description") or "").strip()
                if title and desc:
                    options.append({"title": title, "description": desc})
            if options:
                return options[:5]  # cap at 5 just in case
    return []


def _fallback_options(detector_hint: str) -> list[dict[str, Any]]:
    """Generic fallback if Planner is unavailable."""
    if detector_hint == "gate_complexity":
        return [
            {
                "title": "Extract internal logic into helper functions",
                "description": "Identify the inner loop/branch with the most logic and move it into a private helper function. Repeat until the main function's branch count drops below the gate limit.",
            },
            {
                "title": "Flatten with early returns",
                "description": "Replace nested if/else chains with guard clauses that return early on failure conditions, reducing nesting depth and combined branch complexity.",
            },
        ]
    return [
        {"title": "Simplify implementation", "description": "Refactor to reduce complexity and improve maintainability."},
    ]


def _build_user_chosen_recovery_card(
    *,
    task_card: dict[str, Any],
    task_id: str,
    chosen_title: str,
    chosen_description: str,
    failure_summary: str,
    violation_info: dict[str, Any],
) -> dict[str, Any]:
    """Build a recovery card embedding the user-chosen refactor approach.

    The engine's `_resume_pending_executor_recovery_card()` will load this and
    feed `recovery.must_fix` into the executor prompt.
    """
    # Use the violating file as the authoritative files_to_change.
    # TASK_CARD_ACTIVE may point to a different task's files (e.g. when the
    # active card was already rotated forward), so we override it with the
    # file that actually contains the gate violation.
    violation_path = str(violation_info.get("path") or "").strip()
    files_to_change: list[str] = []
    if violation_path:
        files_to_change.append(violation_path)
    for f in list(task_card.get("files_to_change") or []):
        if f and f not in files_to_change:
            files_to_change.append(f)

    invariants = list(task_card.get("invariants") or [])
    forbidden = list(task_card.get("forbidden_changes") or [])
    verify_cmd = str(task_card.get("verify_cmd") or "").strip()

    must_fix_text = (
        f"{failure_summary}\n\n"
        f"User-selected refactor approach: {chosen_title}\n"
        f"Instructions: {chosen_description}"
    )

    instructions = [
        f"Apply the user-selected refactor approach: {chosen_title}",
        chosen_description,
        "Preserve public behavior, function signature, return shape, exception behavior, and side effects.",
        "Do not change tests or unrelated source files for this recovery.",
    ]
    if verify_cmd:
        instructions.append(f"Run the original scoped verify command after the refactor: {verify_cmd}")

    # files_to_change must be <= 3 per task_card schema
    files_to_change = files_to_change[:3]

    # Ensure invariants is non-empty (schema requirement)
    if not invariants:
        invariants = ["Preserve public function signature and externally observable behavior."]

    # Preserve test_plan from original card; required by schema
    test_plan = str(task_card.get("test_plan") or "").strip()
    if not test_plan:
        test_plan = f"Run {verify_cmd}" if verify_cmd else "Re-run the scoped verify command."

    return {
        "schema_version": "contract_first.task_card.v1",
        "task_id": task_id,  # keep original task_id so TASK_GRAPH cursor matches
        "task_name": f"{task_card.get('task_name') or task_id} (user-selected redesign)",
        "why_this_layer": "User chose this refactor approach via kodawari decide.",
        "files_to_change": files_to_change,
        "new_files": [],
        "read_only_files": list(task_card.get("read_only_files") or []),
        "invariants": invariants,
        "test_plan": test_plan,
        "forbidden_changes": forbidden,
        "coverage_hints": list(task_card.get("coverage_hints") or []),
        "related_existing_tests": list(task_card.get("related_existing_tests") or []),
        "verify_cmd": verify_cmd,
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "user_redesign_accepted",
            "must_fix": [must_fix_text],
            "instructions": instructions,
            "gate_violations": [violation_info] if violation_info else [],
            "reason": f"User chose redesign approach: {chosen_title}",
            "user_chosen_title": chosen_title,
            "user_chosen_description": chosen_description,
        },
        "target_symbols": _build_target_symbols(violation_info),
        "requires": list(task_card.get("requires") or []),
    }


def _build_target_symbols(violation_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Build target_symbols entries that match the contract-first schema.

    Schema requires: file, kind, name. Extra metadata fields are allowed.
    """
    if not isinstance(violation_info, dict) or not violation_info.get("symbol"):
        return []
    return [
        {
            "file": str(violation_info.get("path") or ""),
            "kind": "function",
            "name": str(violation_info.get("symbol") or ""),
        }
    ]


def _clear_escalated_state(planning_dir: Path) -> None:
    """Reset autopilot state so resume can pick up the recovery card."""
    state_file = planning_dir / ".autopilot_state.json"
    if not state_file.exists():
        return
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    state["last_stage_status"] = "executor_redesign_accepted"
    state["last_error"] = None
    state.pop("task_claim", None)
    state["error_history"] = []
    state["error_events"] = []

    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _rewind_state_to_task(planning_dir: Path, task_id: str) -> None:
    """Rewind autopilot active_task to the given task and clear blockers.

    When the user chooses a redesign, we must point autopilot back at the
    task that escalated so it re-executes with the new refactor approach.
    If the task was prematurely moved into completed_tasks, remove it.
    """
    state_file = planning_dir / ".autopilot_state.json"
    if not state_file.exists():
        return
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    # Remove the task from completed_tasks (it shouldn't be there if it failed)
    completed = state.get("completed_tasks", [])
    completed = [t for t in completed if not t.startswith(f"{task_id}:") and t != task_id]
    state["completed_tasks"] = completed

    # Reset cycle/stage so autopilot starts fresh on this task
    state["current_stage"] = "INIT"
    state["cycle"] = 0
    state["active_task"] = None  # let autopilot re-select based on ACTIVE card
    state["last_stage_status"] = "executor_redesign_accepted"
    state["last_error"] = None
    state.pop("task_claim", None)
    state["error_history"] = []
    state["error_events"] = []

    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _mark_task_skipped(planning_dir: Path, task_id: str) -> None:
    """Record that the user chose to skip this task.

    A skip marker file is written; autopilot resume must read it and
    move the task into a skipped state before resuming.
    """
    marker = planning_dir / ".executor_skip_task.json"
    marker.write_text(json.dumps({"task_id": task_id, "skipped_by": "user"}, indent=2), encoding="utf-8")
    _clear_escalated_state(planning_dir)


# ---------------------------------------------------------------------------
# Unified escalation dispatcher (v2 — handles all EscalationKind values)
# ---------------------------------------------------------------------------


def _try_unified_decide(planning_dir: Path) -> int | None:
    """If a unified .{phase}_decision_request.json exists, handle it.

    Returns:
        0 / 1: handled with exit code
        None: no unified request found, caller should fall through to legacy
              .executor_redesign_request.json path
    """
    try:
        from kodawari.autopilot.escalation import (
            DecisionResponse,
            EscalationKind,
            build_planner_prompt,
            write_decision_response,
        )
        from kodawari.autopilot.escalation.handler import find_pending_request
    except ImportError:
        return None

    pending = find_pending_request(planning_dir)
    if pending is None:
        return None
    phase, req = pending

    print(f"\n=== Decision needed: {req.escalation_kind} (phase={phase}, count={req.escalation_count}) ===")
    print(f"Feature: {req.feature}")
    if req.task_id:
        print(f"Task: {req.task_id}")
    print(f"Failure: {req.failure_summary[:200]}{'...' if len(req.failure_summary) > 200 else ''}\n")

    try:
        kind = EscalationKind(req.escalation_kind)
    except ValueError:
        logger.error(f"Unknown EscalationKind: {req.escalation_kind!r}")
        return 1

    # GATE_REFACTOR_NEEDED (formerly gate_complexity) → reuse legacy path
    # which has working source-extraction + recovery card writing logic.
    # Other kinds use the new minimal flow: planner-prompt → user-choice → write response.
    if kind == EscalationKind.GATE_REFACTOR_NEEDED:
        # Fall through to legacy path (which still reads .executor_redesign_request.json
        # written by the legacy_compat mirror in handler.py).
        return None

    # Call Planner with kind-specific prompt
    options = _call_planner_for_kind(
        kind=kind,
        request=req,
        planning_dir=planning_dir,
    )
    if not options:
        logger.warning(f"Planner returned no options for {kind.value}; using fallback")
        options = [{"title": "Skip this escalation", "description": "Proceed without applying any specific approach."}]

    print(f"Planner generated {len(options)} option(s):\n")

    # Show CLI dialog (skip GUI for unified path; can extend later)
    from kodawari.gui.redesign_chooser import show_redesign_dialog
    choice = show_redesign_dialog(options)
    print(f"\nUser action: {choice.action}")

    # Build response
    chosen_title = ""
    chosen_description = ""
    response = DecisionResponse(
        phase=phase,
        escalation_kind=kind.value,
        action=choice.action,
    )
    if choice.action == "accept" and choice.option_index is not None:
        idx = choice.option_index
        if 0 <= idx < len(options):
            response.option_index = idx
            response.option = options[idx]
            chosen_title = str(options[idx].get("title") or "")
            chosen_description = str(options[idx].get("description") or "")
    elif choice.action == "custom":
        response.description = choice.custom_text
        chosen_description = choice.custom_text
        chosen_title = "Custom user-provided approach"
    elif choice.action == "skip":
        chosen_title = "Skip"
        chosen_description = "User opted to skip this escalation."

    # Write response file
    write_decision_response(planning_dir, phase, response)
    print(f"Wrote decision response: .{phase}_decision_response.json")

    # If this is a planning split, also persist .planning_split_proposal.json
    if kind == EscalationKind.PLANNING_DEADLOCK and choice.action == "accept" and isinstance(response.option, dict):
        sub_features = response.option.get("sub_features") or []
        if sub_features:
            split_proposal = {
                "schema_version": "workflow.split_proposal.v1",
                "parent_feature": req.feature,
                "parent_split_depth": req.current_split_depth,
                "sub_features": sub_features,
                "topological_validated": True,
                "max_depth_check": {"current": req.current_split_depth, "limit": req.max_split_depth, "allowed": req.current_split_depth < req.max_split_depth},
            }
            split_path = planning_dir / ".planning_split_proposal.json"
            split_path.write_text(json.dumps(split_proposal, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"Wrote split proposal: {split_path.name} with {len(sub_features)} sub-feature(s)")

    # Remove the request file (consumed)
    req_path = planning_dir / f".{phase}_decision_request.json"
    try:
        req_path.unlink()
    except OSError:
        pass

    print(f"\nDecision applied for {kind.value}: {chosen_title}")
    print("Run autopilot again to resume.")
    return 0


def _call_planner_for_kind(
    *,
    kind: Any,
    request: Any,
    planning_dir: Path,
) -> list[dict[str, Any]]:
    """Call Planner (claude CLI) with kind-specific prompt; return options."""
    try:
        from kodawari.autopilot.escalation import build_planner_prompt
    except ImportError:
        return []

    project_root = _detect_project_root(planning_dir)
    prompt = build_planner_prompt(
        kind=kind,
        failure_summary=request.failure_summary,
        feature=request.feature,
        task_id=request.task_id,
        context=request.context,
    )
    text = _call_planner_via_role(prompt, project_root=project_root)
    if text is None:
        text = _planner_text_via_claude_subprocess(prompt, project_root=project_root)
    if text is None:
        return []
    return _extract_options_from_text(text)


def _handle_abort(planning_dir: Path) -> int:
    """Mark all pending decision requests as aborted."""
    aborted = []
    for phase in ("planning", "executor", "gate"):
        req_path = planning_dir / f".{phase}_decision_request.json"
        if req_path.exists():
            (planning_dir / f".{phase}_decision_aborted.marker").write_text(
                f"aborted_at={Path(req_path).stat().st_mtime}\n", encoding="utf-8"
            )
            try:
                req_path.unlink()
                aborted.append(phase)
            except OSError:
                pass
    if aborted:
        print(f"Aborted pending decision request(s): {aborted}")
    else:
        print("No pending decision requests to abort.")
    return 0


def _handle_status(planning_dir: Path) -> int:
    """List pending decision requests + their counts."""
    from kodawari.autopilot.escalation.handler import (
        escalation_count,
        find_pending_request,
        request_filename,
    )
    print(f"Planning dir: {planning_dir}")
    found = False
    for phase in ("planning", "executor", "gate"):
        req_path = planning_dir / request_filename(phase)
        count = escalation_count(planning_dir, phase)
        if req_path.exists():
            print(f"  [{phase}] PENDING — escalation count {count}/2")
            found = True
        elif count > 0:
            print(f"  [{phase}] no pending (last count {count}/2)")
    if not found:
        print("  No pending decision requests.")
    return 0


def _auto_decide_disabled() -> bool:
    return os.environ.get("WORKFLOW_AUTO_DECIDE", "1").strip().lower() in {
        "0",
        "false",
        "off",
        "no",
    }


def _load_pending_redesign_request(planning_dir: Path) -> dict[str, Any] | None:
    """Read .executor_redesign_request.json; return None when missing/invalid."""
    request_file = planning_dir / ".executor_redesign_request.json"
    if not request_file.exists():
        logger.info("auto_decide_pending: no .executor_redesign_request.json present")
        return None
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("auto_decide_pending: cannot read request: %s", exc)
        return None
    if not str(request.get("task_id") or "").strip():
        logger.warning("auto_decide_pending: request missing task_id")
        return None
    if not str(request.get("failure_summary") or "").strip():
        logger.warning("auto_decide_pending: request missing failure_summary")
        return None
    return request


def _violation_context(
    failure_summary: str, project_root: Path | None
) -> tuple[dict[str, Any], str]:
    """Parse the complexity violation + read the offending function source."""
    violation_info = _parse_complexity_violation(failure_summary) or {}
    if not violation_info or project_root is None:
        return violation_info, ""
    function_source = _read_function_source(
        project_root=project_root,
        file_path=violation_info.get("path", ""),
        function_name=violation_info.get("symbol", ""),
        line_hint=violation_info.get("line", 0),
    )
    return violation_info, function_source


def _planner_options_for_request(
    *,
    request: dict[str, Any],
    planning_dir: Path,
    project_root: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """Resolve planner options + violation context for a pending request."""
    task_id = str(request["task_id"]).strip()
    failure_summary = str(request["failure_summary"]).strip()
    detector_hint = str(request.get("detector_hint") or "").strip()

    task_card = _load_task_card(planning_dir) or {}
    invariants = list(task_card.get("invariants") or [])
    files_to_change = list(task_card.get("files_to_change") or [])

    violation_info, function_source = _violation_context(failure_summary, project_root)

    logger.info("auto_decide_pending: calling HTTP planner for %s", task_id)
    options = _call_planner_for_options(
        task_id=task_id,
        detector_hint=detector_hint,
        failure_summary=failure_summary,
        violation_info=violation_info,
        function_source=function_source,
        invariants=invariants,
        files_to_change=files_to_change,
        project_root=project_root,
    )
    if not options:
        logger.warning("auto_decide_pending: planner returned no options; using fallback")
        options = _fallback_options(detector_hint)
    return options, violation_info, failure_summary


def _write_auto_decide_artifacts(
    *,
    planning_dir: Path,
    task_id: str,
    chosen: dict[str, Any],
    chosen_title: str,
    chosen_description: str,
    new_active_card: dict[str, Any],
) -> None:
    """Write the four decision artifacts that ``apply_pending_resume`` consumes."""
    (planning_dir / ".executor_redesign_response.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.redesign_response.v1",
                "task_id": task_id,
                "action": "accept",
                "option_index": 0,
                "option": chosen,
                "auto_accepted_via": "auto_decide_pending",
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (planning_dir / ".user_redesign_decision.json").write_text(
        json.dumps(
            {
                "schema_version": "execution.user_redesign_decision.v1",
                "task_id": task_id,
                "chosen_title": chosen_title,
                "chosen_description": chosen_description,
                "must_fix": list((new_active_card.get("recovery") or {}).get("must_fix") or []),
                "consumed_at": None,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    card_json = json.dumps(new_active_card, indent=2, ensure_ascii=False)
    (planning_dir / ".execution_recovery_card.json").write_text(card_json, encoding="utf-8")
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(card_json, encoding="utf-8")
    (planning_dir / ".executor_decision_response.json").write_text(
        json.dumps(
            {
                "schema_version": "workflow.decision_response.v1",
                "phase": "executor",
                "escalation_kind": "GATE_REFACTOR_NEEDED",
                "action": "accept",
                "option_index": 0,
                "option": chosen,
                "description": chosen_description,
                "auto_accepted_via": "auto_decide_pending",
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def auto_decide_pending(
    planning_dir: Path,
    *,
    project_root: Path | None = None,
) -> bool:
    """Auto-accept option 0 from the HTTP planner for any pending decision request.

    Called by the engine recovery mixin right after ``maybe_escalate`` writes a
    request file. Lets the autopilot stay fully autonomous on gate-complexity
    failures: planner designs the refactor → response is written → next
    autopilot run picks up the recovery card via ``apply_pending_resume``.

    Returns True when a response was written, False when the request was
    missing, the planner returned no usable options, or auto-decide is disabled
    by the ``WORKFLOW_AUTO_DECIDE=0`` env var.
    """
    if _auto_decide_disabled():
        logger.info("auto_decide_pending: disabled via WORKFLOW_AUTO_DECIDE=0")
        return False

    planning_dir = Path(planning_dir)
    if project_root is None:
        project_root = _detect_project_root(planning_dir)

    request = _load_pending_redesign_request(planning_dir)
    if request is None:
        return False
    task_id = str(request["task_id"]).strip()

    options, violation_info, failure_summary = _planner_options_for_request(
        request=request,
        planning_dir=planning_dir,
        project_root=project_root,
    )
    if not options:
        return False

    chosen = options[0]
    chosen_title = str(chosen.get("title") or "Auto-accepted planner option")
    chosen_description = str(chosen.get("description") or "")

    original_task_card = _find_original_task_card(planning_dir, task_id) or _load_task_card(planning_dir) or {}
    new_active_card = _build_user_chosen_recovery_card(
        task_card=original_task_card,
        task_id=task_id,
        chosen_title=chosen_title,
        chosen_description=chosen_description,
        failure_summary=failure_summary,
        violation_info=violation_info,
    )

    _write_auto_decide_artifacts(
        planning_dir=planning_dir,
        task_id=task_id,
        chosen=chosen,
        chosen_title=chosen_title,
        chosen_description=chosen_description,
        new_active_card=new_active_card,
    )
    _rewind_state_to_task(planning_dir, task_id)
    (planning_dir / ".executor_redesign_request.json").unlink(missing_ok=True)
    logger.info(
        "auto_decide_pending: accepted option[0] (%s) for %s; recovery card written",
        chosen_title[:80],
        task_id,
    )
    return True


__all__ = ["run_decide_command", "auto_decide_pending"]

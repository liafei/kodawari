"""Kind-specific Planner prompt templates for ``kodawari decide``.

Each ``EscalationKind`` maps to a focused prompt that asks the Planner
LLM to generate 2-3 concrete options the user can pick from. The
prompt includes:

- The failure summary
- Any kind-specific context (round counts, failed files, etc.)
- Strict JSON output schema for parsing

Kept simple — each prompt is plain text with embedded values, no
templating engine. Easy to tune per kind.
"""

from __future__ import annotations

from typing import Any

from kodawari.autopilot.escalation.kinds import EscalationKind


_JSON_SCHEMA_BLOCK = """Output **JSON ONLY** (no prose, no markdown fences) matching:
{
  "options": [
    {"title": "<short label>", "description": "<actionable instruction, 1-3 sentences>"}
  ]
}
Provide 2-3 options. Each must be a genuinely different strategy."""


def build_planner_prompt(
    kind: EscalationKind,
    *,
    failure_summary: str = "",
    feature: str = "",
    task_id: str = "",
    context: dict[str, Any] | None = None,
    function_source: str = "",
    invariants: list[str] | None = None,
) -> str:
    """Build a kind-specific Planner prompt."""
    ctx = dict(context or {})
    invariant_text = ""
    if invariants:
        invariant_text = "\n## Invariants\n" + "\n".join(f"  - {x}" for x in invariants[:6])

    if kind == EscalationKind.GATE_REFACTOR_NEEDED:
        return _gate_refactor_prompt(
            failure_summary=failure_summary,
            feature=feature,
            task_id=task_id,
            context=ctx,
            function_source=function_source,
            invariant_text=invariant_text,
        )

    if kind == EscalationKind.GATE_FILE_SPLIT_NEEDED:
        return _gate_file_split_prompt(
            failure_summary=failure_summary,
            feature=feature,
            task_id=task_id,
            context=ctx,
            invariant_text=invariant_text,
        )

    if kind == EscalationKind.GATE_TASK_CARD_DESIGN_BUG:
        return _task_card_design_prompt(
            failure_summary=failure_summary,
            feature=feature,
            task_id=task_id,
            context=ctx,
        )

    if kind == EscalationKind.PLANNING_APPROVAL_REQUIRED:
        return _planning_approval_prompt(
            failure_summary=failure_summary,
            feature=feature,
            context=ctx,
        )

    if kind == EscalationKind.PLANNING_DEADLOCK:
        return _planning_deadlock_prompt(
            failure_summary=failure_summary,
            feature=feature,
            context=ctx,
        )

    if kind == EscalationKind.PLANNING_PREREQ_MISSING:
        return _planning_prereq_prompt(
            failure_summary=failure_summary,
            feature=feature,
            context=ctx,
        )

    if kind == EscalationKind.PLANNING_ENV_FAIL:
        return _planning_env_fail_prompt(
            failure_summary=failure_summary,
            feature=feature,
            context=ctx,
        )

    if kind == EscalationKind.EXECUTOR_STUCK:
        return _executor_stuck_prompt(
            failure_summary=failure_summary,
            feature=feature,
            task_id=task_id,
            context=ctx,
            invariant_text=invariant_text,
        )

    if kind == EscalationKind.EXECUTOR_PATCH_BROKEN:
        return _executor_patch_broken_prompt(
            failure_summary=failure_summary,
            task_id=task_id,
            context=ctx,
        )

    if kind == EscalationKind.EXECUTOR_PRECONDITION_MISSING:
        return _executor_precondition_prompt(
            failure_summary=failure_summary,
            task_id=task_id,
            context=ctx,
        )

    if kind == EscalationKind.EXECUTOR_MODEL_INCAPABLE:
        return _executor_model_incapable_prompt(
            failure_summary=failure_summary,
            task_id=task_id,
            context=ctx,
        )

    if kind == EscalationKind.COMPLIANCE_BLOCK:
        return _compliance_block_prompt(failure_summary=failure_summary)

    if kind == EscalationKind.INFRA_INTERRUPTION:
        return _infra_interruption_prompt(failure_summary=failure_summary, context=ctx)

    # default fallback
    return (
        f"Escalation kind: {kind.value}\nFailure summary: {failure_summary}\n\n{_JSON_SCHEMA_BLOCK}"
    )


# --- per-kind templates ----------------------------------------------------

def _gate_refactor_prompt(*, failure_summary, feature, task_id, context, function_source, invariant_text):
    return f"""You are reviewing a code-quality gate violation. The executor's automatic
recovery rounds could not bring the flagged function below the gate limit.
Generate 2-3 distinct, concrete refactor approaches a junior engineer could
implement immediately.

## Failure
Task: {task_id} (feature: {feature})
Detector: {context.get('detector_hint', 'unknown')}
Path: {context.get('path', '?')}, Symbol: {context.get('symbol', '?')}
Current metric: {context.get('actual', '?')}, Gate limit: {context.get('limit', '?')}

{invariant_text}

## Function source (excerpt)
```python
{function_source[:2500] if function_source else "(source not available)"}
```

## Your Task
{_JSON_SCHEMA_BLOCK}

Each option must name specific helpers to extract or branches to flatten.
Focus on reducing the flagged metric below the gate limit while preserving
the listed invariants. Failure summary: {failure_summary[:200]}"""


def _gate_file_split_prompt(*, failure_summary, feature, task_id, context, invariant_text):
    return f"""A file exceeds the gate's size/complexity sum threshold. The fix
requires splitting the file across multiple modules.

## Failure
Task: {task_id} (feature: {feature})
Path: {context.get('path', '?')}
Lines: {context.get('lines', '?')}, Complexity sum: {context.get('complexity_sum', '?')}
Gate limit: file_lines={context.get('file_lines_limit', 1500)}, complexity_sum={context.get('complexity_sum_limit', 30)}

{invariant_text}

## Your Task
Suggest 2-3 ways to split this file. Each option names the new module(s)
and which functions/classes move into them. Preserve all public exports.

{_JSON_SCHEMA_BLOCK}

Failure summary: {failure_summary[:200]}"""


def _task_card_design_prompt(*, failure_summary, feature, task_id, context):
    return f"""Gate flagged a scope/import-rule violation. This usually means the
task_card's files_to_change or requires fields are wrong — the executor
tried to modify the right file but the task card didn't authorize it
(or the task should depend on a prerequisite that wasn't run).

## Failure
Task: {task_id} (feature: {feature})
Gate violation: {context.get('violation', '?')}
Disallowed path: {context.get('path', '?')}

## Your Task
Suggest 2-3 fixes:
1. Adjust task_card.files_to_change to include the needed path
2. Insert a prerequisite task
3. Move the change to a different existing task in the graph

{_JSON_SCHEMA_BLOCK}

Failure summary: {failure_summary[:200]}"""


def _planning_approval_prompt(*, failure_summary, feature, context):
    history = context.get("blocking_findings_history") or []
    tasks_count = context.get("last_plan_tasks_count", "?")
    return f"""The planning conversation converged with 0 blocking reviewer findings
on the final round. The plan is ready but config decision_policy requires
explicit user approval before executing.

## Plan context
Feature: {feature}
Rounds used: {context.get('round_count', '?')}
Blocking-findings history: {history}
Tasks in final plan: {tasks_count}

## Your Task
The plan is already approved by reviewer. Generate 2-3 user-facing options:

1. Accept the plan as-is and start execution
2. Manually edit the plan before execution (user describes changes)
3. Split into sub-features anyway (if user wants finer control)

Output JSON:
{{
  "options": [
    {{"title": "Accept current plan and execute",
      "description": "Plan reviewed clean. Proceed to execute all {tasks_count} task(s) without modification."}},
    {{"title": "Manual revision before execute",
      "description": "User will edit TASK_GRAPH.json or TASK_CARD_*.json before resume."}},
    {{"title": "Split into independent sub-features",
      "description": "Treat plan as too coarse — break into smaller features for staged delivery."}}
  ]
}}"""


def _planning_deadlock_prompt(*, failure_summary, feature, context):
    findings_history = context.get("blocking_findings_history") or []
    round_count = context.get("round_count", 0)
    return f"""The planner-reviewer loop did not converge after {round_count} rounds.
Reviewer findings count history: {findings_history}. The task is likely
too large to be planned as a single feature.

## Failure context
Feature: {feature}
Failure summary: {failure_summary[:300]}

## Your Task
Propose 2-3 ways to split this feature into smaller sub-features. Each
sub-feature must be:
- self-contained (one concern: schema migration / data ingestion / API / UI)
- have a clear depends_on order on other sub-features
- be small enough to plan in 3-5 rounds

Output JSON in this shape:
{{
  "options": [
    {{
      "title": "Split into N sub-features",
      "description": "<rationale>",
      "sub_features": [
        {{"name": "feature_name", "depends_on": [], "task_summary": "...",
          "approximate_task_count": 3}}
      ]
    }}
  ]
}}

Provide 2-3 split options."""


def _planning_prereq_prompt(*, failure_summary, feature, context):
    missing = context.get("missing_surfaces") or context.get("missing_preconditions") or []
    return f"""The planner determined this feature cannot be implemented as-is
because prerequisite work is missing.

## Failure context
Feature: {feature}
Missing surfaces / preconditions: {missing}
Failure summary: {failure_summary[:300]}

## Your Task
Propose 2-3 ways to address the missing prerequisites. Each option may:
1. Insert a prerequisite feature to run first
2. Modify the PRD to scope around the missing pieces
3. Use an alternative existing surface

{_JSON_SCHEMA_BLOCK}"""


def _planning_env_fail_prompt(*, failure_summary, feature, context):
    run_reason = context.get("run_reason", "")
    return f"""Planner environment failed (transport/timeout/output truncation):
{run_reason}. This is usually a model or gateway capacity issue, not a
task-design problem.

## Failure context
Feature: {feature}
Failure: {failure_summary[:300]}

## Your Task
Propose 2-3 mitigations:
1. Switch to a different planner model/transport (specify which)
2. Reduce planning context size via WORKFLOW_PLANNER_CONTEXT_MAX_CHARS
3. Retry as-is (transient outage)

{_JSON_SCHEMA_BLOCK}"""


def _executor_stuck_prompt(*, failure_summary, feature, task_id, context, invariant_text):
    return f"""The executor's recovery loop is exhausted on this task. The model
appears unable to make progress (read loop / no-write / repeated stalls).

## Failure
Task: {task_id} (feature: {feature})
Failure code: {context.get('failure_code', '?')}
{invariant_text}

## Your Task
Propose 2-3 ways to unstuck this task:
1. Switch to a stronger executor model
2. Narrow the task scope (split this task into 2)
3. Manual implementation hint (specific code to inject as must_fix)

{_JSON_SCHEMA_BLOCK}

Failure summary: {failure_summary[:200]}"""


def _executor_patch_broken_prompt(*, failure_summary, task_id, context):
    return f"""The task_card.patch_plan is missing or applies-then-fails repeatedly.
The patch plan likely references stale file content or wrong line ranges.

## Failure
Task: {task_id}
Failure: {failure_summary[:200]}

## Your Task
Propose 2-3 fixes:
1. Rebuild patch_plan from current file content
2. Remove patch_plan and fall back to str_replace
3. Update task_card.files_to_change scope

{_JSON_SCHEMA_BLOCK}"""


def _executor_precondition_prompt(*, failure_summary, task_id, context):
    missing = context.get("missing_preconditions") or []
    return f"""The executor declared this task infeasible: required preconditions
missing. The plan graph needs adjustment.

## Failure
Task: {task_id}
Missing: {missing}
Failure: {failure_summary[:200]}

## Your Task
Propose 2-3 plan-graph adjustments:
1. Insert a prerequisite task that provides the missing precondition
2. Update task_card.requires to point at an existing task
3. Reframe this task to not need the missing precondition

{_JSON_SCHEMA_BLOCK}"""


def _executor_model_incapable_prompt(*, failure_summary, task_id, context):
    return f"""The executor model is repeatedly hitting cache-hit reads without
making progress. This is a model capability gap, not a workflow bug.

## Failure
Task: {task_id}
wasted_read_count: {context.get('wasted_read_count', '?')}

## Your Task
Propose 2-3 mitigations:
1. Switch to a stronger model (suggest specific model name)
2. Provide manual code hint (must_fix)
3. Skip task and document in DELIVERY_REPORT

{_JSON_SCHEMA_BLOCK}

Failure summary: {failure_summary[:200]}"""


def _compliance_block_prompt(*, failure_summary):
    return f"""A compliance check blocked the task. This must be human-resolved.
Auto-options not provided.

Failure: {failure_summary[:400]}

Output:
{{
  "options": [
    {{"title": "Resolve compliance violation manually",
      "description": "User must inspect the violation and decide whether to fix code, change policy, or seek waiver."}}
  ]
}}"""


def _infra_interruption_prompt(*, failure_summary, context):
    return f"""Infrastructure interruption (worktree conflict / state lock /
Ctrl+C). State should be recoverable from snapshot.

Context: {context}
Failure: {failure_summary[:200]}

## Your Task
Propose 2-3 recovery actions:
1. Resume from last successful snapshot
2. Reset state and restart current task
3. Manual investigation (specify what to check)

{_JSON_SCHEMA_BLOCK}"""


__all__ = ["build_planner_prompt"]

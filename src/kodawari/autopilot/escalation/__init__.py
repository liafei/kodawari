"""Unified escalation system for kodawari autopilot.

When a failure mode is beyond kodawari's automatic recovery (LLM
reasoning ceiling, task-too-large, model-incapable, gate violations that
need design changes, planning-phase deadlock, etc.), the failure is
classified into one of ~12 ``EscalationKind`` values and surfaced to the
user via ``kodawari decide``. The user picks an option, the response
file is consumed on next autopilot resume, and the workflow continues.

Public API:
- :class:`EscalationKind` — enum of all escalation categories
- :func:`classify` — map ``failure_event`` + phase → ``EscalationKind`` or None
- :func:`maybe_escalate` — top-level entry; writes ``.{phase}_decision_request.json``
- :func:`read_decision_response` — read user's choice
- :func:`escalation_count` — current count for ``phase``
- :func:`build_planner_prompt` — kind-specific Planner prompt template
"""

from kodawari.autopilot.escalation.kinds import EscalationKind, classify
from kodawari.autopilot.escalation.handler import (
    DecisionRequest,
    DecisionResponse,
    escalation_count,
    maybe_escalate,
    read_decision_response,
    write_decision_response,
)
from kodawari.autopilot.escalation.planner_prompts import build_planner_prompt

__all__ = [
    "DecisionRequest",
    "DecisionResponse",
    "EscalationKind",
    "build_planner_prompt",
    "classify",
    "escalation_count",
    "maybe_escalate",
    "read_decision_response",
    "write_decision_response",
]

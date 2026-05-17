"""Tests for _compact_previous_findings (B-1: Mimo planner round-2+ timeout fix).

Real-world large-PRD planning produced 24 blocking findings in round 1.
Round 2's planner prompt = 60K context + 12K of unfiltered findings JSON +
8K previous_plan + 5K schema/preamble = ~93K chars. Mimo HTTP planner
hard-times out at 120s on prompts of that size, so the round 2 retry
(after Mimo round 1 also fell back to chat mode) failed and the run
escalated as planner_transport_timeout.

This compaction caps the findings JSON to bounded content:
  * top 8 (high/critical/blocking severity)
  * description ≤ 200 chars, recommendation ≤ 150 chars
  * already-demoted findings are filtered out (planner has nothing to revise)
  * a tail hint records how many were truncated, so the model is told that
    un-addressed concerns will be re-flagged next round rather than silently
    dropped.
"""

from __future__ import annotations

import json

from kodawari.autopilot.planning.planning_agent import (
    _compact_previous_findings,
)


def _finding(
    severity: str = "blocking",
    *,
    category: str = "scope",
    description: str = "missing test for service layer change",
    recommendation: str = "add scoped test before execution",
    extras: dict | None = None,
) -> dict:
    item = {
        "severity": severity,
        "category": category,
        "description": description,
        "recommendation": recommendation,
    }
    if extras:
        item.update(extras)
    return item


def test_compact_returns_empty_for_no_findings() -> None:
    assert _compact_previous_findings(None) == []
    assert _compact_previous_findings([]) == []


def test_compact_drops_demoted_findings() -> None:
    """severity_demoted=True means deterministic_repair already auto-fixed
    that field; planner has nothing to revise, drop the finding."""
    findings = [
        _finding(extras={"severity_demoted": True, "demoted_reason": "deterministic_repair_already_applied:add_missing_task_change_log_entry"}),
        _finding(),
    ]

    out = _compact_previous_findings(findings)

    # Only one non-demoted finding survives.
    assert len(out) == 1
    assert out[0]["category"] == "scope"


def test_compact_filters_by_severity() -> None:
    """Only blocking/critical/high-severity findings remain in the planner
    prompt; medium/info/low are advisory and surfaced via review_focus."""
    findings = [
        _finding(severity="blocking", category="scope"),
        _finding(severity="critical", category="security"),
        _finding(severity="high", category="completeness"),
        _finding(severity="medium", category="style"),
        _finding(severity="info", category="docs"),
        _finding(severity="low", category="naming"),
    ]

    out = _compact_previous_findings(findings)

    severities = {entry["severity"] for entry in out}
    assert severities == {"blocking", "critical", "high"}


def test_compact_caps_to_max_items_and_emits_truncation_tail() -> None:
    """When more findings than the prompt budget allows, truncate to top N
    and append a hint so the model knows the reviewer will re-flag the
    rest if they go unaddressed.

    The default budget (24) is generous so realistic plan-review rounds
    keep every finding the reviewer raised; this test forces a tail by
    explicitly passing max_items=8."""
    findings = [_finding() for _ in range(20)]

    out = _compact_previous_findings(findings, max_items=8)

    # 8 capped + 1 truncation tail hint.
    assert len(out) == 9
    assert out[-1]["category"] == "_truncation_tail"
    assert "12 additional finding" in out[-1]["description"]
    assert out[-1]["severity"] == "info"


def test_default_budget_keeps_realistic_blocker_set_intact() -> None:
    """Realistic plan-review rounds raise up to ~24 blockers. The default
    budget must keep all of them so the planner sees the full surface
    rather than fixing top-N and getting the rest re-flagged next round."""
    findings = [_finding() for _ in range(24)]

    out = _compact_previous_findings(findings)

    assert len(out) == 24
    # No truncation hint when within budget.
    assert all(entry["category"] != "_truncation_tail" for entry in out)


def test_compact_does_not_emit_tail_when_within_budget() -> None:
    findings = [_finding() for _ in range(3)]

    out = _compact_previous_findings(findings)

    assert len(out) == 3
    assert all(entry["category"] != "_truncation_tail" for entry in out)


def test_compact_truncates_long_description_and_recommendation() -> None:
    long_desc = "X" * 500
    long_rec = "Y" * 500
    findings = [_finding(description=long_desc, recommendation=long_rec)]

    out = _compact_previous_findings(findings)

    # Description capped at 180 chars (incl. ellipsis), recommendation at 120.
    assert len(out[0]["description"]) <= 180
    assert len(out[0]["recommendation"]) <= 120
    assert out[0]["description"].endswith("…")
    assert out[0]["recommendation"].endswith("…")


def test_compact_preserves_short_description_verbatim() -> None:
    short_desc = "scoped test missing"
    findings = [_finding(description=short_desc, recommendation="add it")]

    out = _compact_previous_findings(findings)

    assert out[0]["description"] == short_desc
    assert out[0]["recommendation"] == "add it"


def test_compact_includes_task_id_when_present() -> None:
    findings = [_finding(extras={"task_id": "T1"})]

    out = _compact_previous_findings(findings)

    assert out[0]["task_id"] == "T1"


def test_compact_omits_task_id_when_absent() -> None:
    out = _compact_previous_findings([_finding()])
    assert "task_id" not in out[0]


def test_compact_real_world_size_reduction() -> None:
    """Simulates the failing run-005 case: 24 blocking findings + 1 high
    + 1 medium (filtered) + 1 demoted (filtered).

    The raw JSON of 24 long-description findings was ~12K chars, pushing
    Mimo's round-2 prompt past 120s. With per-field truncation the
    rendered JSON drops well under that ceiling while keeping every
    eligible finding the planner needs to revise."""
    findings = [
        _finding(
            description="Task X must include scoped test for Y" * 5,
            recommendation="Add scoped test before execution",
        )
        for _ in range(24)
    ]
    findings.append(_finding(severity="high", description="should consider Z"))
    findings.append(_finding(severity="medium", description="minor style"))
    findings.append(_finding(extras={"severity_demoted": True, "category": "change_log"}))

    out = _compact_previous_findings(findings)
    rendered = json.dumps(out, ensure_ascii=False, indent=2)

    # 24 blocking + 1 high (medium + demoted filtered) = 25 eligible. With
    # default budget=24, top 24 kept + 1 truncation tail = 25 entries.
    assert len(out) == 25
    assert out[-1]["category"] == "_truncation_tail"
    # Per-field truncation keeps the rendered JSON well under raw 12K size.
    # Empirically 25 entries × ~320 chars per JSON entry ≈ 8K — comfortably
    # below the round-2 timeout cliff (~80K total prompt).
    assert len(rendered) < 9000
    # Verify all retained descriptions are within the 180-char cap.
    for entry in out[:-1]:
        assert len(entry["description"]) <= 180
        assert len(entry["recommendation"]) <= 120


def test_non_dict_finding_entries_skipped() -> None:
    findings = [_finding(), "not a dict", 42, _finding(category="x2")]

    out = _compact_previous_findings(findings)

    assert len(out) == 2
    assert {entry["category"] for entry in out} == {"scope", "x2"}

"""Tests for planning_findings.demote_findings_already_repaired (Fix A).

The deterministic_repair pass runs before the reviewer every round and fixes
several structural fields (layer_owner, change_log, invariants, verify_recipes,
files_to_change scope conflicts, parallel write conflicts). The reviewer can
still flag the same fields because it never sees the repair log.

Without this layer the planning loop spent rounds 4-7 of a 7-round run
re-litigating already-fixed structure (the actual real-world bug). This test
suite locks in the fix.
"""

from __future__ import annotations

from kodawari.autopilot.planning.planning_findings import (
    demote_findings_already_repaired,
    demote_meta_blocker_findings_to_info,
    is_meta_blocker_finding,
)


def test_no_repairs_returns_payload_unchanged() -> None:
    review = {
        "approved": False,
        "findings": [
            {"severity": "blocking", "category": "scope", "description": "missing route"},
        ],
    }
    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=[], plan_payload={"tasks": []}
    )
    assert demoted == []
    assert out is review


def test_change_log_repair_demotes_change_log_finding_for_same_task() -> None:
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "completeness",
                "description": "change_log missing modified task fields for T1",
                "recommendation": "add change_log entry",
            }
        ],
    }
    plan = {
        "tasks": [
            {"task_id": "T1", "task_name": "Service"},
            {"task_id": "T2", "task_name": "Route"},
        ]
    }
    repairs = [
        {"rule": "add_missing_task_change_log_entry", "task_id": "T1", "location": "change_log"}
    ]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert len(demoted) == 1
    finding = out["findings"][0]
    assert finding["severity"] == "info"
    assert finding["severity_demoted"] is True
    assert finding["demoted_reason"] == (
        "deterministic_repair_already_applied:add_missing_task_change_log_entry"
    )
    assert finding["demoted_task_ids"] == ["T1"]


def test_change_log_repair_does_not_demote_finding_about_different_task() -> None:
    """T1's repair must not silence a finding about T2."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "completeness",
                "description": "change_log missing entry for T2 — planner removed task",
                "recommendation": "add change_log entry for T2",
            }
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}, {"task_id": "T2"}]}
    repairs = [{"rule": "add_missing_task_change_log_entry", "task_id": "T1"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert demoted == []
    assert out["findings"][0]["severity"] == "blocking"


def test_layer_owner_repair_uses_location_to_resolve_task_id() -> None:
    """When task_id is absent, _repair_log_task_id parses ``tasks[N].layer_owner``."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "high",
                "category": "structure",
                "description": "layer_owner missing for T1",
                "recommendation": "set layer_owner",
            }
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}, {"task_id": "T2"}]}
    repairs = [{"rule": "infer_missing_layer_owner", "location": "tasks[1].layer_owner"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert len(demoted) == 1
    assert out["findings"][0]["severity"] == "info"
    assert out["findings"][0]["demoted_task_ids"] == ["T1"]


def test_verify_recipes_repair_demotes_plan_wide_finding_without_task_id() -> None:
    """Plan-wide repairs (verify_recipes, serialize_parallel) need no task_id mention."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "coverage",
                "description": "verify_recipes contains duplicates",
                "recommendation": "deduplicate",
            }
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}]}
    repairs = [{"rule": "dedupe_verify_recipes", "location": "verify_recipes[2]"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert len(demoted) == 1
    assert out["findings"][0]["severity"] == "info"


def test_invariants_truncation_demotes_invariants_count_complaint() -> None:
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "shape",
                "description": "tasks[1].invariants exceeds 5 entries — keep top 5 only (T1)",
                "recommendation": "truncate invariants",
            }
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}]}
    repairs = [{"rule": "truncate_invariants", "location": "tasks[1].invariants"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert len(demoted) == 1
    assert out["findings"][0]["severity"] == "info"


def test_unrelated_finding_stays_blocking() -> None:
    """A finding that does not match any repair bucket must keep its severity."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "security",
                "description": "Plan exposes service account credentials in logs",
                "recommendation": "redact",
            }
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}]}
    repairs = [{"rule": "infer_missing_layer_owner", "task_id": "T1"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert demoted == []
    assert out["findings"][0]["severity"] == "blocking"


def test_unknown_repair_rule_is_ignored() -> None:
    """A rule not in _REPAIR_RULE_BUCKETS is treated as a no-op signature."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "completeness",
                "description": "change_log missing entries for T1",
                "recommendation": "fix",
            }
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}]}
    repairs = [{"rule": "some_future_rule_we_havent_added_yet", "task_id": "T1"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert demoted == []
    assert out["findings"][0]["severity"] == "blocking"


def test_already_info_finding_is_passed_through_unchanged() -> None:
    review = {
        "approved": False,
        "findings": [
            {"severity": "info", "category": "completeness", "description": "change_log noted"}
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}]}
    repairs = [{"rule": "add_missing_task_change_log_entry", "task_id": "T1"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert demoted == []
    assert out["findings"][0]["severity"] == "info"
    assert out["findings"][0].get("severity_demoted") is None


def test_review_payload_other_fields_preserved() -> None:
    """severity demotion must not rewrite ``approved``, ``score``, ``assessment``."""
    review = {
        "approved": False,
        "score": 4.1,
        "assessment": "structural_checks_failed",
        "contradictions": ["x"],
        "findings": [
            {
                "severity": "blocking",
                "category": "completeness",
                "description": "change_log missing for T1",
                "recommendation": "fix",
            }
        ],
    }
    plan = {"tasks": [{"task_id": "T1"}]}
    repairs = [{"rule": "add_missing_task_change_log_entry", "task_id": "T1"}]

    out, demoted = demote_findings_already_repaired(
        review, deterministic_repairs=repairs, plan_payload=plan
    )

    assert len(demoted) == 1
    assert out["approved"] is False
    assert out["score"] == 4.1
    assert out["assessment"] == "structural_checks_failed"
    assert out["contradictions"] == ["x"]


# ---------------------------------------------------------------------------
# Phase B: demote_meta_blocker_findings_to_info
# ---------------------------------------------------------------------------


def test_meta_blocker_classifier_requires_meta_field_plus_recursive_marker() -> None:
    """Tight keyword pairing — bare evidence_resolutions mention is NOT meta."""
    assert is_meta_blocker_finding(
        {
            "category": "structure",
            "description": "evidence_resolutions[R5F1] must cite a ref about itself",
        }
    ) is True
    assert is_meta_blocker_finding(
        {
            "category": "plan_consistency",
            "description": "structural compliance requires meta-structural claim about evidence_resolutions",
        }
    ) is True
    assert is_meta_blocker_finding(
        {
            "category": "structure",
            "description": "evidence_resolutions[R1F1] is missing required field evidence_refs",
        }
    ) is False
    assert is_meta_blocker_finding(
        {
            "category": "scope",
            "description": "files_to_change does not match the canonical handler",
        }
    ) is False


def test_meta_blocker_demote_rewrites_blocking_to_info() -> None:
    review = {
        "approved": False,
        "score": 8.4,
        "findings": [
            {
                "severity": "blocking",
                "category": "plan_consistency",
                "description": (
                    "evidence_resolutions[R5F1] must include an evidence_ref that "
                    "references the meta-structural claim about itself"
                ),
                "recommendation": "Add a self-referential evidence_ref",
            }
        ],
    }
    out, demoted = demote_meta_blocker_findings_to_info(review)
    assert len(demoted) == 1
    finding = out["findings"][0]
    assert finding["severity"] == "info"
    assert finding["severity_demoted"] is True
    assert finding["demoted_reason"] == "meta_blocker_streak_demotion"
    assert out["approved"] is True
    assert out["approved_by_meta_blocker_demotion"] is True


def test_meta_blocker_demote_skips_non_meta_blockers() -> None:
    """A real-scope blocker must NOT be demoted."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "scope",
                "description": "files_to_change must include the canonical handler module",
                "recommendation": "Add the route handler",
            }
        ],
    }
    out, demoted = demote_meta_blocker_findings_to_info(review)
    assert demoted == []
    assert out is review
    assert out["findings"][0]["severity"] == "blocking"


def test_meta_blocker_demote_only_touches_meta_when_mixed() -> None:
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "plan_consistency",
                "description": "evidence_resolutions[R5F1] must reference itself recursively",
            },
            {
                "severity": "blocking",
                "category": "scope",
                "description": "real owner_surface mismatch on handler call chain",
            },
        ],
    }
    out, demoted = demote_meta_blocker_findings_to_info(review)
    assert len(demoted) == 1
    assert out["findings"][0]["severity"] == "info"
    assert out["findings"][1]["severity"] == "blocking"
    assert out["approved"] is True


def test_meta_blocker_demote_noop_when_findings_already_info() -> None:
    review = {
        "approved": True,
        "findings": [
            {
                "severity": "info",
                "category": "plan_consistency",
                "description": "evidence_resolutions[R5F1] references itself meta-structurally",
            }
        ],
    }
    out, demoted = demote_meta_blocker_findings_to_info(review)
    assert demoted == []
    assert out is review


def test_meta_blocker_demote_refuses_to_silence_security_finding() -> None:
    """Hard-stop category (security/auth/credentials/data_loss/privacy)
    must never be demoted even when wearing recursive meta wording.
    Regression guard against a high-severity safety finding being silently
    dropped because reviewer phrasing happened to match meta_blocker."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "high",
                "category": "security",
                "description": (
                    "evidence_resolutions must reference the credential rotation "
                    "claim about itself — meta-structural compliance"
                ),
                "recommendation": "Audit credential exposure path",
            }
        ],
    }
    out, demoted = demote_meta_blocker_findings_to_info(review)
    assert demoted == []
    assert out is review
    assert out["findings"][0]["severity"] == "high"


def test_meta_blocker_demote_refuses_data_loss_finding_with_meta_wording() -> None:
    """``data_loss`` category match plus high-hard-stop term keeps severity."""
    review = {
        "approved": False,
        "findings": [
            {
                "severity": "high",
                "category": "data_loss",
                "description": (
                    "evidence_resolutions must reference the data_loss claim itself"
                ),
            }
        ],
    }
    out, demoted = demote_meta_blocker_findings_to_info(review)
    assert demoted == []
    assert out["findings"][0]["severity"] == "high"


def test_is_meta_blocker_finding_excludes_hard_stop_categories() -> None:
    """Unit-level guard mirroring the demote helper's safety filter."""
    assert (
        is_meta_blocker_finding(
            {
                "severity": "blocking",
                "category": "auth",
                "description": "evidence_resolutions references itself meta-structurally",
            }
        )
        is False
    )
    assert (
        is_meta_blocker_finding(
            {
                "severity": "blocking",
                "category": "plan_consistency",
                "description": "evidence_resolutions references itself meta-structurally",
            }
        )
        is True
    )


def test_meta_blocker_demote_reason_param_threads_through_audit_marker() -> None:
    """Phase C distinguishes streak demotion from late-round recovery via
    the ``reason`` parameter so the artifact's meta_blocker_demotion_log
    keeps both paths auditable. Default reason preserves Phase B behavior."""
    from kodawari.autopilot.planning.planning_findings import (
        META_BLOCKER_LATE_ROUND_RECOVERY_REASON,
        META_BLOCKER_STREAK_REASON,
    )

    base_review = lambda: {
        "approved": False,
        "findings": [
            {
                "severity": "blocking",
                "category": "plan_consistency",
                "description": (
                    "evidence_resolutions[R5F1] must cite the Round 7 finding "
                    "via meta-structural evidence_ref about itself"
                ),
            }
        ],
    }
    out_default, _ = demote_meta_blocker_findings_to_info(base_review())
    assert out_default["findings"][0]["demoted_reason"] == META_BLOCKER_STREAK_REASON

    out_late, _ = demote_meta_blocker_findings_to_info(
        base_review(),
        reason=META_BLOCKER_LATE_ROUND_RECOVERY_REASON,
    )
    assert (
        out_late["findings"][0]["demoted_reason"]
        == META_BLOCKER_LATE_ROUND_RECOVERY_REASON
    )

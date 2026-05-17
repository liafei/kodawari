from __future__ import annotations

from kodawari.autopilot.planning.review_evidence_scout import (
    PENDING_STATUSES,
    RESOLUTION_STATUSES,
    build_review_evidence_pack,
    classify_review_finding,
    pending_evidence_requests,
)
from kodawari.autopilot.planning.planning_validators import validate_evidence_resolutions


def test_classifies_factual_review_findings() -> None:
    assert classify_review_finding({"description": "Plan is not anchored to the canonical PRD task"}) == "canonical_task_anchor"
    assert classify_review_finding({"description": "Owner surface and files_to_change do not match the handler"}) == "owner_surface"
    assert classify_review_finding({"description": "Event vs post ranking semantics need a product decision"}) == "product_semantics"
    assert classify_review_finding({"description": "Missing route regression test coverage"}) == "test_coverage"
    assert classify_review_finding({"description": "This is a subjective style preference"}) == ""


def test_meta_blocker_bucket_catches_recursive_evidence_demands() -> None:
    """Phase B: reviewer recursing on plan-meta fields (evidence_resolutions
    asked to cite itself) maps to a stable meta_blocker bucket so streak
    detection survives reviewer reword tricks across rounds."""
    assert classify_review_finding(
        {
            "category": "plan_consistency",
            "description": (
                "evidence_resolutions[R5F1] must include an evidence_ref that "
                "references the meta-structural claim about itself"
            ),
        }
    ) == "meta_blocker"
    assert classify_review_finding(
        {
            "category": "structural_validity",
            "description": "recursive evidence requirement on the R6F1 entry",
        }
    ) == "meta_blocker"
    assert classify_review_finding(
        {
            "category": "structure",
            "description": "circular evidence requirement on change_log entries",
        }
    ) == "meta_blocker"


def test_meta_blocker_does_not_swallow_legit_evidence_resolution_findings() -> None:
    """Bare evidence_resolutions mention without a recursion marker is a
    legitimate first-round structural ask and must NOT bucket as meta."""
    assert classify_review_finding(
        {
            "category": "structure",
            "description": "evidence_resolutions[R1F1] is missing required field evidence_refs",
        }
    ) != "meta_blocker"
    assert classify_review_finding(
        {
            "category": "completeness",
            "description": "change_log missing entry for T1 — planner removed a task",
        }
    ) != "meta_blocker"


def test_meta_blocker_catches_real_external_trends_v1_round7_finding() -> None:
    """Regression: real Round 7 reviewer wording from the external_trends_v1
    7-round deadlock that motivated Phase B. Reviewer asks the planner's
    evidence_resolutions entry for R5F1 to cite the *Round 7* finding — i.e.
    the entry would need to predict a future complaint. Recursive in
    structural intent even though the reviewer never wrote 'itself' /
    'meta-structural'."""
    real_finding = {
        "severity": "blocking",
        "category": "structure",
        "description": (
            "The evidence_resolutions entry for R5F1 is present but has status "
            "'finding_supported' with evidence_refs that do not explicitly cite "
            "the Round 7 finding. The structural requirement is that each "
            "evidence resolution must cite at least one evidence ref that "
            "directly addresses the reviewer's claim."
        ),
        "recommendation": "Add evidence_refs that cite the Round 7 finding directly.",
    }
    assert classify_review_finding(real_finding) == "meta_blocker"


def test_pending_status_set_excludes_finding_supported_and_human_decision() -> None:
    """v2: only ``ambiguous`` keeps a request pending. Earlier the set
    contained ``finding_supported`` and ``needs_human_decision`` too, which
    made the request unclosable and drove the planner-reviewer loop into
    ``planning_evidence_blocked`` regardless of plan revisions."""
    assert PENDING_STATUSES == {"ambiguous"}
    assert "needs_human_decision" not in RESOLUTION_STATUSES


def test_evidence_pack_marks_every_factual_finding_as_ambiguous() -> None:
    """Scout no longer pre-judges findings as ``finding_supported`` or
    ``needs_human_decision``. The planner settles the resolution; the
    orchestrator's ambiguous-streak detector handles deadlocks."""
    context = {
        "prd_excerpt": "The PRD defines Event objects as the ranking atom for social replies.",
        "repo_manifest": {"files": ["backend/api/v1/services/social_event_service.py", "tests/test_social_event.py"]},
    }
    pack = build_review_evidence_pack(
        round_number=2,
        plan_payload={"summary": "rank posts", "tasks": [{"task_id": "T1", "files_to_change": ["backend/api/v1/services/social_post_service.py"]}]},
        findings=[
            {
                "severity": "blocking",
                "category": "product",
                "description": "Event vs post ranking semantics are ambiguous",
                "recommendation": "Confirm the product atom before execution",
            }
        ],
        context=context,
    )

    assert pack["schema_version"] == "planning.review_evidence.v1"
    request = pack["requests"][0]
    assert request["status"] == "ambiguous"
    assert request["finding_id"] == "R2F1"
    assert len(request["evidence"]) <= 5
    assert pending_evidence_requests([pack])[0]["finding_id"] == "R2F1"


def test_evidence_pack_owner_surface_no_longer_self_supports() -> None:
    """Regression for the owner_surface finding_supported pre-stamp that
    made any reviewer-flagged surface mismatch unclosable."""
    pack = build_review_evidence_pack(
        round_number=1,
        plan_payload={"summary": "x", "tasks": [{"task_id": "T1", "files_to_change": ["unrelated.py"]}]},
        findings=[
            {
                "severity": "blocking",
                "category": "structure",
                "description": "owner surface and handler chain do not match files_to_change",
                "recommendation": "trace the route to the handler",
            }
        ],
        context={
            "repo_manifest": {"files": ["src/handler/route.py", "src/handler/owner.py"]},
        },
    )

    assert pack["requests"][0]["status"] == "ambiguous"


def test_validate_evidence_resolutions_requires_structured_reply() -> None:
    pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
            }
        ]
    }

    errors = validate_evidence_resolutions({"summary": "x"}, [pack])

    assert any("evidence_resolutions missing" in error for error in errors)


def test_validate_evidence_resolutions_allows_finding_refuted_with_valid_ref() -> None:
    """v2 removes the interlock that blocked refutation when the scout had
    pre-stamped the request as finding_supported. Now the planner can
    legitimately refute any pending request as long as it cites a ref from
    the pack and supplies a rationale."""
    pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
            }
        ]
    }
    plan = {
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "finding_refuted",
                "evidence_refs": ["plan:summary"],
                "rationale": "the plan summary already covers this surface",
            }
        ]
    }

    assert validate_evidence_resolutions(plan, [pack]) == []


def test_validate_evidence_resolutions_rejects_unknown_refs() -> None:
    pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
            }
        ]
    }
    plan = {
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence_refs": ["unknown"],
                "rationale": "maybe",
            }
        ]
    }

    errors = validate_evidence_resolutions(plan, [pack])

    assert any("unknown refs" in error for error in errors)


def test_validate_evidence_resolutions_rejects_legacy_needs_human_decision() -> None:
    """``needs_human_decision`` is no longer a permitted resolution
    status. Any attempt by the planner to use it (e.g. from an old
    prompt) is rejected at the validator boundary."""
    pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
            }
        ]
    }
    plan = {
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "needs_human_decision",
                "evidence_refs": ["plan:summary"],
                "rationale": "punt",
            }
        ]
    }

    errors = validate_evidence_resolutions(plan, [pack])

    assert any("status must be one of" in error for error in errors)


def test_validate_evidence_resolutions_accepts_ambiguous_with_valid_ref() -> None:
    pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
            }
        ]
    }
    plan = {
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence_refs": ["plan:summary"],
                "rationale": "Evidence is inconclusive.",
            }
        ]
    }

    assert validate_evidence_resolutions(plan, [pack]) == []


def test_validate_evidence_resolutions_blocks_finding_supported_without_scope_delta() -> None:
    """Layer C: when planner declares finding_supported on a previously-
    emitted finding but the plan does not change in the scope the finding
    pointed at, the validator must block. Otherwise the planner can verbally
    accept findings while keeping the same erroneous scope (the user's
    T100C-style deadlock)."""
    round1_pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
                "reviewer_claim": (
                    "files_to_change misses the route handler and service "
                    "owner_surface for events social aggregation"
                ),
                "instruction": "Add the route handler to files_to_change.",
            }
        ]
    }
    round2_pack = {
        "requests": [
            {
                "finding_id": "R2F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
                "reviewer_claim": "minor wording nit",
                "instruction": "Restate the summary.",
            }
        ]
    }
    previous_plan = {
        "tasks": [
            {
                "task_id": "T1",
                "files_to_change": ["tests/test_existing.py"],
                "invariants": [],
                "test_plan": "covered",
            }
        ]
    }
    current_plan_no_scope_delta = {
        "tasks": [
            {
                "task_id": "T1",
                "files_to_change": ["tests/test_existing.py"],
                # Only narrative summary changed, no scope-bearing field touches
                "invariants": ["unchanged"],
                "test_plan": "covered",
            }
        ],
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "finding_supported",
                "evidence_refs": ["plan:summary"],
                "rationale": "we accept the finding",
            },
            {
                "finding_id": "R2F1",
                "status": "ambiguous",
                "evidence_refs": ["plan:summary"],
                "rationale": "still pending",
            },
        ],
    }

    errors = validate_evidence_resolutions(
        current_plan_no_scope_delta,
        [round1_pack, round2_pack],
        previous_plan=previous_plan,
    )

    assert any(
        "did not change in the scope this finding pointed at" in err for err in errors
    ), errors


def test_validate_evidence_resolutions_accepts_finding_supported_with_token_match() -> None:
    """Layer C positive path: planner adds the scope the finding pointed at
    (matching subject tokens in invariants/test_plan) → validator passes."""
    round1_pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
                "reviewer_claim": (
                    "files_to_change misses the route handler and service "
                    "owner_surface for events social aggregation"
                ),
                "instruction": "Add the route handler to files_to_change.",
            }
        ]
    }
    round2_pack = {"requests": []}
    previous_plan = {
        "tasks": [
            {"task_id": "T1", "files_to_change": ["tests/test_existing.py"]}
        ]
    }
    current_plan = {
        "tasks": [
            {
                "task_id": "T1",
                "files_to_change": ["tests/test_existing.py"],
                "invariants": [
                    "Add events social aggregation route handler wiring"
                ],
                "test_plan": "covered",
            }
        ],
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "finding_supported",
                "evidence_refs": ["plan:summary"],
                "rationale": "added the route handler",
            }
        ],
    }

    errors = validate_evidence_resolutions(
        current_plan, [round1_pack, round2_pack], previous_plan=previous_plan
    )

    assert errors == []


def test_validate_evidence_resolutions_accepts_finding_supported_with_basename_match() -> None:
    """Layer C basename fallback (Q1 c): when the planner's recommendation uses
    domain wording that doesn't appear verbatim in the planner's plan, but the
    planner adds a file whose basename matches the finding subject — accept it.
    """
    round1_pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
                "reviewer_claim": "events social aggregation handler missing",
                "instruction": "Add the events social aggregation handler.",
            }
        ]
    }
    previous_plan = {"tasks": [{"task_id": "T1", "files_to_change": []}]}
    current_plan = {
        "tasks": [
            {
                "task_id": "T1",
                "files_to_change": [
                    "backend/api/v1/services/social_event_aggregation_service.py"
                ],
                "invariants": ["wire it up"],
            }
        ],
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "finding_supported",
                "evidence_refs": ["plan:summary"],
                "rationale": "added the handler module",
            }
        ],
    }

    errors = validate_evidence_resolutions(
        current_plan, [round1_pack], previous_plan=previous_plan
    )

    assert errors == []


def test_validate_evidence_resolutions_first_round_acceptance_not_blocked() -> None:
    """Layer C footgun avoidance: a finding emitted for the first time in
    the current round can be marked finding_supported without enforcement.
    The planner has not had a chance to ignore it yet."""
    round1_pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
                "reviewer_claim": "events social aggregation handler missing",
                "instruction": "Add the handler.",
            }
        ]
    }
    plan = {
        "tasks": [{"task_id": "T1", "files_to_change": ["tests/test_x.py"]}],
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "finding_supported",
                "evidence_refs": ["plan:summary"],
                "rationale": "we accept on first hearing",
            }
        ],
    }

    # No previous_plan → first-round case, must not enforce delta check
    errors = validate_evidence_resolutions(plan, [round1_pack], previous_plan=None)
    assert errors == []

    # Even with a previous_plan, when the finding_id is fresh in the current
    # pack (i.e. not seen in any earlier pack), the closure check is skipped.
    errors = validate_evidence_resolutions(
        plan, [round1_pack], previous_plan={"tasks": []}
    )
    assert errors == []


def test_validate_evidence_resolutions_finding_refuted_also_requires_scope_delta() -> None:
    """Layer C symmetry (Scenario B fix): finding_refuted is the easier
    escape hatch if it doesn't get the same delta requirement. Apply
    symmetric enforcement so the planner can't dodge by switching to
    finding_refuted instead of finding_supported."""
    round1_pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
                "reviewer_claim": "events social aggregation handler missing",
                "instruction": "Add the handler.",
            }
        ]
    }
    round2_pack = {"requests": []}
    previous_plan = {
        "tasks": [{"task_id": "T1", "files_to_change": ["tests/test_a.py"]}]
    }
    current_plan = {
        "tasks": [{"task_id": "T1", "files_to_change": ["tests/test_a.py"]}],
        "evidence_resolutions": [
            {
                "finding_id": "R1F1",
                "status": "finding_refuted",
                "evidence_refs": ["plan:summary"],
                "rationale": "we deny the claim",
            }
        ],
    }

    errors = validate_evidence_resolutions(
        current_plan, [round1_pack, round2_pack], previous_plan=previous_plan
    )

    assert any(
        "did not change in the scope this finding pointed at" in err for err in errors
    ), errors


def test_pending_evidence_requests_filters_already_resolved() -> None:
    """A finding closed by an earlier round must not re-surface in later
    rounds. This is the ``prior_resolutions`` filter introduced in v2."""
    pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
            }
        ]
    }
    prior_resolutions = [
        {
            "finding_id": "R1F1",
            "status": "finding_refuted",
            "evidence_refs": ["plan:summary"],
            "rationale": "cleared in round 1",
        }
    ]

    assert pending_evidence_requests([pack], prior_resolutions=prior_resolutions) == []


def test_pending_evidence_requests_keeps_ambiguous_resolution_pending() -> None:
    """An ``ambiguous`` resolution does not close the request — the
    planner is still expected to make progress in subsequent rounds, and
    a persistent ambiguous run is what the orchestrator escalates on."""
    pack = {
        "requests": [
            {
                "finding_id": "R1F1",
                "status": "ambiguous",
                "evidence": [{"ref_id": "plan:summary"}],
            }
        ]
    }
    prior_resolutions = [
        {
            "finding_id": "R1F1",
            "status": "ambiguous",
            "evidence_refs": ["plan:summary"],
            "rationale": "still inconclusive",
        }
    ]

    pending = pending_evidence_requests([pack], prior_resolutions=prior_resolutions)
    assert [item["finding_id"] for item in pending] == ["R1F1"]

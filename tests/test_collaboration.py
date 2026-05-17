from kodawari.autopilot.collaboration import (
    ArchitectureDecision,
    build_round_record,
    CollaborationAction,
    CollaborationRole,
    TaskRouter,
    build_collaboration_context,
    mark_codex_fix_applied,
    normalize_reviewer_feedback,
    record_opus_review,
    request_executor_recovery,
    sync_collaboration_to_state,
    update_round_record_outcome,
)


def test_architecture_decision_round_trip() -> None:
    decision = ArchitectureDecision(
        decision_id="ADR-001",
        decision="Freeze ranking snapshots per user/day",
        rationale="Keep personalized results deterministic",
        constraints=["No cross-day mutation", "Timezone aware"],
        api_contracts=["GET /api/v1/ranking returns snapshot_id"],
        test_strategy=["Add snapshot stability tests"],
    )
    restored = ArchitectureDecision.from_dict(decision.to_dict())
    assert restored.decision_id == "ADR-001"
    assert restored.constraints == ["No cross-day mutation", "Timezone aware"]


def test_task_router_and_review_fix_loop() -> None:
    assert TaskRouter.route("T001: Architecture review for ranking algorithm") == CollaborationRole.OPUS
    context = build_collaboration_context(
        "T020",
        "T020: Implement ranking API endpoint",
        architecture_decisions=[ArchitectureDecision(decision_id="ADR-020", decision="Keep schema", rationale="compatibility")],
    )
    assert context.assigned_role == CollaborationRole.CODEX
    assert context.next_action() == CollaborationAction.IMPLEMENT

    state_payload: dict[str, object] = {}
    record_opus_review(
        context,
        approved=False,
        must_fix=["Add boundary tests"],
        summary="need tests",
        state_payload=state_payload,
    )
    assert context.next_action() == CollaborationAction.FIX_ROUND
    assert len(context.review_history) == 1
    assert context.review_history[0].review_iteration == 1
    assert "collaboration_context" in state_payload

    context.implementation_started = True
    mark_codex_fix_applied(context, resolution_summary="tests added", state_payload=state_payload)
    assert context.next_action() == CollaborationAction.VERIFY
    assert context.fix_history
    assert context.fix_history[0]["actor"] == "codex"
    assert context.fix_history[0]["resolved_must_fix"] == ["Add boundary tests"]


def test_normalize_reviewer_feedback_applies_quality_thresholds() -> None:
    feedback = normalize_reviewer_feedback(
        {
            "approved": True,
            "reviewer": "opus",
            "summary": "Looks good at first glance",
            "score": 82,
            "target_score": 95,
            "min_dimension_score": 80,
            "dimension_scores": {
                "architecture": 79,
                "tests": 88,
            },
        },
        review_iteration=3,
    )

    assert feedback.approved is False
    assert feedback.review_iteration == 3
    assert feedback.gate_recommendation == "REVIEW_FIX_REQUIRED"
    assert any("score 82 below target 95" in item.lower() for item in feedback.must_fix)
    assert any("architecture" in item.lower() for item in feedback.blocking_items)


def test_sync_collaboration_to_state_keeps_decisions() -> None:
    context = build_collaboration_context(
        "T021",
        "T021: Refactor ranking scorer",
        architecture_decisions=[
            ArchitectureDecision(
                decision_id="ADR-021",
                decision="Use weighted score normalization",
                rationale="keep deterministic ordering",
            )
        ],
    )
    state_payload: dict[str, object] = {}
    synced = sync_collaboration_to_state(state_payload, context)
    assert synced["collaboration_context"]["task_id"] == "T021"
    assert synced["architecture_decisions"][0]["id"] == "ADR-021"


def test_round_record_helpers_capture_boundary_and_outcome() -> None:
    context = build_collaboration_context(
        "T030",
        "T030: absorb orchestration round metadata",
        architecture_decisions=[
            ArchitectureDecision(
                decision_id="ADR-030",
                decision="Persist reviewer metadata",
                rationale="stable round semantics",
            )
        ],
    )
    record_opus_review(
        context,
        approved=False,
        summary="Need test updates",
        must_fix=["Add reviewer regression tests"],
    )
    round_record = build_round_record(
        round_index=3,
        cycle=7,
        task_id=context.task_id,
        task_label=context.task_label,
        action=CollaborationAction.FIX_ROUND,
        actor=CollaborationRole.CODEX,
        context=context,
    )
    assert round_record["round_id"] == "R003"
    assert round_record["assigned_role_before"] == "codex"
    assert round_record["must_fix_open"] == 1

    mark_codex_fix_applied(context, resolution_summary="tests updated")
    updated = update_round_record_outcome(round_record, context)
    assert updated["assigned_role_after"] == "opus"
    assert updated["must_fix_remaining"] == 0
    assert updated["gate_recommendation"] == "REVIEW_PENDING"


def test_record_opus_review_global_fail_downgrades_and_persists_named_fields() -> None:
    context = build_collaboration_context(
        "T040",
        "T040: Review global consistency handling",
    )
    context.review_scope = "full_feature"

    record_opus_review(
        context,
        approved=True,
        summary="Local implementation looks correct.",
        must_fix=[],
        should_fix=[],
        blocking_items=[],
        severity="low",
        gate_recommendation="PROCEED_TO_GATE",
        global_consistency_verdict="fail",
        local_implementation_verdict="pass",
        deterministic_finding_responses=[
            {
                "finding_type": "out_of_scope_files",
                "acknowledged": True,
                "assessment": "Changed file exceeded task card boundary.",
            }
        ],
        evidence_refs=[
            {
                "artifact": ".review_bundle.json",
                "field_path": "deterministic_findings.out_of_scope_files",
                "reason": "Boundary violation evidence",
            }
        ],
    )

    assert context.review_feedback.approved is False
    assert context.review_feedback.global_consistency_verdict == "FAIL"
    assert context.review_feedback.local_implementation_verdict == "PASS"
    assert context.review_feedback.gate_recommendation == "REVIEW_FIX_REQUIRED"
    assert "global consistency check failed" in context.review_feedback.blocking_items
    assert "Resolve global consistency conflicts before proceeding" in context.review_feedback.must_fix
    assert context.review_feedback.deterministic_finding_responses == [
        {
            "finding_type": "out_of_scope_files",
            "acknowledged": True,
            "assessment": "Changed file exceeded task card boundary.",
        }
    ]
    assert context.review_feedback.evidence_refs == [
        {
            "artifact": ".review_bundle.json",
            "field_path": "deterministic_findings.out_of_scope_files",
            "reason": "Boundary violation evidence",
        }
    ]
    assert len(context.review_history) == 1
    assert context.review_history[0].global_consistency_verdict == "FAIL"


def test_single_task_attribution_this_task_overrides_approved() -> None:
    """single_task scope + global FAIL + attribution=this_task -> override."""
    context = build_collaboration_context("T050", "T050: structural attribution test")
    # default scope is single_task

    record_opus_review(
        context,
        approved=True,
        summary="local diff looks fine",
        must_fix=[],
        should_fix=[],
        blocking_items=[],  # NOTE: empty — old keyword scan would NOT trigger override
        severity="low",
        gate_recommendation="PROCEED_TO_GATE",
        global_consistency_verdict="fail",
        global_failure_attribution="this_task",
    )

    # Structured attribution must override even when blocking_items is empty —
    # this is the whole point of replacing the substring scan.
    assert context.review_feedback.approved is False
    assert context.review_feedback.global_failure_attribution == "this_task"


def test_single_task_attribution_sibling_tasks_does_not_override() -> None:
    """single_task scope + global FAIL + attribution=sibling_tasks -> do NOT override.

    The reviewer's natural-language `blocking_items` may contain the word
    'invariant' (or any of the legacy keywords) by coincidence; with the
    structured attribution field, the keyword presence MUST NOT trigger
    override when the attribution explicitly says the failure is upstream.
    """
    context = build_collaboration_context("T051", "T051: sibling-gap attribution")

    record_opus_review(
        context,
        approved=True,
        summary="local diff looks fine; missing sibling implementation",
        must_fix=[],
        should_fix=[],
        # Adversarial: contains 'invariant' word that the legacy substring scan
        # would have picked up as a local violation.
        blocking_items=["The system invariant cannot be checked until T060 lands."],
        severity="medium",
        gate_recommendation="REVIEW_PENDING",
        global_consistency_verdict="fail",
        global_failure_attribution="sibling_tasks",
    )

    # Structured field wins over substring scan: do NOT downgrade approved.
    # NOTE: approved may still be False because blocking_items is non-empty
    # (collaboration_core._resolve_review_approved). What we're testing is
    # that the FAIL verdict did NOT itself force the override path.
    assert context.review_feedback.global_failure_attribution == "sibling_tasks"
    assert "global consistency check failed" not in (context.review_feedback.blocking_items or [])


def test_single_task_attribution_unknown_does_not_override() -> None:
    """single_task scope + global FAIL + attribution=unknown -> do NOT override.

    Conservative default: if the reviewer can't attribute, don't deadlock.
    """
    context = build_collaboration_context("T052", "T052: unattributable failure")

    record_opus_review(
        context,
        approved=True,
        summary="cannot determine source of inconsistency",
        must_fix=[],
        should_fix=[],
        blocking_items=[],
        severity="low",
        gate_recommendation="PROCEED_TO_GATE",
        global_consistency_verdict="fail",
        global_failure_attribution="unknown",
    )

    assert context.review_feedback.global_failure_attribution == "unknown"
    assert "global consistency check failed" not in (context.review_feedback.blocking_items or [])


def test_single_task_missing_attribution_falls_back_to_legacy_scan() -> None:
    """When reviewer omits attribution entirely, the legacy substring scan still
    works as a transitional safety net (with deprecation warning logged).
    """
    context = build_collaboration_context("T053", "T053: legacy reviewer compat")

    record_opus_review(
        context,
        approved=True,
        summary="local diff seems ok",
        must_fix=[],
        should_fix=[],
        # Contains a legacy keyword "layer boundary" — under fallback this
        # still triggers override (so we don't regress strict reviewers).
        blocking_items=["The diff crosses a layer boundary into the wrong module."],
        severity="high",
        gate_recommendation="REVIEW_FIX_REQUIRED",
        global_consistency_verdict="fail",
        # Intentionally NOT setting global_failure_attribution.
    )

    assert context.review_feedback.global_failure_attribution is None
    assert context.review_feedback.approved is False
    assert "global consistency check failed" in (context.review_feedback.blocking_items or [])


def test_request_executor_recovery_does_not_bump_review_iteration() -> None:
    """Stall recovery routes to FIX_ROUND but must not consume reviewer round budget."""
    context = build_collaboration_context("T030", "T030: stall recovery")

    # First a real external reviewer turn to seed review_iteration = 1.
    record_opus_review(context, approved=False, must_fix=["fix tests"], summary="initial")
    assert context.review_feedback.review_iteration == 1
    assert len(context.review_history) == 1

    # Two stall recoveries — neither should bump iteration nor append to history.
    request_executor_recovery(
        context,
        blocking_reason="EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        summary="stall #1",
    )
    request_executor_recovery(
        context,
        blocking_reason="VERIFY_FAILED_RETRYABLE",
        summary="stall #2",
    )

    assert context.review_feedback.review_iteration == 1  # unchanged by stall fakes
    assert len(context.review_history) == 1               # still one real review
    # Still routed to FIX_ROUND because must_fix is non-empty.
    assert context.next_action() == CollaborationAction.FIX_ROUND
    assert context.review_feedback.must_fix == ["VERIFY_FAILED_RETRYABLE"]
    assert context.assigned_role == CollaborationRole.CODEX


def test_request_executor_recovery_sets_source_marker_and_state_sync() -> None:
    context = build_collaboration_context("T031", "T031: stall marker")
    state_payload: dict[str, object] = {}

    request_executor_recovery(
        context,
        blocking_reason="EXECUTOR_STALLED_NO_WRITE_PROGRESS",
        summary="recovery requested",
        source="executor_stall_recovery",
        state_payload=state_payload,
    )

    assert context.review_feedback.source == "executor_stall_recovery"
    assert context.review_feedback.approved is False
    assert context.review_feedback.gate_recommendation == "REVIEW_FIX_REQUIRED"
    assert "collaboration_context" in state_payload


def test_request_executor_recovery_then_real_review_keeps_counter_consistent() -> None:
    """After stall recovery, a real reviewer turn still bumps iteration cleanly to 2."""
    context = build_collaboration_context("T032", "T032: mixed sequence")

    record_opus_review(context, approved=False, must_fix=["fix one"], summary="r1")
    assert context.review_feedback.review_iteration == 1

    request_executor_recovery(
        context,
        blocking_reason="EXECUTOR_STALLED_FRAGMENTED_READS",
        summary="stall",
    )
    assert context.review_feedback.review_iteration == 1

    record_opus_review(context, approved=False, must_fix=["fix two"], summary="r2")
    assert context.review_feedback.review_iteration == 2
    assert len(context.review_history) == 2

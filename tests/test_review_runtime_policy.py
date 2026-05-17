from kodawari.autopilot.review_runtime_policy import (
    classify_review_runtime,
    review_quality_grading_enabled,
)


def test_classify_review_runtime_marks_real_mode_as_real() -> None:
    result = classify_review_runtime(
        {
            "mode": "real_codex_reviewer",
            "real_requested": True,
            "real_required": False,
            "fallback_used": False,
        },
        require_real_peer_review=False,
    )

    assert result.review_quality == "real"
    assert result.is_real_review is True
    assert result.semantic_review_performed is True


def test_classify_review_runtime_marks_requested_fallback_as_degraded() -> None:
    result = classify_review_runtime(
        {
            "mode": "simulate_local",
            "real_requested": True,
            "real_required": False,
            "fallback_used": True,
        },
        require_real_peer_review=False,
    )

    assert result.review_quality == "degraded"
    assert result.is_real_review is False
    assert result.semantic_review_performed is False


def test_classify_review_runtime_marks_plain_simulation_as_simulated() -> None:
    result = classify_review_runtime(
        {
            "mode": "simulate_local",
            "real_requested": False,
            "real_required": False,
            "fallback_used": False,
        },
        require_real_peer_review=False,
    )

    assert result.review_quality == "simulated"
    assert result.is_real_review is False
    assert result.semantic_review_performed is False


def test_classify_review_runtime_marks_required_failure_as_unavailable() -> None:
    result = classify_review_runtime(
        {
            "mode": "real_required_failed",
            "real_requested": True,
            "real_required": True,
            "fallback_used": False,
        },
        require_real_peer_review=True,
    )

    assert result.review_quality == "unavailable"
    assert result.real_required is True
    assert result.is_real_review is False
    assert result.semantic_review_performed is False


def test_review_quality_grading_enabled_defaults_on(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("WORKFLOW_REVIEW_QUALITY_GRADING", raising=False)
    assert review_quality_grading_enabled() is True


def test_review_quality_grading_enabled_can_be_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WORKFLOW_REVIEW_QUALITY_GRADING", "off")
    assert review_quality_grading_enabled() is False

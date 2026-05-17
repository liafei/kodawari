from kodawari.autopilot.review_bridge import validate_dual_review_evidence


def test_dual_review_blocks_when_self_or_peer_evidence_missing() -> None:
    result = validate_dual_review_evidence(
        codex_self_reviews=[],
        peer_reviews=[],
        must_fix_items=[],
    )

    assert result["status"] == "FAIL"
    assert "Missing Codex self-review evidence." in result["issues"]
    assert "Missing Opus peer-review evidence." in result["issues"]


def test_dual_review_blocks_when_real_opus_requested_but_gateway_mode_mismatch() -> None:
    result = validate_dual_review_evidence(
        codex_self_reviews=[{"reviewer": "codex", "approved": True, "summary": "ok"}],
        peer_reviews=[
            {
                "reviewer": "opus",
                "approved": True,
                "review_runtime": {"real_requested": True, "mode": "mock_peer_review"},
            }
        ],
        must_fix_items=[],
    )

    assert result["status"] == "FAIL"
    assert any("real review mode" in item for item in result["issues"])


def test_dual_review_blocks_when_must_fix_items_remain_open() -> None:
    result = validate_dual_review_evidence(
        codex_self_reviews=[{"reviewer": "codex", "approved": True, "summary": "ok"}],
        peer_reviews=[{"reviewer": "opus", "approved": True, "review_runtime": {"mode": "real_opus_gateway"}}],
        must_fix_items=["Add scoped test"],
    )

    assert result["status"] == "FAIL"
    assert "Must-fix items are still open." in result["issues"]


def test_dual_review_passes_when_backend_contract_requires_neither_review() -> None:
    result = validate_dual_review_evidence(
        codex_self_reviews=[],
        peer_reviews=[],
        must_fix_items=[],
        require_self_review=False,
        require_peer_review=False,
    )

    assert result["status"] == "PASS"

from kodawari.autopilot.review_bridge import validate_dual_review_evidence
from kodawari.autopilot.review_contract import derive_runtime_review_evidence, resolve_review_evidence_requirements
from kodawari.cli.delivery_evidence import _review_evidence_check


def test_derive_runtime_review_evidence_allows_claude_code_without_self_or_peer_reviews() -> None:
    payload = derive_runtime_review_evidence(
        run_result={
            "codex_self_reviews": [],
            "peer_review_summary": {"review_count": 0, "enabled": False, "skipped": True},
            "must_fix_open_items": [],
        },
        execution_backend="claude_code",
    )

    assert payload is not None
    assert payload["status"] == "PASS"
    assert payload["checks"]["required_self_review"] is False
    assert payload["checks"]["required_peer_review"] is False
    assert _review_evidence_check(payload)["status"] == "PASS"


def test_review_requirements_do_not_require_self_review_when_review_loop_disabled() -> None:
    requirements = resolve_review_evidence_requirements(
        execution_backend="external_cli",
        self_review_count=0,
        peer_review_summary={"review_count": 0, "enabled": False, "skipped": True},
        peer_review_enabled=False,
        default_require_self_review=True,
    )

    assert requirements["require_self_review"] is False
    assert requirements["require_peer_review"] is False


def test_derive_runtime_review_evidence_allows_external_cli_when_review_loop_disabled() -> None:
    payload = derive_runtime_review_evidence(
        run_result={
            "codex_self_reviews": [],
            "peer_review_summary": {"review_count": 0, "enabled": False, "skipped": True},
            "must_fix_open_items": [],
        },
        execution_backend="external_cli",
    )

    assert payload is not None
    assert payload["status"] == "PASS"
    assert payload["checks"]["required_self_review"] is False
    assert payload["checks"]["required_peer_review"] is False


def test_validate_dual_review_evidence_can_skip_unrequired_self_and_peer_reviews() -> None:
    result = validate_dual_review_evidence(
        codex_self_reviews=[],
        peer_reviews=[],
        must_fix_items=[],
        require_self_review=False,
        require_peer_review=False,
    )

    assert result["status"] == "PASS"
    assert result["checks"]["required_self_review"] is False
    assert result["checks"]["required_peer_review"] is False


def test_review_evidence_check_respects_backend_aligned_requirement_flags() -> None:
    result = _review_evidence_check(
        {
            "status": "PASS",
            "checks": {
                "self_review_count": 0,
                "peer_review_count": 0,
                "must_fix_remaining": 0,
                "required_self_review": False,
                "required_peer_review": False,
            },
            "issues": [],
        }
    )

    assert result["status"] == "PASS"

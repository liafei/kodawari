"""No-fake-run policy regression tests (smallest-viable scope).

Locks in 4 changes that close structural silent-pass slips:

- Fix 1 (narrowed): normalize_review_payload no longer flips approved=false→true
  without a reviewer verdict anchor; P1.6 score-gap flip now requires a real
  reviewer mode. Validated by test_autopilot_opus_gateway.py.
- Fix 3 (gated): _default_review_feedback raises ReviewerUnavailableError when
  WORKFLOW_REVIEW_ENABLED=1 and we're not in a pytest environment.
- Fix 4: summarize_peer_review returns approved=False (not True) when no
  peer review entries were submitted.
- Fix 10: _build_compat_verify_payload reports passed=False when no verify
  command actually ran (command_executed=False).
"""

from __future__ import annotations

import os
import pytest

from kodawari.autopilot.review.review_bridge import summarize_peer_review


def test_summarize_peer_review_empty_list_returns_approved_false() -> None:
    """No-fake-run Fix 4: empty peer_reviews means zero reviewer
    invocations. The previous default approved=True silently claimed
    approval without any signal; downstream gates accepted runs where
    the review step was bypassed entirely. Now approved=False with an
    explicit reason so audit trails record the gap."""
    summary = summarize_peer_review(
        feature="demo",
        reviews=[],
    )
    assert summary["approved"] is False
    assert summary["approved_reason"] == "no_peer_review_ran"
    assert summary["review_count"] == 0


def test_summarize_peer_review_with_entries_keeps_approval_logic() -> None:
    """The empty-list flip must not break the populated-list path."""
    summary = summarize_peer_review(
        feature="demo",
        reviews=[
            {"approved": True, "review_iteration": 0, "review_runtime": {"mode": "real_opus_gateway"}},
        ],
    )
    assert summary["approved"] is True
    assert summary["review_count"] == 1


def test_default_review_feedback_raises_when_review_enabled_and_production() -> None:
    """No-fake-run Fix 3: when an operator explicitly opted in to real
    peer review (WORKFLOW_REVIEW_ENABLED=1) AND we are not running under
    pytest's PYTEST_CURRENT_TEST env var, _default_review_feedback must
    refuse to fabricate a simulated review payload. Without this, an
    adapter that lost its .review() callable silently produces an
    approved=true payload labelled simulated_default."""
    from kodawari.autopilot.engine.engine_review_mixin import (
        EngineReviewMixin,
        ReviewerUnavailableError,
    )

    class _ConfigStub:
        require_real_peer_review = False
        real_peer_review = False

    class _Dummy(EngineReviewMixin):
        def __init__(self) -> None:
            self.config = _ConfigStub()

    dummy = _Dummy()

    # 1) Test environment (PYTEST_CURRENT_TEST is set by pytest itself,
    #    OR WORKFLOW_SDK_TEST_MODE=1) — must NOT raise.
    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    try:
        os.environ["WORKFLOW_REVIEW_ENABLED"] = "1"
        # PYTEST_CURRENT_TEST is set by pytest itself for the duration of
        # this test, so the production-strict raise is suppressed.
        assert os.environ.get("PYTEST_CURRENT_TEST") is not None
        result = dummy._default_review_feedback(
            changed_files=["src/foo.py", "tests/test_foo.py"],
            peer_review_policy={"target_score": 95, "min_dimension_score": 80},
        )
        # Honest label preserved + review_runtime block carries the
        # simulated mode tag so downstream gates can classify it.
        assert result["review_source"] == "simulated_default"
        assert result["review_runtime"]["mode"] == "simulate_local"
        assert result["review_runtime"]["semantic_review_performed"] is False

        # 2) Production-like: clear PYTEST_CURRENT_TEST and WORKFLOW_SDK_TEST_MODE
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        with pytest.raises(ReviewerUnavailableError):
            dummy._default_review_feedback(
                changed_files=["src/foo.py", "tests/test_foo.py"],
                peer_review_policy={"target_score": 95, "min_dimension_score": 80},
            )
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled


def test_default_review_feedback_subscription_mode_still_simulates() -> None:
    """Subscription-mode users (kodawari default per CLAUDE.md:
    'Default is Claude subscription, no API key') run without
    WORKFLOW_REVIEW_ENABLED=1 set. The Fix 3 raise must NOT fire in
    that mode — keeping subscription-mode users on the simulated path
    is the explicit carve-out from sub-agent review."""
    from kodawari.autopilot.engine.engine_review_mixin import EngineReviewMixin

    class _ConfigStub:
        require_real_peer_review = False
        real_peer_review = False

    class _Dummy(EngineReviewMixin):
        def __init__(self) -> None:
            self.config = _ConfigStub()

    dummy = _Dummy()

    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    try:
        # WORKFLOW_REVIEW_ENABLED unset/0 (subscription mode default)
        os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        # Even if we clear pytest env vars to simulate production, the
        # subscription gate (WORKFLOW_REVIEW_ENABLED!=1) keeps us on
        # the simulated path — no raise.
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        result = dummy._default_review_feedback(
            changed_files=["src/foo.py", "tests/test_foo.py"],
            peer_review_policy={"target_score": 95, "min_dimension_score": 80},
        )
        assert result["review_source"] == "simulated_default"
        # Honest review_runtime block still attached.
        assert result["review_runtime"]["mode"] == "simulate_local"
    finally:
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode


def test_p16_flip_runs_via_with_review_runtime_runtime_path() -> None:
    """Critical regression: the P1.6 score-gap flip must fire in the
    REAL runtime path. ``normalize_review_payload`` runs on raw LLM JSON
    BEFORE ``with_review_runtime`` attaches the review_runtime block —
    so any flip guarded on ``payload["review_runtime"]["mode"]`` inside
    ``normalize_review_payload`` would silently never fire. The fix
    relocates the flip to ``apply_score_gap_demote_if_real`` invoked by
    ``with_review_runtime`` after the runtime block is attached.

    This test exercises the full path with a raw payload (no manually
    injected review_runtime), confirming the flip works end-to-end."""
    from kodawari.autopilot.execution.local_adapter_review_runtime import (
        with_review_runtime,
    )
    from kodawari.autopilot.review.opus_gateway import normalize_review_payload

    # Step 1: raw reviewer payload (as parse_review_content would emit
    # after normalize, BEFORE with_review_runtime).
    raw_payload = {
        "approved": False,
        "summary": "Implementation is solid; score 9/10 below target 10.",
        "must_fix": ["Score 9 below target 10"],
        "should_fix": [],
        "blocking_items": [],
        "severity": "info",
        "score": 9,
        "target_score": 10,
        "gate_recommendation": "APPROVED",
    }
    normalized = normalize_review_payload(raw_payload)
    # normalize_review_payload must NOT flip approved (no runtime mode yet)
    assert normalized["approved"] is False

    # Step 2: with_review_runtime attaches mode + applies P1.6 demote
    final_real = with_review_runtime(
        normalized,
        mode="real_opus_gateway",
        real_requested=True,
        real_required=False,
        gateway={"backend": "openai", "model": "gpt-5.5"},
    )
    assert final_real["approved"] is True
    assert final_real.get("score_gap_demoted") is True
    assert final_real["must_fix"] == []

    # Negative: simulated runtime does NOT trigger the flip
    final_sim = with_review_runtime(
        normalize_review_payload(dict(raw_payload)),
        mode="simulate_local",
        real_requested=False,
        real_required=False,
        gateway={"backend": "simulate", "model": ""},
    )
    assert final_sim["approved"] is False


def test_default_review_feedback_raises_when_config_require_real_peer_review() -> None:
    """Fix 3 (d) gap-fill from sub-agent ae38cd56: production code can
    opt in to real peer review via engine config (require_real_peer_review
    or real_peer_review), not only via WORKFLOW_REVIEW_ENABLED env var.
    The raise must fire on either surface."""
    from kodawari.autopilot.engine.engine_review_mixin import (
        EngineReviewMixin,
        ReviewerUnavailableError,
    )

    class _ConfigStub:
        def __init__(self, **kw: Any) -> None:
            self.require_real_peer_review = kw.get("require_real_peer_review", False)
            self.real_peer_review = kw.get("real_peer_review", False)

    class _Dummy(EngineReviewMixin):
        def __init__(self, **kw: Any) -> None:
            self.config = _ConfigStub(**kw)

    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    try:
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)

        # config.require_real_peer_review=True triggers raise even without env
        dummy_a = _Dummy(require_real_peer_review=True)
        with pytest.raises(ReviewerUnavailableError):
            dummy_a._default_review_feedback(
                changed_files=["src/foo.py", "tests/test_foo.py"],
                peer_review_policy={},
            )

        # config.real_peer_review=True also triggers raise
        dummy_b = _Dummy(real_peer_review=True)
        with pytest.raises(ReviewerUnavailableError):
            dummy_b._default_review_feedback(
                changed_files=["src/foo.py", "tests/test_foo.py"],
                peer_review_policy={},
            )
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled


def test_default_review_feedback_test_mode_env_alone_opts_into_simulation() -> None:
    """Fix 3 test-env detection: WORKFLOW_SDK_TEST_MODE=1 alone (without
    PYTEST_CURRENT_TEST) must suppress the production raise so CI scripts
    that run kodawari in test-mode see simulated review payloads."""
    from kodawari.autopilot.engine.engine_review_mixin import EngineReviewMixin

    class _ConfigStub:
        require_real_peer_review = False
        real_peer_review = False

    class _Dummy(EngineReviewMixin):
        def __init__(self) -> None:
            self.config = _ConfigStub()

    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    try:
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ["WORKFLOW_SDK_TEST_MODE"] = "1"
        os.environ["WORKFLOW_REVIEW_ENABLED"] = "1"
        dummy = _Dummy()
        # No raise: test mode opts in to simulation.
        result = dummy._default_review_feedback(
            changed_files=["src/foo.py", "tests/test_foo.py"],
            peer_review_policy={},
        )
        assert result["review_source"] == "simulated_default"
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled


def test_is_test_environment_no_longer_trips_on_sys_modules_pytest() -> None:
    """Fix 0a regression: is_test_environment() in execution_artifacts
    used to also return True when ``"pytest" in sys.modules`` — which
    accidentally treated VS Code's Python test explorer, tox, nox, and
    coverage sessions as test environments because they all import
    pytest in long-lived shells. Production now only counts explicit
    signals: PYTEST_CURRENT_TEST (set by pytest itself for the current
    test) and WORKFLOW_SDK_TEST_MODE=1 (explicit opt-in)."""
    import sys
    from kodawari.autopilot.execution import execution_artifacts

    assert "pytest" in sys.modules, "this test only runs under pytest"

    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    try:
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        # pytest still in sys.modules — the dropped check would have returned True
        assert execution_artifacts.is_test_environment() is False

        os.environ["WORKFLOW_SDK_TEST_MODE"] = "1"
        assert execution_artifacts.is_test_environment() is True

        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        os.environ["PYTEST_CURRENT_TEST"] = "tests/foo.py::test_bar"
        assert execution_artifacts.is_test_environment() is True
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode


def test_validate_proceed_review_evidence_fails_in_strict_mode_when_unenforced() -> None:
    """Fix 6 regression: when review-evidence enforcement is off (no
    config flag set) AND the operator opted into production strict
    (WORKFLOW_REVIEW_ENABLED=1, non-test env), the proceed-gate
    pre-check used to return SKIP which the gate treated as PASS. Now
    returns FAIL so review-evidence cannot be silently bypassed in
    production. Subscription / dev / test runs keep the legacy SKIP."""
    from kodawari.autopilot.engine.engine_review_mixin import EngineReviewMixin

    class _ConfigStub:
        require_real_peer_review = False
        real_peer_review = False

    class _RuntimeStub:
        def __init__(self) -> None:
            self.config_override = {}
            self.execution_result = {}
            self.codex_self_reviews = []
            self.peer_reviews = []
            self.peer_review_summary = {}
            self.peer_review_enabled = False
            class _Ctx:
                class _Feedback:
                    must_fix: list[str] = []
                review_feedback = _Feedback()
            self.context = _Ctx()

    class _Dummy(EngineReviewMixin):
        def __init__(self) -> None:
            self.config = _ConfigStub()
        def _contract_first_mode(self) -> str:
            return ""

    dummy = _Dummy()
    runtime = _RuntimeStub()

    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    try:
        # Subscription / dev mode (WORKFLOW_REVIEW_ENABLED unset) → SKIP preserved
        os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        # Stay under pytest (PYTEST_CURRENT_TEST is set) so strict gate is off
        result_dev = dummy._validate_proceed_review_evidence(runtime)
        assert result_dev["status"] == "SKIP"
        assert result_dev["blocking_reason"] == ""

        # Production strict (env=1, non-test) → FAIL surfaced
        os.environ["WORKFLOW_REVIEW_ENABLED"] = "1"
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        result_strict = dummy._validate_proceed_review_evidence(runtime)
        assert result_strict["status"] == "FAIL"
        assert "review_evidence_skipped_in_strict_mode" in result_strict["issues"]
        assert result_strict["blocking_reason"]
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled


def test_fake_codex_adapter_raises_outside_test_environment() -> None:
    """Fix 2 regression: FakeCodexAdapter.review() must refuse to run
    in production. It returns canned data without any real LLM call —
    silently allowing it in production was a structural fake-pass slip."""
    from kodawari.autopilot.execution.fake_adapter import (
        FakeAdapterConfig,
        FakeAdapterProductionUseError,
        FakeCodexAdapter,
    )

    adapter = FakeCodexAdapter(FakeAdapterConfig())

    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    try:
        # 1) Inside pytest → no raise, returns honest review_runtime block
        payload = adapter.review(task="t", context={}, changed_files=["src/foo.py"])
        assert payload["review_runtime"]["mode"] == "fake_adapter"
        assert payload["review_runtime"]["fake_evidence"] is True
        assert payload["review_runtime"]["review_quality"] == "simulated"
        # reviewer label kept as "opus" for boundary-enforcer compatibility
        assert payload["reviewer"] == "opus"

        # 2) Outside test env → raise
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        with pytest.raises(FakeAdapterProductionUseError):
            adapter.review(task="t", context={}, changed_files=["src/foo.py"])

        # 3) Explicit test-mode opt-in → no raise
        os.environ["WORKFLOW_SDK_TEST_MODE"] = "1"
        payload2 = adapter.review(task="t", context={}, changed_files=["src/foo.py"])
        assert payload2["review_runtime"]["fake_evidence"] is True
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode


def test_self_review_local_default_strict_mode_fails_closed() -> None:
    """Fix 8 regression: LocalCodexAdapter.self_review's local_default
    path runs ``run_codex_self_review`` which returns
    approved=bool(content) without any LLM call. In production strict
    mode, surface this as a blocker; in dev mode, keep the legacy
    behavior so local iteration without a self-review backend works."""
    from kodawari.autopilot.execution.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    # Dummy adapter with empty config — _resolved_self_review_backend() returns "".
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig())

    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    original_self_review_backend = os.environ.get("WORKFLOW_SELF_REVIEW_BACKEND")
    try:
        os.environ.pop("WORKFLOW_SELF_REVIEW_BACKEND", None)

        # 1) Dev mode (no WORKFLOW_REVIEW_ENABLED) → local_default keeps legacy behavior
        os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        payload_dev = adapter.self_review(
            task="t",
            context={},
            changed_files=["src/foo.py"],
        )
        assert payload_dev["source"] == "kodawari.self_review.local_default"
        assert payload_dev.get("review_quality") == "local_default"
        # Note: legacy bool(content) approved value preserved in dev mode

        # 2) Production strict (env=1, non-test) → fails closed
        os.environ["WORKFLOW_REVIEW_ENABLED"] = "1"
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        payload_strict = adapter.self_review(
            task="t",
            context={},
            changed_files=["src/foo.py"],
        )
        assert payload_strict["approved"] is False
        assert payload_strict["blocking_reason"] == "LOCAL_DEFAULT_NOT_A_REVIEW"
        assert payload_strict.get("review_quality") == "local_default"
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled
        if original_self_review_backend is None:
            os.environ.pop("WORKFLOW_SELF_REVIEW_BACKEND", None)
        else:
            os.environ["WORKFLOW_SELF_REVIEW_BACKEND"] = original_self_review_backend


def test_degraded_reviewer_blocks_in_production_strict_mode() -> None:
    """Fix 9 regression: when a real reviewer was attempted but degraded
    to simulated fallback (transient HTTP/timeout), the engine
    previously silently accepted the fallback when require_real=False.
    In production strict mode (_no_fake_run_strict()=True) this
    silent-accept is a fake-pass slip. Now the orchestrator's normal
    round loop provides retry semantics; blocking on degraded surfaces
    the transient failure to the operator."""
    from kodawari.autopilot.engine.engine_review_mixin import EngineReviewMixin

    class _ConfigStub:
        require_real_peer_review = False
        real_peer_review = False

    class _Dummy(EngineReviewMixin):
        def __init__(self) -> None:
            self.config = _ConfigStub()

    dummy = _Dummy()
    degraded_review = {
        "approved": True,  # fallback returned approved=true
        "review_runtime": {
            "mode": "simulate_local",
            "real_requested": True,
            "real_required": False,
            "fallback_used": True,
            "error": {"message": "HTTP 503 from reviewer gateway", "kind": "gateway_request_failed"},
        },
    }

    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    try:
        # 1) Dev mode (no WORKFLOW_REVIEW_ENABLED) → degraded silently accepted
        os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        error_dev = dummy._review_blocking_error(degraded_review)
        assert error_dev == "", f"dev mode should accept degraded fallback, got {error_dev!r}"

        # 2) Production strict (env=1, non-test) → blocks proceed
        os.environ["WORKFLOW_REVIEW_ENABLED"] = "1"
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        error_strict = dummy._review_blocking_error(degraded_review)
        assert error_strict, "production strict should block on degraded fallback"
        # Either the gateway error message OR the strict-mode default message
        assert "HTTP 503" in error_strict or "production strict mode" in error_strict
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled


def test_compat_verify_payload_production_strict_reports_passed_false() -> None:
    """No-fake-run Fix 10 (gated): when verify_cmd could not execute and
    we fall back to the post_execution_qa-derived compat payload,
    production strict mode (WORKFLOW_REVIEW_ENABLED=1 + non-test env)
    reports passed=False with status=NO_VERIFY_COMMAND. Without strict
    mode (subscription-mode dev or pytest), the old passed=True behavior
    is preserved so local iteration without verify_cmd keeps working."""
    from kodawari.autopilot.core.runtime_checks import _build_compat_verify_payload

    qa_payload = {
        "status": "PASS",
        "summary": "post-execution QA evaluated file evidence",
        "artifacts": [],
    }
    verify_targeting = {
        "verify_cmd": "",
        "verify_cmd_resolved": "",
        "verify_target_source": "compat",
        "verify_targets": [],
    }

    # 1) Dev mode (pytest env auto-on) → old behavior preserved
    response = _build_compat_verify_payload(
        feature="demo",
        task_label="T1",
        changed_files=["backend/main.py"],
        qa_payload=qa_payload,
        verify_targeting=verify_targeting,
    )
    assert response["command_executed"] is False
    assert response["passed"] is True  # pytest env keeps old behavior

    # 2) Production strict (WORKFLOW_REVIEW_ENABLED=1, PYTEST_CURRENT_TEST cleared)
    original_pytest_var = os.environ.get("PYTEST_CURRENT_TEST")
    original_test_mode = os.environ.get("WORKFLOW_SDK_TEST_MODE")
    original_review_enabled = os.environ.get("WORKFLOW_REVIEW_ENABLED")
    try:
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        os.environ["WORKFLOW_REVIEW_ENABLED"] = "1"
        response_strict = _build_compat_verify_payload(
            feature="demo",
            task_label="T1",
            changed_files=["backend/main.py"],
            qa_payload=qa_payload,
            verify_targeting=verify_targeting,
        )
        assert response_strict["command_executed"] is False
        assert response_strict["passed"] is False
        assert response_strict["status"] == "NO_VERIFY_COMMAND"
    finally:
        if original_pytest_var is None:
            os.environ.pop("PYTEST_CURRENT_TEST", None)
        else:
            os.environ["PYTEST_CURRENT_TEST"] = original_pytest_var
        if original_test_mode is None:
            os.environ.pop("WORKFLOW_SDK_TEST_MODE", None)
        else:
            os.environ["WORKFLOW_SDK_TEST_MODE"] = original_test_mode
        if original_review_enabled is None:
            os.environ.pop("WORKFLOW_REVIEW_ENABLED", None)
        else:
            os.environ["WORKFLOW_REVIEW_ENABLED"] = original_review_enabled

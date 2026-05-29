"""Tests for the Phase-5 wiring: failure_analyzer integrated into gate_round.

锁定最小接入行为：
- WORKFLOW_VERIFY_ANALYZER 默认开启；0 时 _reopen_for_fix_round 不调用 analyzer
- 默认开启 + Tier B 失败 → must_fix 消息含 "Tier B" 摘要
- 默认开启 + Tier A 已授权 → must_fix 消息含 "Tier A authorized" 摘要
- failure_analysis 写入 round_record details
- _analyze_verify_stdout 空 stdout 返回空列表
"""
from __future__ import annotations

import textwrap
from types import SimpleNamespace
from typing import Any

import pytest

from kodawari.autopilot.engine.gate_round import (
    _analyze_verify_stdout,
    _build_fix_round_msg,
    _reopen_for_fix_round,
    _resolve_verify_check,
    _verify_analyzer_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_PYTEST_ATTR_ERROR = textwrap.dedent("""\
    ================================== FAILURES ===================================
    ____________________________ test_missing_attr ________________________________

    tests/test_things.py:17: in test_missing_attr
        foo.nonexistent_method()
    E   AttributeError: 'Foo' object has no attribute 'nonexistent_method'

    ========================== short test summary info ============================
    FAILED tests/test_things.py::test_missing_attr
""")

_PYTEST_LITERAL_FAIL = textwrap.dedent("""\
    ================================== FAILURES ===================================
    _______________________ test_channel_count _________________________________

    tests/test_p0.py:42: in test_channel_count
        assert len(channels) == 5
    E       assert 4 == 5

    ========================== short test summary info ============================
    FAILED tests/test_p0.py::test_channel_count
""")


def _make_runtime() -> Any:
    """간단한 mock runtime with review_feedback attribute."""
    feedback = SimpleNamespace(
        approved=True,
        summary="",
        must_fix=[],
        gate_recommendation="",
    )

    def record_verify(ctx, *, passed):
        pass

    ctx = SimpleNamespace(review_feedback=feedback)
    runtime = SimpleNamespace(context=ctx)
    return runtime


# ---------------------------------------------------------------------------
# _verify_analyzer_enabled
# ---------------------------------------------------------------------------

def test_analyzer_enabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_VERIFY_ANALYZER", raising=False)
    assert _verify_analyzer_enabled() is True


def test_analyzer_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_VERIFY_ANALYZER", "0")
    assert _verify_analyzer_enabled() is False


def test_analyzer_enabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_VERIFY_ANALYZER", "1")
    assert _verify_analyzer_enabled() is True


def test_resolve_verify_check_uses_task_card_verify_command(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_build_verify_check(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "PASS", "passed": True, "verify_cmd": kwargs["verify_cmd"]}

    monkeypatch.setattr(
        "kodawari.autopilot.engine.gate_round.build_verify_check",
        fake_build_verify_check,
    )
    engine = SimpleNamespace(
        config=SimpleNamespace(project_root=tmp_path, feature="feature", verify_cmd="pytest -q"),
        state=SimpleNamespace(current_stage=None),
        _resolve_post_execution_qa=lambda _runtime: {},
        _implementation_verify_cmd=lambda: "node tests/test-agent-approval-handlers.js",
    )
    runtime = SimpleNamespace(
        task_label="T2: handlers",
        last_changed_files=["tests/test-agent-approval-handlers.js"],
        pre_compact_payload={},
    )

    result = _resolve_verify_check(engine, runtime)

    assert result["passed"] is True
    assert seen["verify_cmd"] == "node tests/test-agent-approval-handlers.js"


# ---------------------------------------------------------------------------
# _analyze_verify_stdout
# ---------------------------------------------------------------------------

def test_analyze_empty_stdout_returns_empty() -> None:
    assert _analyze_verify_stdout("", []) == []


def test_analyze_attribute_error_is_tier_b() -> None:
    results = _analyze_verify_stdout(_PYTEST_ATTR_ERROR, [])
    assert len(results) == 1
    assert results[0]["tier"] == "B"
    assert results[0]["authorized_mutation"] is False


def test_analyze_literal_fail_is_tier_a_unauthorized() -> None:
    results = _analyze_verify_stdout(_PYTEST_LITERAL_FAIL, [])
    assert len(results) == 1
    assert results[0]["tier"] == "A"
    assert results[0]["authorized_mutation"] is False
    assert results[0]["failure"]["file"] == "tests/test_p0.py"


def test_analyze_literal_fail_tier_a_authorized_with_allowlist() -> None:
    allowed = [
        {
            "file": "tests/test_p0.py",
            "match_kind": "literal_assert",
            "old_pattern": "assert len(channels) == 5",
            "new_pattern": "assert len(channels) == 4",
        }
    ]
    results = _analyze_verify_stdout(_PYTEST_LITERAL_FAIL, allowed)
    assert results[0]["tier"] == "A"
    assert results[0]["authorized_mutation"] is True


# ---------------------------------------------------------------------------
# _build_fix_round_msg
# ---------------------------------------------------------------------------

def test_build_msg_no_analysis_is_plain() -> None:
    msg = _build_fix_round_msg("tests failed", "some output", [])
    assert "Fix verify failure: tests failed" in msg
    assert "some output" in msg
    assert "Tier" not in msg


def test_build_msg_with_tier_b_shows_tier_b_line() -> None:
    analysis = [{"tier": "B", "authorized_mutation": False, "classification": "impl_failure"}]
    msg = _build_fix_round_msg("tests failed", "", analysis)
    assert "Tier B" in msg
    assert "do NOT mutate tests" in msg


def test_build_msg_with_tier_a_authorized_shows_authorized_line() -> None:
    analysis = [{"tier": "A", "authorized_mutation": True, "classification": "stale_assertion_candidate"}]
    msg = _build_fix_round_msg("tests failed", "", analysis)
    assert "Tier A authorized" in msg
    assert "allowed_test_mutations" in msg


def test_build_msg_with_tier_a_unauthorized_shows_unauthorized_line() -> None:
    analysis = [{"tier": "A", "authorized_mutation": False, "classification": "stale_assertion_candidate"}]
    msg = _build_fix_round_msg("tests failed", "", analysis)
    assert "Tier A unauthorized" in msg


# ---------------------------------------------------------------------------
# _reopen_for_fix_round
# ---------------------------------------------------------------------------

def test_reopen_disabled_analyzer_no_tier_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_VERIFY_ANALYZER", "0")
    runtime = _make_runtime()
    verify_check = {"blocking_reason": "4 != 5", "stdout_excerpt": _PYTEST_LITERAL_FAIL}
    analysis = _reopen_for_fix_round(runtime, verify_check)
    assert analysis == []
    assert runtime.context.review_feedback.approved is False
    assert "Tier" not in runtime.context.review_feedback.must_fix[0]


def test_reopen_enabled_analyzer_tier_b_in_must_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_VERIFY_ANALYZER", "1")
    runtime = _make_runtime()
    verify_check = {"blocking_reason": "AttributeError", "stdout_excerpt": _PYTEST_ATTR_ERROR}
    analysis = _reopen_for_fix_round(runtime, verify_check)
    assert len(analysis) == 1
    assert analysis[0]["tier"] == "B"
    must_fix_msg = runtime.context.review_feedback.must_fix[0]
    assert "Tier B" in must_fix_msg


def test_reopen_enabled_analyzer_returns_analysis_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_VERIFY_ANALYZER", "1")
    runtime = _make_runtime()
    verify_check = {"blocking_reason": "assert failed", "stdout_excerpt": _PYTEST_LITERAL_FAIL}
    analysis = _reopen_for_fix_round(runtime, verify_check)
    assert isinstance(analysis, list)
    assert len(analysis) == 1


def test_reopen_sets_review_feedback_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_VERIFY_ANALYZER", "0")
    runtime = _make_runtime()
    verify_check = {"blocking_reason": "test broke", "stdout_excerpt": ""}
    _reopen_for_fix_round(runtime, verify_check)
    fb = runtime.context.review_feedback
    assert fb.approved is False
    assert "test broke" in fb.summary
    assert fb.gate_recommendation == "REVIEW_FIX_REQUIRED"
    assert len(fb.must_fix) == 1


def test_reopen_with_allowed_mutations_authorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_VERIFY_ANALYZER", "1")
    runtime = _make_runtime()
    verify_check = {"blocking_reason": "assert failed", "stdout_excerpt": _PYTEST_LITERAL_FAIL}
    allowed = [
        {
            "file": "tests/test_p0.py",
            "match_kind": "literal_assert",
            "old_pattern": "assert len(channels) == 5",
            "new_pattern": "assert len(channels) == 4",
        }
    ]
    analysis = _reopen_for_fix_round(runtime, verify_check, allowed_mutations=allowed)
    assert analysis[0]["authorized_mutation"] is True
    assert "Tier A authorized" in runtime.context.review_feedback.must_fix[0]

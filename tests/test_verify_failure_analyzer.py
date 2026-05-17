"""Tests for the Tier-A verify-failure analyzer (2026-04-23 Item 8).

Pins the minimum behavior:
- Literal-equality assertions classify as Tier A
- Non-assertion exceptions (AttributeError, KeyError, etc.) are Tier B
- Authorization requires a matching ``allowed_test_mutations`` entry;
  absence still classifies as Tier A but ``authorized_mutation=False``
"""
from __future__ import annotations

import textwrap

import pytest

from kodawari.autopilot.verify.failure_analyzer import (
    FailureClassification,
    PytestFailure,
    classify_failure,
    parse_pytest_failures,
)


_PYTEST_OUTPUT_LITERAL_FAIL = textwrap.dedent("""\
    ============================= test session starts =============================
    collected 1 item

    tests/test_p0_hot_channels_and_dashboard.py::test_channel_count FAILED  [100%]

    ================================== FAILURES ===================================
    _______________________ test_channel_count _________________________________

    tests/test_p0_hot_channels_and_dashboard.py:42: in test_channel_count
        assert len(channels) == 5
    E       assert 4 == 5

    ========================== short test summary info ============================
    FAILED tests/test_p0_hot_channels_and_dashboard.py::test_channel_count
    ============================== 1 failed in 0.01s ==============================
""")


_PYTEST_OUTPUT_LITERAL_FAIL_WITH_WHERE = textwrap.dedent("""\
    ================================== FAILURES ===================================
    _______________________ test_channel_count _________________________________

    tests/test_p0_hot_channels_and_dashboard.py:42: in test_channel_count
        assert len(channels) == 5
    E       assert 4 == 5
    E        +  where 4 = len([1, 2, 3, 4])

    ========================== short test summary info ============================
    FAILED tests/test_p0_hot_channels_and_dashboard.py::test_channel_count
""")


_PYTEST_OUTPUT_ATTRIBUTE_ERROR = textwrap.dedent("""\
    ================================== FAILURES ===================================
    ____________________________ test_missing_attr ________________________________

    tests/test_things.py:17: in test_missing_attr
        foo.nonexistent_method()
    E   AttributeError: 'Foo' object has no attribute 'nonexistent_method'

    ========================== short test summary info ============================
    FAILED tests/test_things.py::test_missing_attr
""")


_PYTEST_OUTPUT_KEY_ERROR = textwrap.dedent("""\
    ================================== FAILURES ===================================
    _____________________________ test_key_missing ________________________________

    tests/test_map.py:5: in test_key_missing
        d["missing"]
    E   KeyError: 'missing'

    ========================== short test summary info ============================
    FAILED tests/test_map.py::test_key_missing
""")


def test_parse_extracts_literal_assert_failure() -> None:
    failures = parse_pytest_failures(_PYTEST_OUTPUT_LITERAL_FAIL)
    assert len(failures) == 1
    failure = failures[0]
    assert failure.file == "tests/test_p0_hot_channels_and_dashboard.py"
    assert failure.lineno == 42
    assert failure.exception_type == "AssertionError"
    assert "assert 4 == 5" in failure.assertion_line


def test_parse_prefers_assert_line_over_where_diagnostic() -> None:
    failures = parse_pytest_failures(_PYTEST_OUTPUT_LITERAL_FAIL_WITH_WHERE)
    assert len(failures) == 1
    assert failures[0].assertion_line.strip() == "E       assert 4 == 5"


def test_parse_extracts_attribute_error() -> None:
    failures = parse_pytest_failures(_PYTEST_OUTPUT_ATTRIBUTE_ERROR)
    assert len(failures) == 1
    assert failures[0].exception_type == "AttributeError"
    assert failures[0].file == "tests/test_things.py"


def test_parse_extracts_key_error() -> None:
    failures = parse_pytest_failures(_PYTEST_OUTPUT_KEY_ERROR)
    assert len(failures) == 1
    assert failures[0].exception_type == "KeyError"


def test_parse_empty_output() -> None:
    assert parse_pytest_failures("") == []
    assert parse_pytest_failures("all green") == []


def test_tier_a_unauthorized_when_no_allowlist() -> None:
    failure = PytestFailure(
        nodeid="tests/test_foo.py::test_count",
        file="tests/test_foo.py",
        lineno=10,
        assertion_line="E       assert 4 == 5",
        exception_type="AssertionError",
    )
    result = classify_failure(failure, allowed_mutations=None)
    assert result.tier == "A"
    assert result.classification == "stale_assertion_candidate"
    assert result.authorized_mutation is False
    assert result.lhs == "4"
    assert result.rhs == "5"
    assert "did not authorize" in result.reason


def test_tier_a_authorized_when_allowlist_matches() -> None:
    failure = PytestFailure(
        nodeid="tests/test_p0.py::test_channel_count",
        file="tests/test_p0.py",
        lineno=42,
        assertion_line="E       assert len(channels) == 5",
        exception_type="AssertionError",
    )
    allowed = [
        {
            "file": "tests/test_p0.py",
            "match_kind": "literal_assert",
            "old_pattern": "assert len(channels) == 5",
            "new_pattern": "assert len(channels) == 4",
            "behavior_change_id": "hot_channel_display_count",
        }
    ]
    result = classify_failure(failure, allowed_mutations=allowed)
    assert result.tier == "A"
    assert result.authorized_mutation is True
    assert result.mutation is not None
    assert result.mutation["new_pattern"] == "assert len(channels) == 4"


def test_tier_a_authorization_requires_file_match() -> None:
    failure = PytestFailure(
        nodeid="tests/test_one.py::t",
        file="tests/test_one.py",
        lineno=5,
        assertion_line="E       assert 4 == 5",
        exception_type="AssertionError",
    )
    allowed = [
        {
            "file": "tests/different_file.py",
            "match_kind": "literal_assert",
            "old_pattern": "assert 4 == 5",
            "new_pattern": "assert 4 == 4",
        }
    ]
    result = classify_failure(failure, allowed_mutations=allowed)
    assert result.tier == "A"
    assert result.authorized_mutation is False


def test_tier_a_authorization_requires_pattern_match() -> None:
    failure = PytestFailure(
        nodeid="tests/test_one.py::t",
        file="tests/test_one.py",
        lineno=5,
        assertion_line="E       assert 4 == 5",
        exception_type="AssertionError",
    )
    allowed = [
        {
            "file": "tests/test_one.py",
            "match_kind": "literal_assert",
            "old_pattern": "assert something_else == 5",
            "new_pattern": "assert something_else == 4",
        }
    ]
    result = classify_failure(failure, allowed_mutations=allowed)
    assert result.authorized_mutation is False


def test_tier_b_attribute_error_is_never_authorized() -> None:
    failure = PytestFailure(
        nodeid="tests/test_foo.py::test_attr",
        file="tests/test_foo.py",
        lineno=10,
        assertion_line="E   AttributeError: 'Foo' object has no attribute 'bar'",
        exception_type="AttributeError",
    )
    # Even if a matching mutation were declared (which would be a bug upstream),
    # Tier B never authorizes.
    allowed = [
        {
            "file": "tests/test_foo.py",
            "match_kind": "literal_assert",
            "old_pattern": "AttributeError",
            "new_pattern": "whatever",
        }
    ]
    result = classify_failure(failure, allowed_mutations=allowed)
    assert result.tier == "B"
    assert result.authorized_mutation is False


def test_tier_b_key_error() -> None:
    failure = PytestFailure(
        nodeid="tests/test_map.py::t",
        file="tests/test_map.py",
        lineno=5,
        assertion_line="E   KeyError: 'missing'",
        exception_type="KeyError",
    )
    result = classify_failure(failure)
    assert result.tier == "B"
    assert result.authorized_mutation is False


def test_tier_b_non_literal_rhs() -> None:
    """assert x == [1, 2, 3] is NOT a literal equality for our purposes."""
    failure = PytestFailure(
        nodeid="tests/test_list.py::t",
        file="tests/test_list.py",
        lineno=1,
        assertion_line="E       assert result == [1, 2, 3]",
        exception_type="AssertionError",
    )
    result = classify_failure(failure)
    assert result.tier == "B"
    assert result.classification == "non_literal_assertion"
    assert result.authorized_mutation is False


def test_tier_a_string_literal_is_ok() -> None:
    failure = PytestFailure(
        nodeid="tests/test_s.py::t",
        file="tests/test_s.py",
        lineno=1,
        assertion_line="E       assert status == 'pending'",
        exception_type="AssertionError",
    )
    result = classify_failure(failure)
    assert result.tier == "A"
    assert result.rhs == "'pending'"


def test_tier_a_boolean_literal_is_ok() -> None:
    failure = PytestFailure(
        nodeid="tests/test_b.py::t",
        file="tests/test_b.py",
        lineno=1,
        assertion_line="E       assert approved == False",
        exception_type="AssertionError",
    )
    result = classify_failure(failure)
    assert result.tier == "A"
    assert result.rhs == "False"


def test_end_to_end_parse_then_classify() -> None:
    failures = parse_pytest_failures(_PYTEST_OUTPUT_LITERAL_FAIL)
    assert len(failures) == 1
    allowed = [
        {
            "file": "tests/test_p0_hot_channels_and_dashboard.py",
            "match_kind": "literal_assert",
            "old_pattern": "assert len(channels) == 5",
            "new_pattern": "assert len(channels) == 4",
        }
    ]
    result = classify_failure(failures[0], allowed_mutations=allowed)
    assert result.tier == "A"
    assert result.authorized_mutation is True


def test_parse_with_where_diagnostic_still_classifies_as_tier_a() -> None:
    failures = parse_pytest_failures(_PYTEST_OUTPUT_LITERAL_FAIL_WITH_WHERE)
    assert len(failures) == 1
    result = classify_failure(failures[0], allowed_mutations=[])
    assert result.tier == "A"
    assert result.classification == "stale_assertion_candidate"


def test_to_dict_roundtrip() -> None:
    failure = PytestFailure(
        nodeid="tests/t.py::t",
        file="tests/t.py",
        lineno=1,
        assertion_line="E assert 1 == 2",
        exception_type="AssertionError",
    )
    result = classify_failure(failure)
    payload = result.to_dict()
    assert payload["tier"] == "A"
    assert payload["authorized_mutation"] is False
    assert payload["lhs"] == "1"
    assert payload["rhs"] == "2"

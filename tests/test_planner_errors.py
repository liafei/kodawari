"""Tests for planner_errors classification."""

from __future__ import annotations

import json

from kodawari.autopilot.planning.planner_errors import (
    PlannerErrorKind,
    classify_subprocess_result,
)


def test_timeout_classified() -> None:
    diag = classify_subprocess_result(
        returncode=None, stdout="", stderr="", timed_out=True,
    )
    assert diag.kind is PlannerErrorKind.TIMEOUT
    assert "WORKFLOW_PLANNER_TIMEOUT" in diag.hint


def test_executable_missing_classified() -> None:
    diag = classify_subprocess_result(
        returncode=None, stdout="", stderr="", start_error="[WinError 2] not found",
    )
    assert diag.kind is PlannerErrorKind.EXECUTABLE_MISSING
    assert "WORKFLOW_PLANNER_EXECUTABLE" in diag.hint


def test_nested_session_classified() -> None:
    diag = classify_subprocess_result(
        returncode=1,
        stdout="",
        stderr="Claude Code cannot be launched inside another Claude Code session.",
    )
    assert diag.kind is PlannerErrorKind.NESTED_SESSION
    assert "CLAUDECODE" in diag.hint


def test_error_max_turns_envelope_classified() -> None:
    envelope = {
        "type": "result",
        "subtype": "error_max_turns",
        "is_error": False,
        "num_turns": 2,
    }
    diag = classify_subprocess_result(
        returncode=0, stdout=json.dumps(envelope), stderr="",
    )
    assert diag.kind is PlannerErrorKind.MAX_TURNS
    assert "--max-turns" in diag.hint


def test_auth_403_from_envelope_classified() -> None:
    envelope = {
        "type": "result",
        "subtype": "error_api_error",
        "result": "Failed to authenticate. API Error: 403 Request not allowed",
    }
    diag = classify_subprocess_result(
        returncode=1, stdout=json.dumps(envelope), stderr="",
    )
    assert diag.kind is PlannerErrorKind.AUTH_FORBIDDEN
    assert "claude auth login" in diag.hint


def test_auth_403_from_stderr_classified() -> None:
    diag = classify_subprocess_result(
        returncode=1,
        stdout="",
        stderr="API Error: 403 Request not allowed",
    )
    assert diag.kind is PlannerErrorKind.AUTH_FORBIDDEN


def test_empty_output_at_exit_zero() -> None:
    diag = classify_subprocess_result(returncode=0, stdout="", stderr="")
    assert diag.kind is PlannerErrorKind.EMPTY_OUTPUT
    assert "--output-format json" in diag.hint


def test_invalid_json_at_exit_zero() -> None:
    diag = classify_subprocess_result(
        returncode=0,
        stdout="here is some free-form markdown, not JSON",
        stderr="",
    )
    assert diag.kind is PlannerErrorKind.INVALID_JSON


def test_unknown_failure_classified() -> None:
    diag = classify_subprocess_result(
        returncode=2, stdout="", stderr="something weird broke",
    )
    assert diag.kind is PlannerErrorKind.UNKNOWN
    assert "something weird broke" in diag.message


def test_render_format_stable() -> None:
    diag = classify_subprocess_result(
        returncode=None, stdout="", stderr="", timed_out=True,
    )
    rendered = diag.render()
    assert " — hint: " in rendered


def test_success_envelope_with_auth_words_in_plan_not_classified_as_forbidden() -> None:
    """Regression: plan JSON may contain words like 'authentication', '403', or 'forbidden'
    (e.g. HTTP endpoint docs, test fixture descriptions). A success envelope must be
    classified as UNKNOWN — not AUTH_FORBIDDEN — so the caller can parse the plan.
    """
    plan_with_auth_words = json.dumps({
        "summary": "Add authentication endpoint",
        "tasks": [
            {
                "id": "T1",
                "label": "Implement POST /auth/login",
                "description": "Returns 403 Forbidden when credentials are invalid",
                "files_to_change": ["backend/api/v1/routes/auth_routes.py"],
            }
        ],
    })
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": plan_with_auth_words,
    }
    diag = classify_subprocess_result(
        returncode=0, stdout=json.dumps(envelope), stderr="",
    )
    # Must be UNKNOWN (caller will parse the plan) — NOT AUTH_FORBIDDEN
    assert diag.kind is PlannerErrorKind.UNKNOWN, (
        f"Expected UNKNOWN for success envelope with auth words in plan, got {diag.kind}: {diag.message}"
    )


def test_success_envelope_with_forbidden_in_result_not_auth_forbidden() -> None:
    """A successful plan that mentions 'forbidden' in test coverage hints must not
    be mis-classified as an auth error."""
    plan = json.dumps({
        "summary": "Add rate limiting",
        "coverage_hints": ["returns 403 Forbidden for unauthenticated requests"],
        "tasks": [],
    })
    envelope = {"type": "result", "subtype": "success", "is_error": False, "result": plan}
    diag = classify_subprocess_result(returncode=0, stdout=json.dumps(envelope), stderr="")
    assert diag.kind is PlannerErrorKind.UNKNOWN


def test_error_api_error_with_authentication_word_not_classified_as_auth_forbidden() -> None:
    """Regression (decision 6): the word 'authentication' alone MUST NOT
    trigger AUTH_FORBIDDEN when the actual error is something else (e.g. a
    500 internal error that incidentally mentions the auth service).

    Before the fix: `r"authentication"` in _AUTH_403_PATTERNS caused any
    error_api_error whose result text contained the word — common in
    microservice error messages — to be classified as AUTH_FORBIDDEN with
    a misleading "run claude auth login" hint.
    After: only \b403\b, 'Request not allowed', and 'forbidden' trigger
    AUTH_FORBIDDEN; other errors surface as API_ERROR with the real text.
    """
    envelope = {
        "type": "result",
        "subtype": "error_api_error",
        "result": "API Error: 500 internal; authentication service timeout",
    }
    diag = classify_subprocess_result(
        returncode=1, stdout=json.dumps(envelope), stderr="",
    )
    assert diag.kind is PlannerErrorKind.API_ERROR, (
        f"expected API_ERROR for 500 that mentions 'authentication' word, "
        f"got {diag.kind}: {diag.message}"
    )
    # The API error text must be carried through so callers see the real cause
    assert "500 internal" in diag.message


def test_error_api_error_with_real_403_still_classified_as_auth_forbidden() -> None:
    """Decision 6 safety: a genuine 403 must still be caught."""
    envelope = {
        "type": "result",
        "subtype": "error_api_error",
        "result": "API Error: 403 Request not allowed",
    }
    diag = classify_subprocess_result(
        returncode=1, stdout=json.dumps(envelope), stderr="",
    )
    assert diag.kind is PlannerErrorKind.AUTH_FORBIDDEN


def test_fallback_regex_no_longer_matches_bare_authentication_word() -> None:
    """Decision 6: the fallback regex (for non-envelope stderr) must not
    fire on the bare word 'authentication' either.
    """
    diag = classify_subprocess_result(
        returncode=2,
        stdout="",
        stderr="connection reset during authentication handshake",
    )
    assert diag.kind is PlannerErrorKind.UNKNOWN, (
        f"expected UNKNOWN for generic stderr mentioning 'authentication', "
        f"got {diag.kind}"
    )


def test_home_access_error_from_node_lstat_classified() -> None:
    """Real Node EPERM lstat stderr from Windows Controlled Folder Access must
    classify as HOME_ACCESS_ERROR, not UNKNOWN."""
    stderr = (
        "node:internal/fs/utils:351\n    throw err;\n    ^\n\n"
        "Error: EPERM: operation not permitted, lstat 'C:\\\\Users\\\\liafei'\n"
        "    at Object.realpathSync (node:fs:2677:7)\n"
    )
    diag = classify_subprocess_result(returncode=1, stdout="", stderr=stderr)
    assert diag.kind is PlannerErrorKind.HOME_ACCESS_ERROR


def test_home_access_error_from_errno_4048_classified() -> None:
    """Alternate Node error shape that surfaces the Windows EPERM errno code."""
    stderr = (
        "at node:internal/main/run_main_module:23:47 {\n"
        "  errno: -4048,\n  syscall: 'lstat',\n  code: 'EPERM',\n"
        "  path: 'C:\\\\Users\\\\liafei'\n}\n\nNode.js v20.5.1"
    )
    diag = classify_subprocess_result(returncode=1, stdout="", stderr=stderr)
    assert diag.kind is PlannerErrorKind.HOME_ACCESS_ERROR


def test_home_access_error_hint_mentions_controlled_folder_access() -> None:
    """Remediation hint must point the operator at Windows Controlled Folder
    Access and give a one-liner Node reproduction."""
    stderr = "Error: EPERM: operation not permitted, lstat 'C:\\\\Users\\\\liafei'"
    diag = classify_subprocess_result(returncode=1, stdout="", stderr=stderr)
    assert "Controlled Folder Access" in diag.hint
    assert "lstatSync" in diag.hint

"""Planner subprocess error classification.

Maps raw subprocess / Claude CLI failures to actionable error categories
with concrete remediation hints. Used by planning_agent.generate_plan and
plan_reviewer.review_plan.

The goal is to turn opaque strings like
    "planner exited with code 1: {\"type\":\"result\", ...}"
into structured diagnoses so the user knows what to fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import re
from typing import Any


class PlannerErrorKind(str, Enum):
    """Closed enum of known planner failure modes.

    Keep this list stable; callers dispatch on it.
    """

    NESTED_SESSION = "nested_session"
    AUTH_FORBIDDEN = "auth_forbidden"
    AUTH_MISSING = "auth_missing"
    MAX_TURNS = "max_turns"
    API_TIMEOUT = "api_timeout"
    API_ERROR = "api_error"
    TIMEOUT = "timeout"
    EXECUTABLE_MISSING = "executable_missing"
    HOME_ACCESS_ERROR = "home_access_error"
    EMPTY_OUTPUT = "empty_output"
    INVALID_JSON = "invalid_json"
    HTTP_TIMEOUT = "planner_http_timeout"
    HTTP_REMOTE_CLOSED = "planner_remote_closed"
    HTTP_4XX = "planner_http_4xx"
    HTTP_5XX = "planner_http_5xx"
    HTTP_ERROR = "planner_http_error"
    CONTEXT_OVERFLOW = "planner_context_overflow"
    STREAMING_REQUIRED = "planner_streaming_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlannerErrorDiagnosis:
    kind: PlannerErrorKind
    message: str
    hint: str

    def render(self) -> str:
        """Human-readable single line for logs / UI."""
        return f"{self.message} — hint: {self.hint}"


_AUTH_403_PATTERNS = (
    r"\b403\b",
    r"Request not allowed",
    r"forbidden",
    # r"authentication" — REMOVED 2026-04-17: this word appears too often in
    # legitimate plan/result content (auth endpoints, auth middleware, token
    # discussions) and caused false positives in error_api_error classification
    # as well as the fallback regex. If you need to classify a genuine auth
    # failure that doesn't mention 403 / forbidden / Request not allowed, add
    # a more specific pattern (e.g. r"401 unauthorized" or r"invalid token").
)

_AUTH_MISSING_PATTERNS = (
    r"not logged in",
    r"please log in",
    r"run `claude auth`",
    r"no valid credentials",
)

_NESTED_PATTERNS = (
    r"cannot be launched inside another Claude Code session",
    r"nested Claude Code",
)

_HOME_ACCESS_PATTERNS = (
    r"EPERM.*lstat",
    r"lstat.*EPERM",
    r"operation not permitted,\s*lstat",
    r"errno:\s*-4048",
)


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(re.search(p, lowered, re.IGNORECASE) for p in patterns)


def classify_subprocess_result(
    *,
    returncode: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool = False,
    start_error: str = "",
) -> PlannerErrorDiagnosis:
    """Classify a finished (or failed-to-start) subprocess result.

    returncode is None when the process never finished normally (timeout /
    start error). start_error is the OSError message when the executable
    could not be launched at all.
    """
    if start_error:
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.EXECUTABLE_MISSING,
            message=f"planner executable could not start: {start_error}",
            hint="verify `claude` CLI is installed and on PATH, or set WORKFLOW_PLANNER_EXECUTABLE",
        )
    if timed_out:
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.TIMEOUT,
            message="planner wall-clock timeout",
            hint="increase WORKFLOW_PLANNER_TIMEOUT (default 300s) or reduce context size",
        )

    combined = f"{stdout}\n{stderr}".strip()

    # Check for nested-session block even before parsing envelope
    if _contains_any(combined, _NESTED_PATTERNS):
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.NESTED_SESSION,
            message="claude CLI refused to run inside an existing Claude Code session",
            hint="run from a terminal without CLAUDECODE env; _subprocess_env() should already strip it — check your launcher",
        )

    # Node-level EPERM on lstat of the user home — Windows Controlled Folder
    # Access / ACL denies Node access to the path before cli.js can run.
    # Python-side `os.lstat(home)` may succeed under the same ACL, so we
    # catch the failure from Node's stderr instead of preflight-probing.
    if _contains_any(combined, _HOME_ACCESS_PATTERNS):
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.HOME_ACCESS_ERROR,
            message="claude CLI cannot lstat Windows user home (Node EPERM)",
            hint=(
                "check Windows Controlled Folder Access (Virus & threat protection > "
                "Ransomware protection) and grant Node access to %USERPROFILE%; "
                "verify with: node -e \"require('fs').lstatSync(process.env.USERPROFILE)\""
            ),
        )

    # Parse Claude CLI envelope (preferred over textual match)
    envelope: dict[str, Any] | None = None
    try:
        envelope = json.loads(stdout or "{}")
        if not isinstance(envelope, dict):
            envelope = None
    except (json.JSONDecodeError, ValueError):
        envelope = None

    if envelope is not None:
        subtype = str(envelope.get("subtype") or "").strip().lower()
        result_text = str(envelope.get("result") or "")
        if subtype == "success":
            # Caller (planning_agent) reads the plan from result_text; we return UNKNOWN
            # so the caller proceeds to _parse_response. Do NOT run the auth-pattern
            # fallback below — the plan body may legitimately contain words like
            # "authentication" or "403" (e.g. HTTP status codes in test fixtures).
            return PlannerErrorDiagnosis(
                kind=PlannerErrorKind.UNKNOWN,
                message="",
                hint="",
            )
        if subtype == "error_max_turns":
            return PlannerErrorDiagnosis(
                kind=PlannerErrorKind.MAX_TURNS,
                message="claude CLI hit --max-turns before producing a plan",
                hint="increase --max-turns in planner_agent._build_command or reduce required tool calls",
            )
        if subtype in ("error_api_timeout",):
            return PlannerErrorDiagnosis(
                kind=PlannerErrorKind.API_TIMEOUT,
                message="claude API timed out mid-response",
                hint="retry; if persistent, check Anthropic status or reduce prompt size",
            )
        if subtype in ("error_api_error",):
            # Auth errors surface as error_api_error with 403 in result body
            if _contains_any(result_text, _AUTH_403_PATTERNS):
                return PlannerErrorDiagnosis(
                    kind=PlannerErrorKind.AUTH_FORBIDDEN,
                    message="claude API returned 403 Request not allowed",
                    hint="subscription auth is missing or not permitted for -p; run `claude auth login` in this shell, or set ANTHROPIC_API_KEY + WORKFLOW_PLANNER_CLIENT=gateway",
                )
            return PlannerErrorDiagnosis(
                kind=PlannerErrorKind.API_ERROR,
                message=f"claude API returned an error: {result_text[:200]}",
                hint="inspect claude CLI stderr; retry after verifying credentials",
            )

    # Fall back to regex on combined output only when no envelope was parsed or
    # subtype was unrecognised. Never run for subtype==success (handled above).
    if _contains_any(combined, _AUTH_403_PATTERNS):
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.AUTH_FORBIDDEN,
            message="planner reported 403 / forbidden",
            hint="run `claude auth login` in this shell, or set ANTHROPIC_API_KEY + WORKFLOW_PLANNER_CLIENT=gateway",
        )
    if _contains_any(combined, _AUTH_MISSING_PATTERNS):
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.AUTH_MISSING,
            message="planner reported missing credentials",
            hint="run `claude auth login` or set ANTHROPIC_API_KEY",
        )

    if returncode == 0:
        # Exit 0 but no parseable plan output
        if not stdout.strip():
            return PlannerErrorDiagnosis(
                kind=PlannerErrorKind.EMPTY_OUTPUT,
                message="planner exited 0 with empty stdout",
                hint="check if --output-format json is supported by the claude CLI version installed",
            )
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.INVALID_JSON,
            message="planner output was not valid JSON",
            hint="claude CLI may have emitted markdown; update to latest claude CLI or check prompt instructions",
        )

    detail = (stderr.strip() or stdout[:500].strip() or "(no output)")
    return PlannerErrorDiagnosis(
        kind=PlannerErrorKind.UNKNOWN,
        message=f"planner exited with code {returncode}: {detail[:300]}",
        hint="inspect full stdout/stderr; this is an unclassified failure",
    )


def classify_chat_result_failure(*, kind: str, detail: str = "") -> PlannerErrorDiagnosis:
    """Classify a failed OpenAI-compatible chat transport call."""
    normalized = str(kind or "").strip().lower()
    clean_detail = str(detail or "").strip()
    if normalized == "http_timeout":
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.HTTP_TIMEOUT,
            message=f"planner HTTP request timed out{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="retry; if persistent, inspect planner request bytes and reduce planning context or increase WORKFLOW_PLANNER_TIMEOUT",
        )
    if normalized == "remote_closed":
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.HTTP_REMOTE_CLOSED,
            message=f"planner HTTP connection closed before response{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="inspect endpoint/proxy limits and request bytes; retry with reduced planning context if body size is high",
        )
    if normalized == "context_overflow":
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.CONTEXT_OVERFLOW,
            message=f"planner HTTP context overflow{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="reduce planning context or configure a smaller planner context budget for this transport",
        )
    if normalized == "streaming_required":
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.STREAMING_REQUIRED,
            message=f"planner endpoint rejected non-streaming chat request{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="use an endpoint that supports non-streaming chat completions or add streaming support before using this planner transport",
        )
    if normalized in {"auth_missing", "auth_forbidden", "auth_invalid"}:
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.AUTH_MISSING if normalized == "auth_missing" else PlannerErrorKind.AUTH_FORBIDDEN,
            message=f"planner HTTP authentication failed{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="verify the transport api_key_env is set and the key is allowed to use the requested model",
        )
    if normalized == "http_4xx":
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.HTTP_4XX,
            message=f"planner HTTP 4xx error{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="verify base_url/api_format/model compatibility and inspect the redacted response body",
        )
    if normalized == "http_5xx":
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.HTTP_5XX,
            message=f"planner HTTP 5xx error{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="retry; if persistent, check provider status or gateway routing",
        )
    if normalized == "redirect_blocked":
        return PlannerErrorDiagnosis(
            kind=PlannerErrorKind.HTTP_ERROR,
            message=f"planner HTTP redirect was blocked{(': ' + clean_detail[:200]) if clean_detail else ''}",
            hint="fix the base_url so Authorization is not redirected across origins",
        )
    return PlannerErrorDiagnosis(
        kind=PlannerErrorKind.HTTP_ERROR,
        message=f"planner HTTP request failed{(': ' + clean_detail[:200]) if clean_detail else ''}",
        hint="inspect endpoint, proxy settings, and transport configuration",
    )


__all__ = [
    "PlannerErrorKind",
    "PlannerErrorDiagnosis",
    "classify_chat_result_failure",
    "classify_subprocess_result",
]

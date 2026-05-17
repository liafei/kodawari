"""OpenAI-compatible chat transport client.

This module handles transport mechanics only. Callers remain responsible for
interpreting model content and validating role-specific JSON schemas.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
import json
import os
import ssl
import threading
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from kodawari.autopilot.core.http_safety import RedirectBlocked, SafeRedirectHandler
from kodawari.autopilot.core.model_config import WorkflowTransportConfig
from kodawari.autopilot.core.secret_redactor import redact_secret_text


_OPENAI_CHAT_API_FORMATS = {"openai", "openai_chat"}
_LOCALHOSTS = {"localhost", "127.0.0.1", "::1"}
_CONTEXT_OVERFLOW_MARKERS = (
    "context_length",
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
)
_STREAMING_REQUIRED_MARKERS = (
    "stream",
    "streaming",
    "stream=true",
    "stream must be true",
)
_API_KEY_PLACEHOLDERS = {
    "yourkey",
    "your-key",
    "your_api_key",
    "api_key",
    "apikey",
    "<token>",
    "<your-key>",
    "<your_api_key>",
    "token",
    "你的key",
}


_RETRYABLE_KINDS = {"http_5xx", "http_timeout", "remote_closed", "network_error"}


@dataclass(frozen=True)
class ChatCallResult:
    ok: bool
    raw_text: str = ""
    kind: str = ""
    detail: str = ""
    http_status: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    wallclock_ms: int = 0
    endpoint: str = ""
    model_warning: dict[str, Any] = field(default_factory=dict)
    attempts: tuple[dict[str, Any], ...] = ()
    # OpenAI-compatible prompt cache stats (DeepSeek v4, Qwen, etc.). 0 when the
    # provider does not report cache fields (vanilla OpenAI, mimo, fakes).
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0


def parse_prompt_cache_tokens(raw_text: str) -> tuple[int, int]:
    """Extract usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens from a raw
    OpenAI-compatible JSON response body. Returns (0, 0) for any malformed shape
    or absent fields — providers that don't expose cache stats degrade silently.
    """
    if not raw_text:
        return 0, 0
    try:
        body = json.loads(raw_text)
    except (TypeError, ValueError):
        return 0, 0
    if not isinstance(body, dict):
        return 0, 0
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    try:
        hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    except (TypeError, ValueError):
        hit = 0
    try:
        miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    except (TypeError, ValueError):
        miss = 0
    return max(0, hit), max(0, miss)


def call_openai_chat(
    *,
    transport: WorkflowTransportConfig,
    model: str,
    system: str,
    user: str,
    timeout_seconds: int,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None = None,
    max_retries: int = 0,
    total_timeout_seconds: int = 0,
    max_tokens: int = 0,
    response_format: dict[str, Any] | None = None,
) -> ChatCallResult:
    endpoint, endpoint_error = normalize_openai_chat_endpoint(_transport_base_url(transport), api_format=transport.api_format)
    if endpoint_error:
        return _failure(endpoint_error, endpoint=endpoint)
    api_key = _api_key(transport)
    if not api_key:
        return _failure("auth_missing", detail=f"api key env is missing: {transport.api_key_env or '<empty>'}", endpoint=endpoint)
    api_key_error = _api_key_error(api_key, env_name=transport.api_key_env)
    if api_key_error:
        return _failure("auth_invalid", detail=api_key_error, endpoint=endpoint)

    payload = {
        "model": str(model or "").strip(),
        "messages": [
            {"role": "system", "content": str(system or "")},
            {"role": "user", "content": str(user or "")},
        ],
        "temperature": 0,
        "stream": False,
    }
    if int(max_tokens or 0) > 0:
        payload["max_tokens"] = int(max_tokens)
    if response_format:
        payload["response_format"] = dict(response_format)
    request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urlrequest.Request(
        endpoint,
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urlrequest.build_opener(SafeRedirectHandler)
    max_attempts = max(1, int(max_retries or 0) + 1)
    total_budget = max(0, int(total_timeout_seconds or 0))
    started = time.monotonic()
    attempts_log: list[dict[str, Any]] = []
    last_failure: ChatCallResult | None = None
    min_attempt_timeout = 5
    requested_timeout = max(min_attempt_timeout, int(timeout_seconds or 60))
    for attempt in range(1, max_attempts + 1):
        per_attempt_timeout = requested_timeout
        if total_budget > 0:
            elapsed = time.monotonic() - started
            remaining = total_budget - elapsed
            if remaining < min_attempt_timeout:
                break
            per_attempt_timeout = min(per_attempt_timeout, max(min_attempt_timeout, int(remaining)))
        attempt_started = time.monotonic()
        try:
            raw_bytes = _read_response_bytes_with_watchdog(
                request,
                timeout_seconds=per_attempt_timeout,
                opener=opener,
                urlopen_fn=urlopen_fn,
            )
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            warning = _model_warning(raw_text, requested_model=model)
            attempts_log.append(
                _attempt_entry(
                    idx=attempt,
                    kind="ok",
                    detail="",
                    http_status=0,
                    request_bytes=len(request_body),
                    response_bytes=len(raw_bytes),
                    attempt_started=attempt_started,
                    timeout_used=per_attempt_timeout,
                )
            )
            cache_hit, cache_miss = parse_prompt_cache_tokens(raw_text)
            return ChatCallResult(
                ok=True,
                raw_text=raw_text,
                kind="ok",
                request_bytes=len(request_body),
                response_bytes=len(raw_bytes),
                wallclock_ms=_elapsed_ms(started),
                endpoint=endpoint,
                model_warning=warning,
                attempts=tuple(attempts_log),
                prompt_cache_hit_tokens=cache_hit,
                prompt_cache_miss_tokens=cache_miss,
            )
        except RedirectBlocked as exc:
            failure = _failure(
                "redirect_blocked",
                detail=str(exc),
                endpoint=endpoint,
                request_bytes=len(request_body),
                started=started,
            )
            attempts_log.append(_attempt_from_failure(attempt, failure, attempt_started, per_attempt_timeout))
            return _with_attempts(failure, attempts_log)
        except urlerror.HTTPError as exc:
            body = _safe_http_body(exc)
            status = int(getattr(exc, "code", 0) or 0)
            kind = _http_error_kind(status, body)
            failure = _failure(
                kind,
                detail=f"http {status}: {redact_secret_text(body)}",
                http_status=status,
                endpoint=endpoint,
                request_bytes=len(request_body),
                response_bytes=len(body.encode("utf-8", errors="replace")),
                started=started,
            )
            attempts_log.append(_attempt_from_failure(attempt, failure, attempt_started, per_attempt_timeout))
            if kind not in _RETRYABLE_KINDS or attempt >= max_attempts:
                return _with_attempts(failure, attempts_log)
            last_failure = failure
            _sleep_before_retry(attempt, started, total_budget)
        except TimeoutError as exc:
            failure = _failure(
                "http_timeout",
                detail=redact_secret_text(str(exc) or "timed out"),
                endpoint=endpoint,
                request_bytes=len(request_body),
                started=started,
            )
            attempts_log.append(_attempt_from_failure(attempt, failure, attempt_started, per_attempt_timeout))
            if attempt >= max_attempts:
                return _with_attempts(failure, attempts_log)
            last_failure = failure
            _sleep_before_retry(attempt, started, total_budget)
        except UnicodeEncodeError as exc:
            failure = _failure(
                "auth_invalid",
                detail=f"transport api key/env contains characters that cannot be sent in an HTTP header: {redact_secret_text(str(exc))}",
                endpoint=endpoint,
                request_bytes=len(request_body),
                started=started,
            )
            attempts_log.append(_attempt_from_failure(attempt, failure, attempt_started, per_attempt_timeout))
            return _with_attempts(failure, attempts_log)
        except (urlerror.URLError, ssl.SSLError, OSError) as exc:
            kind = "remote_closed" if _looks_remote_closed(str(exc)) else "network_error"
            failure = _failure(
                kind,
                detail=redact_secret_text(str(exc)),
                endpoint=endpoint,
                request_bytes=len(request_body),
                started=started,
            )
            attempts_log.append(_attempt_from_failure(attempt, failure, attempt_started, per_attempt_timeout))
            if attempt >= max_attempts:
                return _with_attempts(failure, attempts_log)
            last_failure = failure
            _sleep_before_retry(attempt, started, total_budget)
    fallback = last_failure or _failure(
        "network_error",
        detail="endpoint request failed",
        endpoint=endpoint,
        request_bytes=len(request_body),
        started=started,
    )
    return _with_attempts(fallback, attempts_log)


def _read_response_bytes_with_watchdog(
    request: urlrequest.Request,
    *,
    timeout_seconds: int,
    opener: Any,
    urlopen_fn: Callable[[urlrequest.Request, int], Any] | None,
) -> bytes:
    outcomes: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    timeout_value = max(1, int(timeout_seconds or 1))

    def _worker() -> None:
        try:
            response_obj = (
                urlopen_fn(request, timeout_value)
                if urlopen_fn is not None
                else opener.open(request, timeout=timeout_value)
            )
            if hasattr(response_obj, "__enter__"):
                with response_obj as response:
                    raw_bytes = response.read()
            else:
                raw_bytes = response_obj.read()
            outcomes.put(("ok", raw_bytes))
        except Exception as exc:  # propagate transport exceptions to the caller thread
            outcomes.put(("error", exc))

    thread = threading.Thread(target=_worker, name="workflow-openai-chat-request", daemon=True)
    thread.start()
    try:
        kind, payload = outcomes.get(timeout=timeout_value)
    except queue.Empty as exc:
        raise TimeoutError(f"request exceeded hard timeout ({timeout_value}s)") from exc
    if kind == "error":
        raise payload
    return bytes(payload or b"")


def _attempt_entry(
    *,
    idx: int,
    kind: str,
    detail: str,
    http_status: int,
    request_bytes: int,
    response_bytes: int,
    attempt_started: float,
    timeout_used: int,
) -> dict[str, Any]:
    return {
        "idx": int(idx),
        "kind": str(kind or ""),
        "detail": str(detail or ""),
        "http_status": int(http_status or 0),
        "request_bytes": int(request_bytes or 0),
        "response_bytes": int(response_bytes or 0),
        "wallclock_ms": _elapsed_ms(attempt_started),
        "timeout_seconds": int(timeout_used or 0),
    }


def _attempt_from_failure(
    attempt: int,
    failure: ChatCallResult,
    attempt_started: float,
    timeout_used: int,
) -> dict[str, Any]:
    return _attempt_entry(
        idx=attempt,
        kind=failure.kind,
        detail=failure.detail,
        http_status=failure.http_status,
        request_bytes=failure.request_bytes,
        response_bytes=failure.response_bytes,
        attempt_started=attempt_started,
        timeout_used=timeout_used,
    )


def _with_attempts(failure: ChatCallResult, attempts_log: list[dict[str, Any]]) -> ChatCallResult:
    return ChatCallResult(
        ok=failure.ok,
        raw_text=failure.raw_text,
        kind=failure.kind,
        detail=failure.detail,
        http_status=failure.http_status,
        request_bytes=failure.request_bytes,
        response_bytes=failure.response_bytes,
        wallclock_ms=failure.wallclock_ms,
        endpoint=failure.endpoint,
        model_warning=failure.model_warning,
        attempts=tuple(attempts_log),
    )


def _sleep_before_retry(attempt: int, started: float, total_budget: int) -> None:
    delay = min(2.0, 0.25 * attempt)
    if total_budget > 0:
        remaining = total_budget - (time.monotonic() - started)
        if remaining <= 0:
            return
        delay = min(delay, max(0.0, remaining - 0.1))
    if delay > 0:
        time.sleep(delay)


def normalize_openai_chat_endpoint(base_url: str, *, api_format: str) -> tuple[str, str]:
    raw = str(base_url or "").strip()
    if not raw:
        return "", "api_base_url_missing"
    parsed = urlparse.urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return "", "endpoint_malformed"
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in _LOCALHOSTS:
        return "", "non_https_endpoint"
    path = parsed.path.rstrip("/")
    normalized_format = str(api_format or "").strip().lower()
    if "anthropic" in path.lower() and normalized_format in _OPENAI_CHAT_API_FORMATS:
        return "", "openai_transport_points_to_anthropic_endpoint"
    if normalized_format in {"", "auto"}:
        return "", "api_format_not_chat_completions"
    if normalized_format not in _OPENAI_CHAT_API_FORMATS:
        return "", "api_format_not_chat_completions"
    if path.endswith("/chat/completions"):
        endpoint_path = path
    elif path.endswith("/v1"):
        endpoint_path = f"{path}/chat/completions"
    elif not path:
        endpoint_path = "/v1/chat/completions"
    else:
        endpoint_path = f"{path}/v1/chat/completions"
    return urlparse.urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", "")), ""


def _transport_base_url(transport: WorkflowTransportConfig) -> str:
    if transport.base_url:
        return str(transport.base_url).strip()
    if transport.base_url_env:
        return str(os.environ.get(transport.base_url_env) or "").strip()
    return ""


def _api_key(transport: WorkflowTransportConfig) -> str:
    if not transport.api_key_env:
        return ""
    return str(os.environ.get(transport.api_key_env) or "").strip()


def _api_key_error(api_key: str, *, env_name: str) -> str:
    normalized = str(api_key or "").strip()
    lowered = normalized.lower()
    if lowered in _API_KEY_PLACEHOLDERS or "你的" in normalized:
        return f"api key env {env_name or '<empty>'} appears to contain a placeholder; set the real provider key"
    if any(ch in normalized for ch in ("\r", "\n", "\x00")):
        return f"api key env {env_name or '<empty>'} contains invalid control characters"
    try:
        f"Bearer {normalized}".encode("ascii")
    except UnicodeEncodeError:
        return f"api key env {env_name or '<empty>'} contains non-ASCII characters; set the real provider key"
    return ""


def _safe_http_body(exc: urlerror.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
    return body


def _http_error_kind(status: int, body: str) -> str:
    lowered = str(body or "").lower()
    if status in {400, 413} and any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS):
        return "context_overflow"
    if status in {400, 501} and any(marker in lowered for marker in _STREAMING_REQUIRED_MARKERS):
        return "streaming_required"
    if status == 401 or status == 403:
        return "auth_forbidden"
    if 400 <= status < 500:
        return "http_4xx"
    if 500 <= status < 600:
        return "http_5xx"
    return "http_error"


def _model_warning(raw_text: str, *, requested_model: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    actual = str(payload.get("model") or "").strip()
    requested = str(requested_model or "").strip()
    if actual and requested and actual != requested:
        return {
            "code": "probe_model_substituted",
            "message": "chat response model differs from requested model",
            "requested": requested,
            "actual": actual,
        }
    return {}


def _looks_remote_closed(message: str) -> bool:
    lowered = str(message or "").lower()
    return "remote end closed" in lowered or "connection reset" in lowered or "connection aborted" in lowered


def _elapsed_ms(started: float | None) -> int:
    if started is None:
        return 0
    return max(0, int((time.monotonic() - started) * 1000))


def _failure(
    kind: str,
    *,
    detail: str = "",
    http_status: int = 0,
    endpoint: str = "",
    request_bytes: int = 0,
    response_bytes: int = 0,
    started: float | None = None,
) -> ChatCallResult:
    return ChatCallResult(
        ok=False,
        kind=kind,
        detail=redact_secret_text(detail),
        http_status=http_status,
        endpoint=endpoint,
        request_bytes=request_bytes,
        response_bytes=response_bytes,
        wallclock_ms=_elapsed_ms(started),
    )


__all__ = ["ChatCallResult", "call_openai_chat", "normalize_openai_chat_endpoint"]

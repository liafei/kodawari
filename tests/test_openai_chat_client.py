from __future__ import annotations

import io
import json
import threading
import time
from urllib import error as urlerror
from urllib import request as urlrequest

from kodawari.autopilot.core.model_config import WorkflowTransportConfig
from kodawari.autopilot.core.openai_chat_client import (
    call_openai_chat,
    normalize_openai_chat_endpoint,
    parse_prompt_cache_tokens,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body


def _transport() -> WorkflowTransportConfig:
    return WorkflowTransportConfig(
        name="mimo_chat",
        kind="http",
        driver="openai_compatible",
        interface="chat",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_TEST_OPENAI_KEY",
    )


def test_normalize_openai_chat_endpoint_variants() -> None:
    assert normalize_openai_chat_endpoint("https://example.test/v1", api_format="openai_chat")[0] == "https://example.test/v1/chat/completions"
    assert normalize_openai_chat_endpoint("https://example.test", api_format="openai_chat")[0] == "https://example.test/v1/chat/completions"
    assert normalize_openai_chat_endpoint("https://example.test/anthropic", api_format="openai_chat")[1] == "openai_transport_points_to_anthropic_endpoint"


def test_call_openai_chat_success(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")

    def _fake_urlopen(request: urlrequest.Request, timeout: int) -> _Response:
        assert timeout == 9
        payload = json.loads(request.data.decode("utf-8"))  # type: ignore[union-attr]
        assert payload["model"] == "mimo-v2.5-pro"
        assert payload["stream"] is False
        return _Response({"model": "mimo-v2.5-pro", "choices": [{"message": {"content": "{\"ok\": true}"}}]})

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=9,
        urlopen_fn=_fake_urlopen,
    )

    assert result.ok is True
    assert result.kind == "ok"
    assert result.request_bytes > 0
    assert result.response_bytes > 0
    # Provider without cache stats -> defaults are 0 (mimo / vanilla OpenAI compat).
    assert result.prompt_cache_hit_tokens == 0
    assert result.prompt_cache_miss_tokens == 0


def test_call_openai_chat_records_prompt_cache_tokens_when_provider_reports_them(monkeypatch) -> None:
    """DeepSeek-style provider returns prompt_cache_*_tokens in usage; surface on ChatCallResult."""
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")

    def _fake_urlopen(_request: urlrequest.Request, _timeout: int) -> _Response:
        return _Response(
            {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "{\"ok\": true}"}}],
                "usage": {
                    "prompt_tokens": 1024,
                    "completion_tokens": 32,
                    "total_tokens": 1056,
                    "prompt_cache_hit_tokens": 768,
                    "prompt_cache_miss_tokens": 256,
                },
            }
        )

    result = call_openai_chat(
        transport=_transport(),
        model="deepseek-v4-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=_fake_urlopen,
    )

    assert result.ok is True
    assert result.prompt_cache_hit_tokens == 768
    assert result.prompt_cache_miss_tokens == 256


def test_parse_prompt_cache_tokens_handles_malformed_payloads() -> None:
    assert parse_prompt_cache_tokens("") == (0, 0)
    assert parse_prompt_cache_tokens("not json") == (0, 0)
    assert parse_prompt_cache_tokens(json.dumps({"usage": "string-not-dict"})) == (0, 0)
    assert parse_prompt_cache_tokens(json.dumps({"usage": {"prompt_cache_hit_tokens": "abc"}})) == (0, 0)
    assert parse_prompt_cache_tokens(json.dumps({})) == (0, 0)
    assert parse_prompt_cache_tokens(
        json.dumps({"usage": {"prompt_cache_hit_tokens": 50, "prompt_cache_miss_tokens": 20}})
    ) == (50, 20)


def test_call_openai_chat_includes_optional_generation_controls(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    captured: dict[str, object] = {}

    def _fake_urlopen(request: urlrequest.Request, _timeout: int) -> _Response:
        captured.update(json.loads(request.data.decode("utf-8")))  # type: ignore[union-attr]
        return _Response({"model": "mimo-v2.5-pro", "choices": [{"message": {"content": "{\"ok\": true}"}}]})

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=_fake_urlopen,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )

    assert result.ok is True
    assert captured["max_tokens"] == 4096
    assert captured["response_format"] == {"type": "json_object"}


def test_call_openai_chat_http_error_is_redacted(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "tp-supersecretkeyvalue")

    def _fake_urlopen(_request: urlrequest.Request, _timeout: int) -> _Response:
        raise urlerror.HTTPError(
            "https://example.test/v1/chat/completions",
            400,
            "Bad Request",
            hdrs={},
            fp=io.BytesIO(b'{"error":"Bearer tp-supersecretkeyvalue"}'),
        )

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=_fake_urlopen,
    )

    assert result.ok is False
    assert result.kind == "http_4xx"
    assert "tp-supersecretkeyvalue" not in result.detail


def test_call_openai_chat_rejects_placeholder_key_before_request(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "你的key")
    called = False

    def _fake_urlopen(_request: urlrequest.Request, _timeout: int) -> _Response:
        nonlocal called
        called = True
        return _Response({"choices": [{"message": {"content": "{}"}}]})

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=_fake_urlopen,
    )

    assert called is False
    assert result.ok is False
    assert result.kind == "auth_invalid"
    assert "placeholder" in result.detail


def test_call_openai_chat_rejects_non_ascii_key_before_request(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "tp-é")

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=lambda _request, _timeout: _Response({"choices": [{"message": {"content": "{}"}}]}),
    )

    assert result.ok is False
    assert result.kind == "auth_invalid"
    assert "non-ASCII" in result.detail


def test_call_openai_chat_retries_5xx_then_succeeds(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.sleep", lambda _s: None)
    calls: list[int] = []

    def _fake_urlopen(request: urlrequest.Request, timeout: int):
        calls.append(timeout)
        if len(calls) == 1:
            raise urlerror.HTTPError(
                "https://example.test/v1/chat/completions",
                502,
                "Bad Gateway",
                hdrs={},
                fp=io.BytesIO(b"upstream error"),
            )
        return _Response({"model": "mimo-v2.5-pro", "choices": [{"message": {"content": "{\"ok\": true}"}}]})

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=12,
        urlopen_fn=_fake_urlopen,
        max_retries=2,
    )

    assert result.ok is True
    assert len(result.attempts) == 2
    assert result.attempts[0]["kind"] == "http_5xx"
    assert result.attempts[0]["http_status"] == 502
    assert result.attempts[1]["kind"] == "ok"
    assert calls == [12, 12]


def test_call_openai_chat_does_not_retry_4xx(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.sleep", lambda _s: None)
    calls: list[int] = []

    def _fake_urlopen(_request: urlrequest.Request, _timeout: int):
        calls.append(1)
        raise urlerror.HTTPError(
            "https://example.test/v1/chat/completions",
            400,
            "Bad Request",
            hdrs={},
            fp=io.BytesIO(b"invalid request"),
        )

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=_fake_urlopen,
        max_retries=3,
    )

    assert result.ok is False
    assert result.kind == "http_4xx"
    assert len(calls) == 1
    assert len(result.attempts) == 1


def test_call_openai_chat_does_not_retry_streaming_required(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.sleep", lambda _s: None)
    calls: list[int] = []

    def _fake_urlopen(_request: urlrequest.Request, _timeout: int):
        calls.append(1)
        raise urlerror.HTTPError(
            "https://example.test/v1/chat/completions",
            501,
            "Not Implemented",
            hdrs={},
            fp=io.BytesIO(b"stream must be true"),
        )

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=_fake_urlopen,
        max_retries=3,
    )

    assert result.ok is False
    assert result.kind == "streaming_required"
    assert len(calls) == 1


def test_call_openai_chat_records_attempts_on_repeated_timeout(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.sleep", lambda _s: None)
    calls: list[int] = []

    def _fake_urlopen(_request: urlrequest.Request, timeout: int):
        calls.append(timeout)
        raise TimeoutError("timed out")

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=8,
        urlopen_fn=_fake_urlopen,
        max_retries=2,
    )

    assert result.ok is False
    assert result.kind == "http_timeout"
    assert len(result.attempts) == 3
    assert all(entry["kind"] == "http_timeout" for entry in result.attempts)
    assert calls == [8, 8, 8]


def test_call_openai_chat_uses_per_attempt_timeout(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.sleep", lambda _s: None)
    calls: list[int] = []

    def _fake_urlopen(_request: urlrequest.Request, timeout: int):
        calls.append(timeout)
        raise TimeoutError("timed out")

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=15,
        urlopen_fn=_fake_urlopen,
        max_retries=2,
        total_timeout_seconds=600,
    )

    assert result.ok is False
    assert calls == [15, 15, 15]
    assert all(entry["timeout_seconds"] == 15 for entry in result.attempts)


def test_call_openai_chat_respects_total_timeout_budget(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    fake_clock = {"now": 1000.0}

    def _fake_monotonic() -> float:
        return fake_clock["now"]

    def _fake_sleep(_s: float) -> None:
        fake_clock["now"] += _s

    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.sleep", _fake_sleep)
    calls: list[int] = []

    def _fake_urlopen(_request: urlrequest.Request, timeout: int):
        calls.append(timeout)
        fake_clock["now"] += timeout  # simulate full timeout consumption
        raise TimeoutError("timed out")

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=60,
        urlopen_fn=_fake_urlopen,
        max_retries=5,
        total_timeout_seconds=90,
    )

    assert result.ok is False
    assert calls[0] == 60
    # Second attempt should be clamped to remaining budget (~30s) instead of full 60.
    assert calls[1] <= 30
    # No further attempts after total budget exhausted.
    assert sum(calls) <= 90


def test_call_openai_chat_does_not_start_attempt_when_remaining_budget_is_too_small(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    fake_clock = {"now": 1000.0}

    def _fake_monotonic() -> float:
        return fake_clock["now"]

    def _fake_sleep(seconds: float) -> None:
        fake_clock["now"] += seconds

    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.monotonic", _fake_monotonic)
    monkeypatch.setattr("kodawari.autopilot.core.openai_chat_client.time.sleep", _fake_sleep)
    calls: list[int] = []

    def _fake_urlopen(_request: urlrequest.Request, timeout: int):
        calls.append(timeout)
        fake_clock["now"] += timeout
        raise TimeoutError("timed out")

    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=60,
        urlopen_fn=_fake_urlopen,
        max_retries=2,
        total_timeout_seconds=63,
    )

    assert result.ok is False
    assert calls == [60]
    assert len(result.attempts) == 1


def test_call_openai_chat_hard_times_out_when_urlopen_blocks(monkeypatch) -> None:
    monkeypatch.setenv("WORKFLOW_TEST_OPENAI_KEY", "secret-key")
    blocker = threading.Event()

    def _fake_urlopen(_request: urlrequest.Request, _timeout: int):
        blocker.wait(30)
        return _Response({"model": "mimo-v2.5-pro", "choices": [{"message": {"content": "{\"ok\": true}"}}]})

    started = time.monotonic()
    result = call_openai_chat(
        transport=_transport(),
        model="mimo-v2.5-pro",
        system="system",
        user="user",
        timeout_seconds=5,
        urlopen_fn=_fake_urlopen,
        max_retries=0,
        total_timeout_seconds=5,
    )
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.kind == "http_timeout"
    assert elapsed < 8
    assert len(result.attempts) == 1
    assert result.attempts[0]["timeout_seconds"] == 5
    blocker.set()

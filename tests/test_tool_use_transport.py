from __future__ import annotations

import time
from urllib import request as urlrequest

import pytest

from kodawari.autopilot.execution import tool_use_transport
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError


def test_tool_use_transport_watchdog_times_out_stuck_read() -> None:
    request = urlrequest.Request("https://example.test/v1/chat/completions", data=b"{}")

    class _SlowResponse:
        def __enter__(self) -> "_SlowResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            time.sleep(2.0)
            return b"{}"

    class _SlowOpener:
        def open(self, *_args: object, **_kwargs: object) -> _SlowResponse:
            return _SlowResponse()

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        tool_use_transport._read_response_text_with_watchdog(
            request,
            opener=_SlowOpener(),
            timeout_seconds=1,
        )

    assert time.monotonic() - started < 1.8


def test_tool_use_transport_maps_hard_timeout_to_specific_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*_args: object, **_kwargs: object) -> str:
        raise TimeoutError("request exceeded hard timeout (1s)")

    monkeypatch.setattr(tool_use_transport, "_read_response_text_with_watchdog", _timeout)

    with pytest.raises(OpenAIToolUseExecutionError) as info:
        tool_use_transport.post_chat(
            endpoint="https://example.test/v1/chat/completions",
            api_key="test-key",
            payload={"model": "m", "messages": []},
            timeout_seconds=1,
            max_retries=0,
        )

    assert info.value.code == "HTTP_TIMEOUT"

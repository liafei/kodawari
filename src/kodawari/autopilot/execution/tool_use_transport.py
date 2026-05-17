"""HTTP transport helpers for the OpenAI-compatible tool-use executor."""

from __future__ import annotations

import json
import os
import queue
import ssl
import threading
import time
from typing import Any, Callable
from urllib import error as urlerror
from urllib import parse as urlparse
from urllib import request as urlrequest

from kodawari.autopilot.core.http_safety import RedirectBlocked, SafeRedirectHandler
from kodawari.autopilot.core.secret_redactor import redact_secret_text
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError

_LOCALHOSTS = {"localhost", "127.0.0.1", "::1"}
_CONTEXT_OVERFLOW_MARKERS = (
    "context_length",
    "context length",
    "maximum context",
    "context window",
    "too many tokens",
    "token limit",
    "tokens exceeds",
    "exceeded token",
)
_WAF_BLOCK_MARKERS = (
    "miwaf",
    "web application firewall",
    "security threat",
    "请求已被阻断",
    "安全威胁",
)


_ANTHROPIC_API_FORMATS = {"anthropic", "anthropic_messages"}


def _normalize_api_format(api_format: str) -> str:
    return str(api_format or "").strip().lower()


def _is_anthropic_format(api_format: str) -> bool:
    return _normalize_api_format(api_format) in _ANTHROPIC_API_FORMATS


def post_chat(
    *,
    endpoint: str,
    api_key: str,
    payload: dict[str, Any],
    timeout_seconds: int,
    max_retries: int = 0,
    api_format: str = "openai_chat",
) -> dict[str, Any]:
    """POST to an OpenAI- or Anthropic-compatible chat endpoint.

    For ``api_format == "anthropic_messages"`` the function:
    1. Converts the OpenAI-shaped ``payload`` (messages, tools, system) into
       Anthropic's ``/v1/messages`` body format.
    2. Sends with ``x-api-key`` + ``anthropic-version`` headers.
    3. Normalizes the response back into OpenAI's ``choices[].message.tool_calls``
       shape so the executor loop can treat both transports identically.

    For ``api_format == "openai_chat"`` (default) the legacy Bearer-auth
    OpenAI path is used unchanged.
    """
    is_anthropic = _is_anthropic_format(api_format)
    if is_anthropic:
        body_payload = _anthropic_payload_from_openai(payload)
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    else:
        body_payload = payload
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    request = urlrequest.Request(
        endpoint,
        data=json.dumps(body_payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    opener = urlrequest.build_opener(SafeRedirectHandler)
    attempts = max(1, int(max_retries or 0) + 1)
    last_error: OpenAIToolUseExecutionError | None = None
    for attempt in range(1, attempts + 1):
        try:
            raw = _read_response_text_with_watchdog(
                request,
                opener=opener,
                timeout_seconds=max(5, int(timeout_seconds)),
            )
            break
        except RedirectBlocked as exc:
            raise OpenAIToolUseExecutionError("REDIRECT_BLOCKED", str(exc)) from exc
        except urlerror.HTTPError as exc:
            body = safe_http_body(exc)
            if is_context_overflow_http_error(exc.code, body):
                raise OpenAIToolUseExecutionError(
                    "EXECUTOR_STALLED_CONTEXT_OVERFLOW",
                    f"http {exc.code}: {redact_secret_text(body)}",
                ) from exc
            if is_waf_http_error(exc.code, body):
                raise OpenAIToolUseExecutionError(
                    "HTTP_WAF_BLOCKED",
                    f"http {exc.code}: {redact_secret_text(body)}",
                ) from exc
            error = OpenAIToolUseExecutionError("HTTP_ERROR", f"http {exc.code}: {redact_secret_text(body)}")
            if int(getattr(exc, "code", 0) or 0) < 500 or attempt >= attempts:
                raise error from exc
            last_error = error
            time.sleep(min(2.0, 0.25 * attempt))
        except TimeoutError as exc:
            error = OpenAIToolUseExecutionError("HTTP_TIMEOUT", redact_secret_text(str(exc) or "timed out"))
            if attempt >= attempts:
                raise error from exc
            last_error = error
            time.sleep(min(2.0, 0.25 * attempt))
        except (urlerror.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            error = OpenAIToolUseExecutionError("HTTP_ERROR", redact_secret_text(str(exc)))
            if attempt >= attempts:
                raise error from exc
            last_error = error
            time.sleep(min(2.0, 0.25 * attempt))
    else:
        raise last_error or OpenAIToolUseExecutionError("HTTP_ERROR", "endpoint request failed")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OpenAIToolUseExecutionError("NON_JSON_RESPONSE", "endpoint returned non-json response") from exc
    if not isinstance(parsed, dict):
        raise OpenAIToolUseExecutionError("NON_JSON_RESPONSE", "endpoint response is not a JSON object")
    if is_anthropic:
        return _anthropic_response_to_openai(parsed)
    return parsed


def _read_response_text_with_watchdog(
    request: urlrequest.Request,
    *,
    opener: Any,
    timeout_seconds: int,
) -> str:
    outcomes: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    timeout_value = max(1, int(timeout_seconds or 1))

    def _worker() -> None:
        try:
            with opener.open(request, timeout=timeout_value) as response:
                raw = response.read().decode("utf-8", errors="replace")
            outcomes.put(("ok", raw))
        except Exception as exc:
            outcomes.put(("error", exc))

    thread = threading.Thread(target=_worker, name="workflow-tool-use-http-request", daemon=True)
    thread.start()
    try:
        kind, payload = outcomes.get(timeout=timeout_value)
    except queue.Empty as exc:
        raise TimeoutError(f"request exceeded hard timeout ({timeout_value}s)") from exc
    if kind == "error":
        raise payload
    return str(payload or "")


def http_timeout_seconds(config: Any, *, cap_fn: Callable[[Any, str, int], int]) -> int:
    explicit = cap_fn(config, "http_timeout_seconds", 0)
    if explicit > 0:
        return max(5, min(300, explicit))
    return max(5, min(300, cap_fn(config, "max_wall_clock_seconds", 1800)))


def is_context_overflow_http_error(status_code: int, body: str) -> bool:
    if int(status_code or 0) not in {400, 413}:
        return False
    lowered = str(body or "").lower()
    return any(marker in lowered for marker in _CONTEXT_OVERFLOW_MARKERS)


def is_waf_http_error(status_code: int, body: str) -> bool:
    if int(status_code or 0) != 403:
        return False
    lowered = str(body or "").lower()
    return any(marker in lowered for marker in _WAF_BLOCK_MARKERS)


def base_url(config: Any) -> str:
    direct = str(getattr(config, "base_url", "") or "").strip()
    if direct:
        return direct
    env_name = str(getattr(config, "base_url_env", "") or "").strip()
    return os.environ.get(env_name, "") if env_name else ""


def chat_completions_endpoint(base_url: str, *, api_format: str) -> str:
    fmt = _normalize_api_format(api_format)
    if fmt not in {"openai", "openai_chat"} and fmt not in _ANTHROPIC_API_FORMATS:
        raise OpenAIToolUseExecutionError(
            "API_FORMAT_UNSUPPORTED",
            "tool-use transport requires api_format in {openai_chat, anthropic_messages}",
        )
    raw = str(base_url or "").strip()
    parsed = urlparse.urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        raise OpenAIToolUseExecutionError("ENDPOINT_MALFORMED", "base_url is not a valid URL")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and host not in _LOCALHOSTS:
        raise OpenAIToolUseExecutionError("ENDPOINT_NON_HTTPS", "base_url must use https outside localhost")
    path = parsed.path.rstrip("/")
    if fmt in _ANTHROPIC_API_FORMATS:
        # Anthropic native: route to /v1/messages
        if path.endswith("/messages"):
            endpoint_path = path
        elif path.endswith("/v1"):
            endpoint_path = f"{path}/messages"
        elif not path:
            endpoint_path = "/v1/messages"
        else:
            endpoint_path = f"{path}/v1/messages"
        return urlparse.urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", ""))
    if "anthropic" in path.lower():
        raise OpenAIToolUseExecutionError("ENDPOINT_NOT_CHAT_COMPLETIONS", "OpenAI chat transport points at an anthropic endpoint")
    if path.endswith("/chat/completions"):
        endpoint_path = path
    elif path.endswith("/v1"):
        endpoint_path = f"{path}/chat/completions"
    elif not path:
        endpoint_path = "/v1/chat/completions"
    else:
        endpoint_path = f"{path}/v1/chat/completions"
    return urlparse.urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, "", ""))


# ---------------------------------------------------------------------------
# OpenAI ↔ Anthropic payload/response conversion
# ---------------------------------------------------------------------------


def _anthropic_payload_from_openai(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI chat-completions payload into Anthropic /v1/messages.

    Input (OpenAI-style):
        {
          "model": str,
          "max_tokens": int,
          "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "...", "tool_calls": [...]},
            {"role": "tool", "tool_call_id": "...", "content": "..."},
          ],
          "tools": [{"type":"function","function":{"name","description","parameters"}}, ...],
          "tool_choice": "auto" | {...},
        }

    Output (Anthropic-style):
        {
          "model": str,
          "max_tokens": int,
          "system": str | list,
          "messages": [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": [{type:"text"|"tool_use", ...}]},
            {"role": "user", "content": [{type:"tool_result", tool_use_id, content}]},
          ],
          "tools": [{"name","description","input_schema"}, ...],
        }
    """
    out: dict[str, Any] = {
        "model": payload.get("model"),
        "max_tokens": int(payload.get("max_tokens") or 4096),
    }
    # Carry over a few common knobs if present
    for key in ("temperature", "top_p", "stop", "stop_sequences", "metadata"):
        if key in payload and payload[key] is not None:
            out[key] = payload[key]

    system_parts: list[str] = []
    messages_in = list(payload.get("messages") or [])
    messages_out: list[dict[str, Any]] = []
    for msg in messages_in:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                system_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_parts.append(str(block.get("text") or ""))
            continue
        if role == "tool":
            # OpenAI tool result → Anthropic user message with tool_result block
            tool_call_id = str(msg.get("tool_call_id") or "")
            tool_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            messages_out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": tool_content,
                }],
            })
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content.strip():
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        blocks.append(block)
            for tc in list(msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                tc_id = str(tc.get("id") or "")
                name = str(fn.get("name") or "")
                args_raw = fn.get("arguments")
                if isinstance(args_raw, str):
                    try:
                        args_obj = json.loads(args_raw) if args_raw.strip() else {}
                    except json.JSONDecodeError:
                        args_obj = {}
                elif isinstance(args_raw, dict):
                    args_obj = args_raw
                else:
                    args_obj = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc_id,
                    "name": name,
                    "input": args_obj,
                })
            if blocks:
                messages_out.append({"role": "assistant", "content": blocks})
            continue
        # user (or anything else treated as user)
        if isinstance(content, list):
            # Pass through block array (likely tool_result or text blocks)
            messages_out.append({"role": "user", "content": content})
        else:
            text = "" if content is None else str(content)
            messages_out.append({"role": "user", "content": text})

    if system_parts:
        out["system"] = "\n\n".join(part for part in system_parts if part)
    out["messages"] = messages_out

    # Tools: OpenAI {"type":"function","function":{...}} → Anthropic {"name", "description", "input_schema"}
    tools_in = list(payload.get("tools") or [])
    tools_out: list[dict[str, Any]] = []
    for tool in tools_in:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if "function" in tool else tool
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        anthropic_tool = {
            "name": name,
            "description": str(fn.get("description") or ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }
        tools_out.append(anthropic_tool)
    if tools_out:
        out["tools"] = tools_out

    # tool_choice translation (best-effort)
    tc_in = payload.get("tool_choice")
    if isinstance(tc_in, str):
        if tc_in == "auto":
            out["tool_choice"] = {"type": "auto"}
        elif tc_in == "none":
            # Anthropic doesn't have a direct "none"; omit tools to disable
            out.pop("tools", None)
        elif tc_in == "required":
            out["tool_choice"] = {"type": "any"}
    elif isinstance(tc_in, dict):
        fn = tc_in.get("function") or {}
        if fn.get("name"):
            out["tool_choice"] = {"type": "tool", "name": fn.get("name")}

    return out


def _anthropic_response_to_openai(resp: dict[str, Any]) -> dict[str, Any]:
    """Translate an Anthropic /v1/messages response into OpenAI chat shape.

    Anthropic response:
        {
          "id": "...",
          "type": "message",
          "role": "assistant",
          "model": "...",
          "content": [{"type":"text","text":"..."}, {"type":"tool_use","id","name","input"}, ...],
          "stop_reason": "end_turn" | "tool_use" | "max_tokens" | ...,
          "usage": {"input_tokens": int, "output_tokens": int}
        }

    Returned OpenAI-style:
        {
          "id": "...",
          "model": "...",
          "choices": [{
            "index": 0,
            "message": {
              "role": "assistant",
              "content": str | None,
              "tool_calls": [{"id","type":"function","function":{"name","arguments"(json str)}}] (optional)
            },
            "finish_reason": "stop" | "tool_calls" | "length"
          }],
          "usage": {"prompt_tokens","completion_tokens","total_tokens"}
        }
    """
    blocks = list(resp.get("content") or [])
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = str(block.get("type") or "").strip().lower()
        if btype == "text":
            text_val = block.get("text")
            if isinstance(text_val, str) and text_val:
                text_parts.append(text_val)
        elif btype == "tool_use":
            tc_id = str(block.get("id") or "")
            name = str(block.get("name") or "")
            inp = block.get("input")
            if not isinstance(inp, (dict, list)):
                inp = {}
            tool_calls.append({
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(inp, ensure_ascii=False),
                },
            })
        # ignore "thinking" and other block types
    message: dict[str, Any] = {"role": "assistant"}
    message["content"] = "\n".join(text_parts) if text_parts else None
    if tool_calls:
        message["tool_calls"] = tool_calls

    stop_reason = str(resp.get("stop_reason") or "").strip().lower()
    if stop_reason == "tool_use":
        finish_reason = "tool_calls"
    elif stop_reason == "max_tokens":
        finish_reason = "length"
    else:
        finish_reason = "stop"

    usage_in = resp.get("usage") or {}
    prompt_tokens = int(usage_in.get("input_tokens") or 0)
    completion_tokens = int(usage_in.get("output_tokens") or 0)
    cache_creation = int(usage_in.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage_in.get("cache_read_input_tokens") or 0)
    return {
        "id": resp.get("id"),
        "model": resp.get("model"),
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens + cache_creation + cache_read,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + cache_creation + cache_read + completion_tokens,
            "prompt_tokens_details": {
                "cached_tokens": cache_read,
            },
        },
    }


def safe_http_body(exc: urlerror.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""

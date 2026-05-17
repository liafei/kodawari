"""Shared JSON extraction for model/CLI/API responses."""

from __future__ import annotations

import json
import re
from typing import Any


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.IGNORECASE | re.DOTALL)


def strip_transport_noise(value: Any) -> str:
    text = str(value or "")
    text = text.lstrip("\ufeff")
    text = _ANSI_RE.sub("", text)
    return text.strip()


def extract_text_content(value: Any) -> str:
    """Unwrap common model transport envelopes into text.

    This is intentionally conservative: it returns text only. Structured dicts
    that are already JSON payloads are handled by ``extract_json_object``.
    """
    if isinstance(value, dict):
        from_choices = _extract_openai_choice_text(value)
        if from_choices:
            return from_choices
        for key in ("result", "content", "text"):
            item = value.get(key)
            if isinstance(item, str):
                return strip_transport_noise(item)
            if isinstance(item, list):
                joined = "\n".join(
                    str(part.get("text") or "")
                    for part in item
                    if isinstance(part, dict) and str(part.get("type") or "").lower() in {"text", "output_text"}
                ).strip()
                if joined:
                    return strip_transport_noise(joined)
        return ""
    return strip_transport_noise(value)


def extract_json_object(value: Any) -> dict[str, Any] | None:
    text = extract_json_object_text(value)
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return dict(payload) if isinstance(payload, dict) else None


def extract_json_object_text(value: Any) -> str:
    if isinstance(value, dict):
        tool_args = _extract_openai_tool_arguments(value)
        if tool_args:
            parsed = extract_json_object_text(tool_args)
            if parsed:
                return parsed
        content = extract_text_content(value)
        if content:
            parsed = extract_json_object_text(content)
            if parsed:
                return parsed
        if _looks_like_transport_envelope(value):
            return ""
        direct = _as_json_object_text(value)
        if direct:
            return direct
    else:
        content = extract_text_content(value)
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            direct = extract_json_object_text(parsed)
            if direct:
                return direct
    text = strip_transport_noise(content)
    if not text:
        return ""
    direct = _as_json_object_text(text)
    if direct:
        return direct
    for match in _FENCED_JSON_RE.finditer(text):
        candidate = strip_transport_noise(match.group(1))
        parsed = _as_json_object_text(candidate)
        if parsed:
            return parsed
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidate = text[start : end + 1]
        parsed = _as_json_object_text(candidate)
        if parsed:
            return parsed
    return ""


def _as_json_object_text(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = strip_transport_noise(value)
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return json.dumps(payload, ensure_ascii=False)


def _looks_like_transport_envelope(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ("choices", "result", "content", "text"))


def _extract_openai_choice_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message") or first.get("delta")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return strip_transport_noise(content) if isinstance(content, str) else ""


def _extract_openai_tool_arguments(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message") or first.get("delta")
    if not isinstance(message, dict):
        return ""
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return ""
    first_call = calls[0]
    if not isinstance(first_call, dict):
        return ""
    function = first_call.get("function")
    if not isinstance(function, dict):
        return ""
    arguments = function.get("arguments")
    return strip_transport_noise(arguments) if isinstance(arguments, str) else ""


__all__ = [
    "extract_json_object",
    "extract_json_object_text",
    "extract_text_content",
    "strip_transport_noise",
]

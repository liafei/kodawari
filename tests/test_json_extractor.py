from __future__ import annotations

import json

from kodawari.autopilot.core.json_extractor import (
    extract_json_object,
    extract_json_object_text,
    extract_text_content,
    strip_transport_noise,
)


def test_extract_json_object_from_claude_envelope() -> None:
    envelope = {"result": '{"approved": true, "summary": "ok"}'}

    assert extract_json_object(envelope) == {"approved": True, "summary": "ok"}


def test_extract_json_object_from_openai_chat_content() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": "Here:\n```json\n{\"approved\": false, \"summary\": \"blocked\"}\n```"
                }
            }
        ]
    }

    assert extract_json_object(payload) == {"approved": False, "summary": "blocked"}


def test_extract_json_object_from_openai_tool_arguments() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_review",
                                "arguments": json.dumps({"approved": True, "summary": "tool"}),
                            }
                        }
                    ]
                }
            }
        ]
    }

    assert extract_json_object(payload) == {"approved": True, "summary": "tool"}


def test_extract_json_object_strips_ansi_and_bom() -> None:
    raw = "\ufeff\x1b[32m{\"approved\": true}\x1b[0m"

    assert strip_transport_noise(raw) == '{"approved": true}'
    assert extract_json_object(raw) == {"approved": True}


def test_extract_json_object_text_fails_closed_on_truncated_json() -> None:
    assert extract_json_object_text('{"approved": true') == ""


def test_extract_text_content_joins_anthropic_blocks() -> None:
    payload = {"content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]}

    assert extract_text_content(payload) == "hello\nworld"

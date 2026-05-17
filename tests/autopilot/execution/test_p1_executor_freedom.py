"""Tests for the Phase 1 changes that loosen executor constraints.

Phase 1 covers 8 of the 10 changes proposed after the kodawari architecture
audit:

* P1-#2: removed the "Do not rewrite whole existing files" bias sentence from
  the exact_str_replace_v1 system prompt
* P1-#3: ``check_complexity`` tool registered in both protocols, dispatched on
  the runtime, and returns NOT_APPLICABLE for non-Python paths
* P1-#4: ``check_complexity`` counts as observation_progress (so it doesn't
  trip max_no_progress_iterations during self-verification rounds)
* P1-#5: ``max_same_tool_calls_per_path`` default raised 5 → 10
* P1-#6: the most recent read of any file the executor has edited is exempt
  from message-compaction (so the LLM never loses the file body it's rewriting)
* P1-#7: ``max_read_windows_per_path`` default raised 8 → 12
* P1-#8: ``build_recovery_card`` prefixes complexity must_fix items with a
  machine-readable ``VIOLATING_FUNCTION=... | CURRENT_COMPLEXITY=... |
  HARD_TARGET=...`` header so the executor has unambiguous targets
* P1-#10: fix-round preamble uses an escalating header and appends a refactor
  strategy tail when any must_fix carries the VIOLATING_FUNCTION marker
"""

from __future__ import annotations

import json

import pytest

from kodawari.autopilot.execution import tool_use_prompt
from kodawari.autopilot.execution.execution_prompt_common import (
    render_fix_round_preamble,
)
from kodawari.autopilot.execution.tool_use_prompt import (
    COMPACTABLE_TOOL_RESULTS,
    EXACT_STR_REPLACE_PROTOCOL,
    FULL_FILE_PROTOCOL,
    FULL_FILE_TOOL_MANIFEST_V1,
    INTERNAL_TARGET_PATH_KEY,
    INTERNAL_TOOL_NAME_KEY,
    PATCH_TOOL_MANIFEST_V1,
    messages_for_payload,
    system_prompt,
    tool_schemas,
)
from kodawari.autopilot.execution.tool_use_result import (
    tool_observation_made_progress,
)
from kodawari.autopilot.recovery.executor_recovery import (
    _format_complexity_must_fix,
    _format_must_fix_list,
    build_recovery_card,
)


# ---------------------------------------------------------------------------
# P1-#2 — system prompt bias removal
# ---------------------------------------------------------------------------


def test_str_replace_prompt_no_longer_forbids_full_file_rewrites() -> None:
    prompt = system_prompt(EXACT_STR_REPLACE_PROTOCOL, profile_text="")
    assert "Do not rewrite whole existing files" not in prompt, (
        "The bias sentence should be removed so the model can switch to "
        "full-file rewrites when surgical str_replace edits aren't reducing "
        "complexity."
    )


def test_str_replace_prompt_still_explains_str_replace_path() -> None:
    """The protocol-level guidance about PATCH_FAILED and how to use the "
    "tools must remain — we only removed the bias, not the operating
    instructions.
    """
    prompt = system_prompt(EXACT_STR_REPLACE_PROTOCOL, profile_text="")
    assert "PATCH_FAILED" in prompt
    assert "str_replace" in prompt


# ---------------------------------------------------------------------------
# P1-#3 — check_complexity tool registration
# ---------------------------------------------------------------------------


def test_check_complexity_in_both_manifests() -> None:
    assert "check_complexity" in FULL_FILE_TOOL_MANIFEST_V1
    assert "check_complexity" in PATCH_TOOL_MANIFEST_V1


def test_check_complexity_in_both_protocol_schemas() -> None:
    for protocol in (FULL_FILE_PROTOCOL, EXACT_STR_REPLACE_PROTOCOL):
        schemas = tool_schemas(protocol)
        names = [s["function"]["name"] for s in schemas]
        assert "check_complexity" in names, f"missing under {protocol}"


def test_check_complexity_schema_requires_path() -> None:
    schemas = tool_schemas(FULL_FILE_PROTOCOL)
    check = next(s for s in schemas if s["function"]["name"] == "check_complexity")
    assert check["function"]["parameters"]["required"] == ["path"]
    assert "path" in check["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# P1-#4 — check_complexity as observation progress
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """Minimal runtime stub for progress checkers that need no state."""


def test_check_complexity_counts_as_progress_when_returning_violations() -> None:
    result = {"ok": True, "violations": [], "functions": [{"name": "foo", "complexity": 4}]}
    assert tool_observation_made_progress(_FakeRuntime(), "check_complexity", result) is True


def test_check_complexity_not_applicable_does_not_count_as_progress() -> None:
    """Avoid gaming: NOT_APPLICABLE / errors must not silence the stall timer."""
    result = {"ok": False, "status": "TOOL_ERROR", "error_code": "NOT_APPLICABLE"}
    assert tool_observation_made_progress(_FakeRuntime(), "check_complexity", result) is False


# ---------------------------------------------------------------------------
# P1-#6 — edited-file read exempt from compaction
# ---------------------------------------------------------------------------


class _RuntimeWithChangedPaths:
    def __init__(self, changed: set[str]) -> None:
        self.changed_paths = set(changed)
        self.read_cache = None


def _read_tool_message(*, content: str, path: str | None) -> dict:
    msg = {
        "role": "tool",
        "tool_call_id": f"call_{path}",
        "content": content,
        INTERNAL_TOOL_NAME_KEY: "read_file",
    }
    if path is not None:
        msg[INTERNAL_TARGET_PATH_KEY] = path
    return msg


def _cap_stub(_cfg: object, _name: str, default: int) -> int:
    return default


def test_compaction_exempts_most_recent_read_of_edited_file() -> None:
    """An edited file's latest read survives compaction even when other reads
    are dropped."""
    large_other = "x" * 30_000  # bigger than max_full_read_tool_result_bytes default (24k)
    messages = [
        # A historical read of an UNRELATED file — should get compacted.
        _read_tool_message(content=large_other, path="other/big.py"),
        # The read of the file we then edited — should be PINNED (full).
        _read_tool_message(content="def normalize(): pass\n", path="backend/foo.py"),
        # A trailing assistant turn (untouched by compaction logic).
        {"role": "assistant", "content": ""},
    ]
    runtime = _RuntimeWithChangedPaths({"backend/foo.py"})
    out = messages_for_payload(
        messages,
        config=object(),
        cap_fn=_cap_stub,
        runtime=runtime,
    )
    # The edited-file read message stays full.
    edited_read = next(m for m in out if m.get("tool_call_id") == "call_backend/foo.py")
    assert "def normalize(): pass" in edited_read["content"]
    # The unrelated big read is compacted (it's bigger than full_budget).
    other_read = next(m for m in out if m.get("tool_call_id") == "call_other/big.py")
    assert len(other_read["content"]) < len(large_other)


def test_compaction_strips_internal_workflow_keys() -> None:
    """The new INTERNAL_TARGET_PATH_KEY must not leak to provider payloads."""
    messages = [_read_tool_message(content="hello", path="backend/foo.py")]
    runtime = _RuntimeWithChangedPaths({"backend/foo.py"})
    out = messages_for_payload(messages, config=object(), cap_fn=_cap_stub, runtime=runtime)
    public = out[0]
    assert INTERNAL_TARGET_PATH_KEY not in public
    assert INTERNAL_TOOL_NAME_KEY not in public


# ---------------------------------------------------------------------------
# P1-#8 — structured prefix on complexity must_fix items
# ---------------------------------------------------------------------------


def test_complexity_must_fix_prefix_extracts_all_fields() -> None:
    item = (
        "backend/modules/content/foo.py: Function normalize_x_thread_payload "
        "complexity 14 exceeds 10. Remediation: extract 2-3 helpers."
    )
    formatted = _format_complexity_must_fix(item)
    assert formatted.startswith("VIOLATING_FUNCTION=normalize_x_thread_payload")
    assert "FILE=backend/modules/content/foo.py" in formatted
    assert "CURRENT_COMPLEXITY=14" in formatted
    assert "GATE_LIMIT=10" in formatted
    assert "HARD_TARGET=8" in formatted  # limit - 2
    # original guidance preserved after the header
    assert "Remediation" in formatted


def test_non_complexity_must_fix_passes_through_unchanged() -> None:
    plain = "Please update the changelog before submitting"
    assert _format_complexity_must_fix(plain) == plain


def test_format_must_fix_list_preserves_order_and_count() -> None:
    items = [
        "free-form note",
        "foo.py: Function bar complexity 11 exceeds 10",
        "",  # empty filtered
        "another note",
    ]
    out = _format_must_fix_list(items)
    assert len(out) == 3
    assert out[0] == "free-form note"
    assert out[1].startswith("VIOLATING_FUNCTION=bar")
    assert out[2] == "another note"


def test_build_recovery_card_uses_formatted_must_fix() -> None:
    card = build_recovery_card(
        original_card={
            "files_to_change": ["backend/foo.py"],
            "invariants": ["public signature stable"],
            "verify_cmd": "pytest -q",
        },
        decision={"action": "deterministic_recovery", "reason": "gate"},
        task_id="T7",
        must_fix=["backend/foo.py: Function bar complexity 13 exceeds 10"],
    )
    recovery = card["recovery"]
    assert recovery["must_fix"][0].startswith("VIOLATING_FUNCTION=bar")


# ---------------------------------------------------------------------------
# P1-#10 — escalating fix-round preamble + complexity tail
# ---------------------------------------------------------------------------


def test_preamble_header_signals_previous_attempt_failure() -> None:
    payload = {"requested_action": "fix_round", "must_fix": ["please retry"]}
    lines = render_fix_round_preamble(payload)
    head = lines[0]
    assert "A PREVIOUS ATTEMPT WAS BLOCKED" in head
    assert "do not just repeat it" in head.lower()


def test_preamble_emits_complexity_tail_when_violating_function_marker_present() -> None:
    payload = {
        "requested_action": "fix_round",
        "must_fix": [
            "VIOLATING_FUNCTION=normalize | CURRENT_COMPLEXITY=14 | HARD_TARGET=8\n"
            "backend/foo.py: ...",
        ],
    }
    lines = render_fix_round_preamble(payload)
    body = "\n".join(lines)
    assert "Refactor strategy for complexity violations" in body
    assert "PREFER rewriting the whole function body" in body
    assert "check_complexity" in body


def test_preamble_no_complexity_tail_for_plain_must_fix() -> None:
    payload = {"requested_action": "fix_round", "must_fix": ["please update changelog"]}
    body = "\n".join(render_fix_round_preamble(payload))
    assert "Refactor strategy" not in body

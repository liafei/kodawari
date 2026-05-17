"""Tests for plan_reviewer.py — response parsing."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any

import pytest

from kodawari.autopilot.planning.plan_reviewer import _build_command, _parse_response


class TestParseReviewerResponse:
    """_parse_response extracts JSON from Codex CLI output."""

    def test_direct_json(self) -> None:
        payload = {"score": 9.0, "approved": True, "findings": [], "contradictions": []}
        result, error = _parse_response(json.dumps(payload))
        assert error == ""
        assert result is not None
        assert result["score"] == 9.0
        assert result["approved"] is True

    def test_json_in_code_block(self) -> None:
        raw = '```json\n{"score": 8.5, "approved": false, "findings": [{"severity": "blocking"}]}\n```'
        result, error = _parse_response(raw)
        assert error == ""
        assert result is not None
        assert result["score"] == 8.5

    def test_json_in_plain_text(self) -> None:
        raw = 'Here is my review:\n\n{"score": 9.1, "approved": true, "findings": []}\n\nDone.'
        result, error = _parse_response(raw)
        assert error == ""
        assert result is not None
        assert result["score"] == 9.1

    def test_no_json_returns_none(self) -> None:
        result, error = _parse_response("This is plain text with no JSON.")
        assert result is None
        assert error != ""

    def test_empty_string(self) -> None:
        result, error = _parse_response("")
        assert result is None
        assert error != ""

    def test_malformed_json(self) -> None:
        result, error = _parse_response('{"score": 9.0, broken}')
        assert result is None
        assert error != ""


def test_build_command_includes_model_when_configured() -> None:
    command = _build_command(executable="codex", model="gpt-5.3-codex")
    assert command[-2:] == ["--model", "gpt-5.3-codex"]


def test_build_command_supports_claude_driver() -> None:
    command = _build_command(executable="claude", model="claude-opus-4-7", driver="claude_cli")
    assert command[:5] == ["claude", "-p", "--output-format", "json", "--max-turns"]
    assert command[-2:] == ["--model", "claude-opus-4-7"]


def test_build_command_wraps_windows_cmd_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kodawari.autopilot.core.subprocess_compat.os.name", "nt")
    command = _build_command(executable=r"C:\npm\codex.cmd", model="", driver="codex_cli")
    assert command[:3] == ["cmd.exe", "/c", r"C:\npm\codex.cmd"]
    assert command[3:7] == ["exec", "--skip-git-repo-check", "--sandbox", "read-only"]


def test_build_command_does_not_wrap_windows_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kodawari.autopilot.core.subprocess_compat.os.name", "nt")
    command = _build_command(executable=r"C:\bin\codex.exe", model="", driver="codex_cli")
    assert command[:5] == [r"C:\bin\codex.exe", "exec", "--skip-git-repo-check", "--sandbox", "read-only"]


def test_parse_response_unwraps_claude_json_envelope() -> None:
    payload = {"score": 9.0, "approved": True, "findings": [], "contradictions": []}
    envelope = {"result": json.dumps(payload)}
    result, error = _parse_response(json.dumps(envelope))
    assert error == ""
    assert result is not None
    assert result["approved"] is True


def test_review_plan_runs_cli_in_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kodawari.autopilot.planning import plan_reviewer

    seen: dict[str, Any] = {}
    payload = {"score": 10.0, "approved": True, "findings": [], "contradictions": [], "assessment": "ok"}

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["command"] = list(command)
        seen["cwd"] = kwargs.get("cwd")
        seen["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(plan_reviewer.subprocess, "run", fake_run)

    result, error = plan_reviewer.review_plan(
        executable="codex",
        plan_payload={"tasks": []},
        task_direction="review target project plan",
        context_text="ctx",
        driver="codex_cli",
        project_root=tmp_path,
    )

    assert error == ""
    assert result == payload
    assert seen["cwd"] == str(tmp_path.resolve())
    assert "--cd" in seen["command"]
    assert str(tmp_path.resolve()) in seen["command"]
    assert f"ACTIVE WORKSPACE ROOT: {tmp_path.resolve()}" in str(seen["input"])


def _http_transport(*, interface: str) -> Any:
    from kodawari.autopilot.core.model_config import WorkflowTransportConfig

    return WorkflowTransportConfig(
        name="reviewer_http",
        kind="http",
        driver="openai_compatible",
        interface=interface,
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="EXAMPLE_API_KEY",
    )


def _stub_chat_result(payload: dict[str, Any]) -> Any:
    from kodawari.autopilot.core.openai_chat_client import ChatCallResult

    return ChatCallResult(ok=True, raw_text=json.dumps(payload))


@pytest.mark.parametrize("interface", ["chat", "tool_use"])
def test_review_plan_http_transport_uses_openai_chat(
    monkeypatch: pytest.MonkeyPatch,
    interface: str,
) -> None:
    """plan_reviewer accepts both HTTP chat and tool_use interfaces (same endpoint)."""
    from kodawari.autopilot.planning import plan_reviewer

    payload = {"score": 9.0, "approved": True, "findings": [], "contradictions": [], "assessment": "ok"}
    seen: dict[str, Any] = {}

    def fake_call(**kwargs: Any) -> Any:
        seen.update(kwargs)
        return _stub_chat_result(payload)

    monkeypatch.setattr(plan_reviewer, "call_openai_chat", fake_call)

    result, error = plan_reviewer.review_plan(
        executable="",
        plan_payload={"tasks": []},
        task_direction="t",
        context_text="ctx",
        model="some-model",
        transport=_http_transport(interface=interface),
    )

    assert error == ""
    assert result == payload
    assert seen["transport"].interface == interface
    assert seen["response_format"] == {"type": "json_object"}


def test_review_plan_rejects_unsupported_transport_with_clear_error() -> None:
    """Unsupported transport surfaces kind/interface/driver, not just driver."""
    from kodawari.autopilot.core.model_config import WorkflowTransportConfig
    from kodawari.autopilot.planning import plan_reviewer

    transport = WorkflowTransportConfig(
        name="bogus",
        kind="grpc",
        driver="openai_compatible",
        interface="streaming",
    )

    result, error = plan_reviewer.review_plan(
        executable="",
        plan_payload={"tasks": []},
        task_direction="t",
        context_text="ctx",
        transport=transport,
    )

    assert result is None
    assert "kind='grpc'" in error
    assert "interface='streaming'" in error
    assert "driver='openai_compatible'" in error


# ---------------------------------------------------------------------------
# A1 stateful reviewer: resolved_findings carry across rounds
# ---------------------------------------------------------------------------


class TestStatefulReviewerResolvedFindings:
    """Verify _build_prompt embeds resolved_findings and review_plan threads them."""

    def test_build_prompt_omits_section_when_empty(self) -> None:
        from kodawari.autopilot.planning.plan_reviewer import _build_prompt

        prompt = _build_prompt(
            plan_payload={"tasks": []},
            task_direction="t",
            context_text="ctx",
            structural_issues=[],
            round_number=1,
            resolved_findings=None,
        )
        assert "Previously resolved findings" not in prompt

    def test_build_prompt_embeds_section_when_present(self) -> None:
        from kodawari.autopilot.planning.plan_reviewer import _build_prompt

        resolved = [
            {
                "severity": "blocking",
                "category": "scope",
                "description": "Plan omits Yahoo RSS endpoint coverage",
                "recommendation": "add task that wires the route",
            }
        ]
        prompt = _build_prompt(
            plan_payload={"tasks": []},
            task_direction="t",
            context_text="ctx",
            structural_issues=[],
            round_number=2,
            resolved_findings=resolved,
        )
        assert "Previously resolved findings" in prompt
        assert "DO NOT re-flag" in prompt
        assert "Yahoo RSS endpoint coverage" in prompt
        assert "blocking" in prompt

    def test_build_prompt_compact_form_drops_recommendation_field(self) -> None:
        """The reviewer doesn't need the full original recommendation text — keep
        prompt small. severity/category/description is enough for re-flag detection."""
        from kodawari.autopilot.planning.plan_reviewer import _build_prompt

        resolved = [
            {
                "severity": "high",
                "category": "tests",
                "description": "Missing negative-path test for malformed RSS",
                "recommendation": "VERY LONG RECOMMENDATION TEXT" * 50,
            }
        ]
        prompt = _build_prompt(
            plan_payload={"tasks": []},
            task_direction="t",
            context_text="ctx",
            structural_issues=[],
            round_number=2,
            resolved_findings=resolved,
        )
        assert "Missing negative-path test for malformed RSS" in prompt
        assert "VERY LONG RECOMMENDATION TEXT" not in prompt

    def test_build_prompt_tolerates_non_dict_items(self) -> None:
        from kodawari.autopilot.planning.plan_reviewer import _build_prompt

        prompt = _build_prompt(
            plan_payload={"tasks": []},
            task_direction="t",
            context_text="ctx",
            structural_issues=[],
            round_number=2,
            resolved_findings=[None, "bad-string", {"severity": "low", "category": "x", "description": "y"}],
        )
        # Bad items dropped; the good one survives.
        assert "Previously resolved findings" in prompt
        assert "y" in prompt

    def test_review_plan_http_passes_resolved_findings_into_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kodawari.autopilot.planning import plan_reviewer

        payload = {"score": 9.0, "approved": True, "findings": [], "contradictions": [], "assessment": "ok"}
        seen: dict[str, Any] = {}

        def fake_call(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return _stub_chat_result(payload)

        monkeypatch.setattr(plan_reviewer, "call_openai_chat", fake_call)

        plan_reviewer.review_plan(
            executable="",
            plan_payload={"tasks": []},
            task_direction="t",
            context_text="ctx",
            transport=_http_transport(interface="chat"),
            resolved_findings=[
                {
                    "severity": "blocking",
                    "category": "scope",
                    "description": "MUST PROPAGATE TO PROMPT",
                }
            ],
        )
        assert "MUST PROPAGATE TO PROMPT" in seen["user"]
        assert "Previously resolved findings" in seen["user"]

    def test_review_plan_http_without_resolved_findings_omits_section(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (None) behavior: no section in the prompt — preserves prior contract."""
        from kodawari.autopilot.planning import plan_reviewer

        payload = {"score": 9.0, "approved": True, "findings": [], "contradictions": [], "assessment": "ok"}
        seen: dict[str, Any] = {}

        def fake_call(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return _stub_chat_result(payload)

        monkeypatch.setattr(plan_reviewer, "call_openai_chat", fake_call)

        plan_reviewer.review_plan(
            executable="",
            plan_payload={"tasks": []},
            task_direction="t",
            context_text="ctx",
            transport=_http_transport(interface="chat"),
        )
        assert "Previously resolved findings" not in seen["user"]


def test_reviewer_prompt_carries_validator_boundary_and_approval_semantics() -> None:
    """G + L5: reviewer prompt must surface (a) the validator boundary
    rule that forbids re-emitting structural issues as findings, and (b)
    the approval-semantics rule that ``approved=true`` with empty findings
    is a legitimate answer. These are the upstream half of Phase B/C —
    they reduce reviewer over-rejection at the source so the downstream
    streak/late-round demoters fire only on residual model variance."""
    from kodawari.autopilot.planning.plan_reviewer import _build_prompt

    prompt = _build_prompt(
        plan_payload={"summary": "x", "tasks": []},
        task_direction="t",
        context_text="c",
        structural_issues=[],
        round_number=1,
    )
    assert "Validator boundary" in prompt
    assert "evidence_resolutions" in prompt
    assert "address the reviewer's claim" in prompt
    assert "severity=info at most, never blocking" in prompt
    assert "Approval semantics" in prompt
    assert "approved=true" in prompt
    assert "Do NOT invent findings" in prompt

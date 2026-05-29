"""Tests for planning_agent.py — plan validation and acyclic checks."""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

from kodawari.autopilot.planning.planning_agent import (
    _check_acyclic,
    _build_prompt,
    _compact_previous_plan,
    _execute_planner_tool,
    generate_plan,
    _validate_plan,
)
from kodawari.autopilot.core.repo_path_guard import DEFAULT_MAX_READ_BYTES
from kodawari.autopilot.core.model_config import WorkflowTransportConfig
from kodawari.autopilot.core.openai_chat_client import ChatCallResult
from kodawari.autopilot.execution.tool_use_types import OpenAIToolUseExecutionError
from kodawari.autopilot.planning.plan_reviewer import review_plan


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def _task(
    task_id: str = "T1",
    files: list[str] | None = None,
    new_files: list[str] | None = None,
    invariants: list[str] | None = None,
    depends_on: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "task_name": f"Task {task_id}",
        "layer_owner": "service",
        "surface": "rest_api",
        "files_to_change": files or ["backend/main.py"],
        "new_files": new_files or [],
        "invariants": invariants or ["no regression"],
        "test_plan": "pytest tests/ -q",
        "verify_cmd": "pytest tests/ -q",
        "depends_on": depends_on or [],
        "provides": [],
        "requires": [],
        "api_contracts": [],
        **overrides,
    }


class TestCheckAcyclic:

    def test_no_deps_is_acyclic(self) -> None:
        tasks = [_task("T1"), _task("T2")]
        assert _check_acyclic(tasks) is True

    def test_linear_chain_is_acyclic(self) -> None:
        tasks = [_task("T1"), _task("T2", depends_on=["T1"]), _task("T3", depends_on=["T2"])]
        assert _check_acyclic(tasks) is True

    def test_direct_cycle_detected(self) -> None:
        tasks = [_task("T1", depends_on=["T2"]), _task("T2", depends_on=["T1"])]
        assert _check_acyclic(tasks) is False

    def test_indirect_cycle_detected(self) -> None:
        tasks = [
            _task("T1", depends_on=["T3"]),
            _task("T2", depends_on=["T1"]),
            _task("T3", depends_on=["T2"]),
        ]
        assert _check_acyclic(tasks) is False

    def test_self_loop_detected(self) -> None:
        tasks = [_task("T1", depends_on=["T1"])]
        assert _check_acyclic(tasks) is False

    def test_empty_tasks_is_acyclic(self) -> None:
        assert _check_acyclic([]) is True


class TestValidatePlan:

    def test_valid_plan_no_errors(self, tmp_path: Path) -> None:
        _write(tmp_path / "backend" / "main.py", "app = 1")
        plan = {"tasks": [_task(files=["backend/main.py"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert errors == []

    def test_verification_only_plan_allows_empty_files_with_verify_cmd(self, tmp_path: Path) -> None:
        _write(tmp_path / "tests" / "test_existing.py", "def test_ok():\n    assert True\n")
        task = _task(
            test_plan="python -m pytest tests/test_existing.py -q",
            verify_cmd="python -m pytest tests/test_existing.py -q",
            execution_constraints={
                "verification_only_noop": True,
                "executor_must_not_edit": True,
            },
            related_existing_tests=["tests/test_existing.py"],
            read_only_files=["tests/test_existing.py"],
        )
        task["files_to_change"] = []
        task["new_files"] = []
        plan = {
            "verify_recipes": [
                {
                    "surface": "test",
                    "command": "python -m pytest tests/test_existing.py -q",
                    "required": True,
                    "roots": ["tests/test_existing.py"],
                }
            ],
            "tasks": [task],
        }

        assert _validate_plan(plan, project_root=tmp_path) == []

    def test_empty_tasks_error(self, tmp_path: Path) -> None:
        errors = _validate_plan({"tasks": []}, project_root=tmp_path)
        assert any("non-empty" in e for e in errors)

    def test_missing_task_id(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "x")
        plan = {"tasks": [_task(task_id="", files=["a.py"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("task_id" in e for e in errors)

    def test_missing_layer_owner(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "x")
        plan = {"tasks": [_task(files=["a.py"], layer_owner="")]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("layer_owner" in e for e in errors)

    def test_missing_surface(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "x")
        plan = {"tasks": [_task(files=["a.py"], surface="")]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("surface" in e for e in errors)

    def test_missing_test_plan(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "x")
        plan = {"tasks": [_task(files=["a.py"], test_plan="")]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("test_plan" in e for e in errors)

    def test_planner_tool_reads_and_searches_large_frontend_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("WORKFLOW_PLANNER_TOOL_MAX_READ_BYTES", raising=False)
        body = "x" * (DEFAULT_MAX_READ_BYTES + 1024)
        _write(
            tmp_path / "mobile" / "www" / "index.html",
            f"{body}\n<button onclick=\"openExternalTrends('google')\">Google</button>\n",
        )

        read_result = _execute_planner_tool(
            project_root=tmp_path,
            name="read_file",
            arguments={"path": "mobile/www/index.html", "limit": 80},
        )
        assert read_result["ok"] is True
        assert read_result["path"] == "mobile/www/index.html"
        assert read_result["truncated"] is True
        assert len(read_result["content"]) == 80

        search_result = _execute_planner_tool(
            project_root=tmp_path,
            name="search_file",
            arguments={"path": "mobile/www/index.html", "query": "openExternalTrends", "max_matches": 5},
        )
        assert search_result["ok"] is True
        assert search_result["matches"] == [
            {"line": 2, "excerpt": "<button onclick=\"openExternalTrends('google')\">Google</button>"}
        ]

    def test_files_to_change_exceeds_3(self, tmp_path: Path) -> None:
        for i in range(4):
            _write(tmp_path / f"f{i}.py", "x")
        plan = {"tasks": [_task(files=["f0.py", "f1.py", "f2.py", "f3.py"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("exceeds 3" in e for e in errors)

    def test_os_invalid_path_reports_structural_issue(self, tmp_path: Path) -> None:
        plan = {"tasks": [_task(files=["backend/bad\0path.py"])]}

        errors = _validate_plan(plan, project_root=tmp_path)

        assert any("invalid or out-of-root paths" in e for e in errors)

    def test_invariants_exceeds_5(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "x")
        plan = {"tasks": [_task(files=["a.py"], invariants=["i"] * 6)]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("exceeds 5" in e for e in errors)

    def test_new_files_not_subset(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "x")
        plan = {"tasks": [_task(files=["a.py"], new_files=["b.py"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("subset" in e for e in errors)

    def test_existing_file_missing(self, tmp_path: Path) -> None:
        plan = {"tasks": [_task(files=["does_not_exist.py"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("missing" in e for e in errors)

    def test_new_file_not_required_to_exist(self, tmp_path: Path) -> None:
        plan = {"tasks": [_task(files=["new_file.py"], new_files=["new_file.py"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert errors == []

    def test_downstream_task_can_reference_upstream_new_file(self, tmp_path: Path) -> None:
        plan = {
            "tasks": [
                _task("T1", files=["generated.py"], new_files=["generated.py"]),
                _task("T2", files=["generated.py"], depends_on=["T1"]),
            ]
        }
        errors = _validate_plan(plan, project_root=tmp_path)
        assert errors == []

    def test_downstream_task_can_reference_upstream_new_file_case_insensitively(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import kodawari.autopilot.planning.planning_agent as planning_agent

        monkeypatch.setattr(
            planning_agent,
            "path_comparison_is_case_insensitive",
            lambda _root: True,
        )
        plan = {
            "tasks": [
                _task("T1", files=["Generated.py"], new_files=["Generated.py"]),
                _task("T2", files=["generated.py"], depends_on=["T1"]),
            ]
        }
        errors = planning_agent._validate_plan(plan, project_root=tmp_path)
        assert errors == []

    def test_task_cannot_reference_new_file_from_later_dependency(self, tmp_path: Path) -> None:
        plan = {
            "tasks": [
                _task("T1", files=["generated.py"]),
                _task("T2", files=["generated.py"], new_files=["generated.py"], depends_on=["T1"]),
            ]
        }
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("generated.py" in error and "missing" in error for error in errors)

    def test_cycle_detected(self, tmp_path: Path) -> None:
        _write(tmp_path / "a.py", "x")
        _write(tmp_path / "b.py", "x")
        plan = {"tasks": [
            _task("T1", files=["a.py"], depends_on=["T2"]),
            _task("T2", files=["b.py"], depends_on=["T1"]),
        ]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("cycle" in e for e in errors)

    def test_rejects_out_of_root_paths(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "outside.py"
        _write(outside, "x")
        plan = {"tasks": [_task(files=["../outside.py"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("out-of-root" in e for e in errors)

    def test_rejects_permission_blocked_paths(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env", "SECRET=x")
        plan = {"tasks": [_task(files=[".env"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("permission-blocked" in e for e in errors)

    def test_rejects_secret_file_extensions(self, tmp_path: Path) -> None:
        _write(tmp_path / "certs" / "server.pem", "x")
        plan = {"tasks": [_task(files=["certs/server.pem"])]}
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("permission-blocked" in e for e in errors)


def test_prompt_injects_planning_mode_greenfield() -> None:
    """A1: greenfield planning_mode emits the greenfield hint section so the
    planner is not blind to filesystem-relaxation that task_graph already grants
    at task_graph.py:_task_executability."""
    prompt = _build_prompt(
        task_direction="bootstrap a new CLI tool",
        context_text="## PRD Excerpt\nbuild a todo CLI",
        previous_findings=[],
        previous_plan=None,
        round_number=1,
        planning_mode="greenfield",
    )

    assert "PLANNING MODE: greenfield" in prompt
    assert "may not yet exist on" in prompt
    assert "PLANNING MODE: existing" not in prompt


def test_prompt_injects_planning_mode_existing_by_default() -> None:
    """A1: when planning_mode is omitted (back-compat) the existing hint is used."""
    prompt = _build_prompt(
        task_direction="next unfinished task",
        context_text="context",
        previous_findings=[],
        previous_plan=None,
        round_number=1,
    )

    assert "PLANNING MODE: existing" in prompt
    assert "PLANNING MODE: greenfield" not in prompt


def test_prompt_normalizes_planning_mode_casing_and_whitespace() -> None:
    """A1: planning_mode is case-insensitive and trims whitespace."""
    prompt_upper = _build_prompt(
        task_direction="x",
        context_text="x",
        previous_findings=[],
        previous_plan=None,
        round_number=1,
        planning_mode="  GREENFIELD  ",
    )
    assert "PLANNING MODE: greenfield" in prompt_upper


def test_prompt_treats_completed_prd_items_as_out_of_scope() -> None:
    prompt = _build_prompt(
        task_direction="pick next unfinished task",
        context_text="## PRD Excerpt\n| 1 | clustering | ✅ 已完成 |\n| 2 | social page | P1 |",
        previous_findings=[],
        previous_plan=None,
        round_number=1,
    )

    assert "Completion/status markers" in prompt
    assert "已完成" in prompt
    assert "Prefer pending/P1/P2/unchecked items" in prompt


def test_revision_prompt_uses_compact_previous_plan() -> None:
    previous = {
        "summary": "old",
        "tasks": [
            _task(
                "T1",
                approach="x" * 5000,
                test_plan="y" * 5000,
                api_contracts=[{"method": "GET", "endpoint": "/x", "response_shape": {"ok": "bool"}}],
            )
        ],
    }
    compact = _compact_previous_plan(previous)
    prompt = _build_prompt(
        task_direction="revise",
        context_text="context",
        previous_findings=[{"severity": "blocking", "description": "fix contract"}],
        previous_plan=previous,
        round_number=2,
    )

    assert "api_contracts" in compact["tasks"][0]
    assert "x" * 1000 not in prompt
    assert "exactly ONE api_contracts entry" in prompt


def test_compact_previous_plan_preserves_structural_fields_and_truncates_narrative() -> None:
    """Cross-round revision needs approach/test_plan/verify_cmd/depends_on/layer_owner
    preserved (truncated) so the planner doesn't re-invent the plan each round."""
    previous = {
        "summary": "old",
        "tasks": [
            _task(
                "T1",
                approach="step 1 then step 2 " * 100,  # long narrative
                test_plan="run all unit tests " * 50,
                coverage_hints=[f"hint-{i}" for i in range(20)],
                execution_constraints={"verification_only_noop": False},
            )
        ],
    }
    compact = _compact_previous_plan(previous)
    t = compact["tasks"][0]
    # Short structural fields preserved verbatim (factory sets layer_owner="service",
    # verify_cmd="pytest tests/ -q", depends_on=[]).
    assert t["verify_cmd"] == "pytest tests/ -q"
    assert t["depends_on"] == []
    assert t["layer_owner"] == "service"
    assert t["execution_constraints"] == {"verification_only_noop": False}
    # Long narrative fields preserved but truncated (≤ 800 chars + ellipsis).
    assert "approach" in t
    assert len(t["approach"]) <= 801  # 800 + the "…" character
    assert t["approach"].startswith("step 1 then step 2")
    assert "test_plan" in t
    assert len(t["test_plan"]) <= 801
    # coverage_hints capped at 6 entries.
    assert len(t["coverage_hints"]) == 6
    assert t["coverage_hints"][0] == "hint-0"


def test_compact_previous_plan_drops_long_field_when_absent() -> None:
    """Tasks that don't carry approach / test_plan don't get fake placeholders."""
    previous = {
        "tasks": [
            {
                "task_id": "T1",
                "task_name": "Task T1",
                "files_to_change": ["a.py"],
                # no approach / test_plan / coverage_hints
            }
        ],
    }
    compact = _compact_previous_plan(previous)
    t = compact["tasks"][0]
    assert "approach" not in t
    assert "test_plan" not in t
    assert "coverage_hints" not in t


# ---------------------------------------------------------------------------
# Change 5: parallel task file conflict detection
# ---------------------------------------------------------------------------


class TestParallelFileConflicts:
    def test_no_conflict_when_files_are_disjoint(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _parallel_file_conflicts
        tasks = [
            _task("T1", files=["a.py"]),
            _task("T2", files=["b.py"]),
        ]
        assert _parallel_file_conflicts(tasks) == []

    def test_no_conflict_when_tasks_are_sequential(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _parallel_file_conflicts
        tasks = [
            _task("T1", files=["shared.py"]),
            _task("T2", files=["shared.py"], depends_on=["T1"]),
        ]
        assert _parallel_file_conflicts(tasks) == []

    def test_conflict_detected_for_parallel_tasks(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _parallel_file_conflicts
        tasks = [
            _task("T1", files=["shared.py"]),
            _task("T2", files=["shared.py"]),
        ]
        errors = _parallel_file_conflicts(tasks)
        assert len(errors) == 1
        assert "T1" in errors[0] and "T2" in errors[0]
        assert "shared.py" in errors[0]

    def test_no_conflict_via_transitive_dependency(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _parallel_file_conflicts
        # T3 depends on T2, T2 depends on T1 — all sequential
        tasks = [
            _task("T1", files=["shared.py"]),
            _task("T2", files=["shared.py"], depends_on=["T1"]),
            _task("T3", files=["shared.py"], depends_on=["T2"]),
        ]
        assert _parallel_file_conflicts(tasks) == []

    def test_conflict_only_in_parallel_branch(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _parallel_file_conflicts
        # T1→T2 and T1→T3, but T2 and T3 are parallel
        tasks = [
            _task("T1", files=["base.py"]),
            _task("T2", files=["feature.py"], depends_on=["T1"]),
            _task("T3", files=["feature.py"], depends_on=["T1"]),
        ]
        errors = _parallel_file_conflicts(tasks)
        assert len(errors) == 1
        assert "feature.py" in errors[0]

    def test_conflict_detected_when_parallel_paths_differ_only_by_case(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _parallel_file_conflicts

        tasks = [
            _task("T1", files=["Shared.py"]),
            _task("T2", files=["shared.py"]),
        ]
        errors = _parallel_file_conflicts(tasks, case_insensitive=True)
        assert len(errors) == 1
        assert "T1" in errors[0] and "T2" in errors[0]

    def test_validate_plan_includes_conflict_errors(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "shared.py").write_text("x", encoding="utf-8")
        plan = {
            "tasks": [
                _task("T1", files=["a.py", "shared.py"]),
                _task("T2", files=["shared.py"]),
            ]
        }
        errors = _validate_plan(plan, project_root=tmp_path)
        assert any("parallel" in e for e in errors)
        assert any("shared.py" in e for e in errors)

    def test_validate_plan_includes_conflict_errors_for_case_only_path_variants(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import kodawari.autopilot.planning.planning_agent as planning_agent

        monkeypatch.setattr(
            planning_agent,
            "path_comparison_is_case_insensitive",
            lambda _root: True,
        )
        plan = {
            "tasks": [
                _task("T1", files=["Shared.py"], new_files=["Shared.py"]),
                _task("T2", files=["shared.py"], new_files=["shared.py"]),
            ]
        }
        errors = planning_agent._validate_plan(plan, project_root=tmp_path)
        assert any("parallel" in e for e in errors)


def test_generate_plan_uses_http_chat_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    transport = WorkflowTransportConfig(
        name="mimo_chat",
        kind="http",
        driver="openai_compatible",
        interface="chat",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.chat"],
    )
    plan = {
        "summary": "http planner",
        "tasks": [_task(files=["README.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }
    diagnostics: dict[str, Any] = {}

    captured_kwargs: dict[str, Any] = {}

    def _fake_call(**kwargs: Any) -> ChatCallResult:
        assert kwargs["transport"] is transport
        assert kwargs["model"] == "mimo-v2.5-pro"
        captured_kwargs.update(kwargs)
        return ChatCallResult(
            ok=True,
            raw_text=json.dumps({"choices": [{"message": {"content": json.dumps(plan)}}]}),
            kind="ok",
            request_bytes=123,
            response_bytes=456,
            wallclock_ms=7,
            endpoint="https://example.test/v1/chat/completions",
            attempts=(
                {
                    "idx": 1,
                    "kind": "ok",
                    "detail": "",
                    "http_status": 0,
                    "request_bytes": 123,
                    "response_bytes": 456,
                    "wallclock_ms": 7,
                    "timeout_seconds": 60,
                },
            ),
        )

    monkeypatch.setattr(planning_agent, "call_openai_chat", _fake_call)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with mimo",
        context_text="Project files:\n- README.md",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
    )

    assert error == ""
    assert payload is not None
    assert payload["summary"] == "http planner"
    assert diagnostics["request_bytes"] == 123
    assert isinstance(diagnostics.get("attempts"), list) and diagnostics["attempts"][0]["idx"] == 1
    assert captured_kwargs["max_retries"] == 2
    assert captured_kwargs["max_tokens"] == 8192
    assert captured_kwargs["response_format"] == {"type": "json_object"}
    assert captured_kwargs["total_timeout_seconds"] >= captured_kwargs["timeout_seconds"]


def test_generate_plan_chat_classifies_finish_reason_length_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DeepSeek-v4-pro style reasoning model truncates mid-JSON with finish_reason=length;
    diagnostics must classify as planner_output_truncated_empty, not generic invalid_json."""
    import kodawari.autopilot.planning.planning_agent as planning_agent

    transport = WorkflowTransportConfig(
        name="deepseek_chat",
        kind="http",
        driver="openai_compatible",
        interface="chat",
        api_format="openai_chat",
        base_url="https://api.deepseek.com/v1",
        api_key_env="WORKFLOW_DEEPSEEK_KEY",
        provides=["interface.chat"],
    )
    # Truncated mid-string: model ran out of output budget while writing a task field.
    truncated_content = '{"summary": "x", "tasks": [{"id":"t1","title":"trunc'
    raw_body = json.dumps(
        {
            "choices": [
                {
                    "message": {"content": truncated_content},
                    "finish_reason": "length",
                }
            ],
            "usage": {
                "completion_tokens": 8192,
                "reasoning_tokens": 5892,
            },
        }
    )

    def _fake_call(**_kwargs: Any) -> ChatCallResult:
        return ChatCallResult(
            ok=True,
            raw_text=raw_body,
            kind="ok",
            request_bytes=2000,
            response_bytes=len(raw_body),
            wallclock_ms=99000,
            endpoint="https://api.deepseek.com/v1/chat/completions",
        )

    monkeypatch.setattr(planning_agent, "call_openai_chat", _fake_call)

    diagnostics: dict[str, Any] = {}
    payload, error = generate_plan(
        executable="",
        task_direction="big task",
        context_text="lots of files",
        model="deepseek-v4-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
    )

    assert payload is None
    # diagnostics surfaces finish_reason from raw_text
    assert diagnostics["finish_reason"] == "length"
    # Classified as truncation, not generic invalid_json
    assert diagnostics["planner_error_kind"] == "planner_output_truncated_empty"
    assert diagnostics["transport_kind"] == "http_chat_fallback"
    # Error message is human-readable and points to the real root cause
    assert "finish_reason=length" in error
    assert "reasoning models" in error


def test_chat_diagnostics_omits_finish_reason_when_response_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Backwards compat: response without finish_reason -> diagnostics has no key (no noise)."""
    import kodawari.autopilot.planning.planning_agent as planning_agent

    transport = WorkflowTransportConfig(
        name="mimo_chat",
        kind="http",
        driver="openai_compatible",
        interface="chat",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.chat"],
    )
    plan = {
        "summary": "ok",
        "tasks": [_task(files=["README.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }
    # No finish_reason in body — like the existing test's mock.
    raw = json.dumps({"choices": [{"message": {"content": json.dumps(plan)}}]})

    monkeypatch.setattr(
        planning_agent,
        "call_openai_chat",
        lambda **_kwargs: ChatCallResult(ok=True, raw_text=raw, kind="ok"),
    )

    diagnostics: dict[str, Any] = {}
    payload, error = generate_plan(
        executable="",
        task_direction="plan",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
    )

    assert error == ""
    assert payload is not None
    assert "finish_reason" not in diagnostics


def test_generate_plan_uses_http_tool_use_transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "README.md", "Project notes\nsocial reply ranking lives in backend/main.py\n")
    _write(tmp_path / "backend" / "main.py", "def app():\n    return 'ok'\n")

    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    diagnostics: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    plan = {
        "summary": "tool planner",
        "tasks": [_task(files=["backend/main.py"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            payload = kwargs["payload"]
            tool_names = {
                item["function"]["name"]
                for item in payload["tools"]
                if isinstance(item, dict) and isinstance(item.get("function"), dict)
            }
            assert {"read_file", "read_file_partial", "search_file", "glob_files", "list_files_in_dir"} <= tool_names
            assert "write_file" not in tool_names
            assert "bash" not in tool_names
            return {
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "README.md", "limit": 500}),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        assert any(message.get("role") == "tool" for message in kwargs["payload"]["messages"])
        return {"choices": [{"message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with mimo tool use",
        context_text="Project files may need inspection.",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
    )

    assert error == ""
    assert payload is not None
    assert payload["summary"] == "tool planner"
    assert len(calls) == 2
    assert diagnostics["transport_kind"] == "http_tool_use"
    assert diagnostics["tool_calls"] == 1


def test_generate_plan_http_tool_use_rejects_missing_project_root(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")

    payload, error = generate_plan(
        executable="",
        task_direction="plan",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        project_root=None,
    )

    assert payload is None
    assert "requires project_root" in error


def test_generate_plan_http_tool_use_forces_final_after_tool_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "docs" / "plan.md", "Next task: add backend health route coverage.\n")
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_MAX_CALLS", "1")
    diagnostics: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    plan = {
        "summary": "forced final planner",
        "tasks": [_task(files=["backend/main.py"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "docs/plan.md"}),
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        payload = kwargs["payload"]
        assert "tools" not in payload
        assert "Tool budget is exhausted" in payload["messages"][-1]["content"]
        return {"choices": [{"message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with bounded tools",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
    )

    assert error == ""
    assert payload is not None
    assert payload["summary"] == "forced final planner"
    assert len(calls) == 2
    assert diagnostics["tool_forced_final"] is True
    assert diagnostics["tool_calls"] == 1


def test_generate_plan_http_tool_use_respects_http_timeout_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "README.md", "ok")
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_HTTP_TIMEOUT", "300")
    captured: dict[str, Any] = {}

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        plan = {
            "summary": "timeout planner",
            "tasks": [_task(files=["README.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
        }
        return {"choices": [{"message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with slow model",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        project_root=tmp_path,
        timeout_seconds=600,
    )

    assert error == ""
    assert payload is not None
    assert captured["timeout_seconds"] == 300
    assert captured["max_retries"] == 0


def test_generate_plan_http_tool_use_retries_are_separate_from_chat_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "README.md", "ok")
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_HTTP_MAX_RETRIES", "2")
    captured: dict[str, Any] = {}

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        plan = {
            "summary": "no retry planner",
            "tasks": [_task(files=["README.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
        }
        return {"choices": [{"message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with tool-use retry isolation",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        project_root=tmp_path,
    )

    assert error == ""
    assert payload is not None
    assert captured["max_retries"] == 0


def test_generate_plan_http_tool_use_writes_trace_jsonl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "README.md", "Project notes\n")
    planning_dir = tmp_path / "planning" / "feature"
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    plan = {
        "summary": "trace planner",
        "tasks": [_task(files=["README.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }
    calls = 0

    def _fake_post_chat(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "README.md"}),
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with trace",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        project_root=tmp_path,
        planning_dir=planning_dir,
    )

    assert error == ""
    assert payload is not None
    trace_path = planning_dir / ".planner_tool_use_trace.jsonl"
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_names = [event["event"] for event in events]
    assert "planner_tool_use_round_start" in event_names
    assert event_names.count("http_request_start") == 2
    assert event_names.count("http_request_end") == 2
    assert "tool_call_executed" in event_names
    assert "final_parse_result" in event_names
    assert events[-1]["ok"] is True
    assert all("test-key" not in json.dumps(event) for event in events)


def test_generate_plan_http_tool_use_decision_checkpoint_after_repeated_no_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "docs" / "prd.md", "P1 task details\n")
    planning_dir = tmp_path / "planning" / "feature"
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_ZERO_CONTENT_LIMIT", "2")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_NO_NEW_EVIDENCE_LIMIT", "1")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_CHECKPOINT_CALLS", "99")
    calls: list[dict[str, Any]] = []
    plan = {
        "summary": "checkpoint planner",
        "tasks": [_task(files=["docs/prd.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) <= 2:
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": f"call_{len(calls)}",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file_partial",
                                        "arguments": json.dumps({"path": "docs/prd.md", "offset": 0, "limit": 20}),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        payload = kwargs["payload"]
        assert "tools" not in payload
        assert "Decision checkpoint" in payload["messages"][-1]["content"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with repeated evidence",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out={},
        project_root=tmp_path,
        planning_dir=planning_dir,
    )

    assert error == ""
    assert payload is not None
    assert payload["summary"] == "checkpoint planner"
    events = [json.loads(line) for line in (planning_dir / ".planner_tool_use_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "progress_guard_triggered" in [event["event"] for event in events]


def test_generate_plan_http_tool_use_checkpoint_invalid_json_gets_repair_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "docs" / "prd.md", "P1 task details\n")
    planning_dir = tmp_path / "planning" / "feature"
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_ZERO_CONTENT_LIMIT", "1")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_CHECKPOINT_CALLS", "1")
    diagnostics: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    plan = {
        "summary": "checkpoint repaired planner",
        "tasks": [_task(files=["docs/prd.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "docs/prd.md"}),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        payload = kwargs["payload"]
        assert "tools" not in payload
        if len(calls) == 2:
            assert "Decision checkpoint" in payload["messages"][-1]["content"]
            return {"choices": [{"finish_reason": "stop", "message": {"content": "Here is the plan: {broken"}}]}
        assert len(calls) == 3
        assert "not valid JSON" in payload["messages"][-1]["content"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with checkpoint repair",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
        planning_dir=planning_dir,
    )

    assert error == ""
    assert payload is not None
    assert payload["summary"] == "checkpoint repaired planner"
    assert diagnostics.get("planner_error_kind") is None
    assert len(calls) == 3
    events = [json.loads(line) for line in (planning_dir / ".planner_tool_use_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    final_results = [event for event in events if event["event"] == "final_parse_result"]
    assert final_results[0]["ok"] is False
    assert final_results[0]["decision_checkpoint"] is True
    assert final_results[1]["ok"] is True
    assert final_results[1]["json_repair_attempt"] is True


def test_generate_plan_http_tool_use_empty_length_skips_json_repair(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "docs" / "prd.md", "P1 task details\n")
    planning_dir = tmp_path / "planning" / "feature"
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_ZERO_CONTENT_LIMIT", "1")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_CHECKPOINT_CALLS", "1")
    diagnostics: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "docs/prd.md"}),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        assert "Decision checkpoint" in kwargs["payload"]["messages"][-1]["content"]
        return {"choices": [{"finish_reason": "length", "message": {"content": ""}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with empty checkpoint",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
        planning_dir=planning_dir,
    )

    assert payload is None
    assert "planner_output_truncated_empty" in error
    assert diagnostics["planner_error_kind"] == "planner_output_truncated_empty"
    assert len(calls) == 2
    events = [json.loads(line) for line in (planning_dir / ".planner_tool_use_trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "decision_checkpoint_json_repair" not in [event.get("phase") for event in events]


def test_generate_plan_http_tool_use_maps_timeout_to_planner_transport_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    diagnostics: dict[str, Any] = {}

    def _timeout(**_kwargs: Any) -> dict[str, Any]:
        raise OpenAIToolUseExecutionError("HTTP_TIMEOUT", "request exceeded hard timeout (1s)")

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _timeout)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with timeout",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
    )

    assert payload is None
    assert "HTTP_TIMEOUT" in error
    assert diagnostics["planner_error_kind"] == "planner_transport_timeout"


def test_generate_plan_http_tool_use_compacts_large_tool_observation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "docs" / "large.md", "A" * 5000)
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_MAX_CALLS", "1")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_RESULT_MAX_CHARS", "700")
    calls: list[dict[str, Any]] = []
    plan = {
        "summary": "compact observation planner",
        "tasks": [_task(files=["docs/large.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "docs/large.md", "limit": 5000}),
                                    },
                                }
                            ]
                        },
                    }
                ]
            }
        tool_messages = [item for item in kwargs["payload"]["messages"] if item.get("role") == "tool"]
        assert tool_messages
        assert len(tool_messages[-1]["content"]) <= 700
        assert "host_truncated" in tool_messages[-1]["content"]
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with large docs",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        project_root=tmp_path,
    )

    assert error == ""
    assert payload is not None
    assert payload["summary"] == "compact observation planner"


def test_generate_plan_http_tool_use_new_evidence_delays_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "docs" / "a.md", "A\n")
    _write(tmp_path / "docs" / "b.md", "B\n")
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_ZERO_CONTENT_LIMIT", "2")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_NO_NEW_EVIDENCE_LIMIT", "1")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_CHECKPOINT_CALLS", "99")
    calls: list[dict[str, Any]] = []
    plan = {
        "summary": "new evidence planner",
        "tasks": [_task(files=["docs/a.md"], test_plan="python -m pytest -q", verify_cmd="python -m pytest -q")],
    }

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        if len(calls) == 1:
            path = "docs/a.md"
        elif len(calls) == 2:
            path = "docs/b.md"
        else:
            assert "Decision checkpoint" not in kwargs["payload"]["messages"][-1].get("content", "")
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(plan)}}]}
        return {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": f"call_{len(calls)}",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": json.dumps({"path": path}),
                                },
                            }
                        ],
                    },
                }
            ]
        }

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with useful reads",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        project_root=tmp_path,
    )

    assert error == ""
    assert payload is not None
    assert payload["summary"] == "new evidence planner"
    assert len(calls) == 3


def test_generate_plan_http_tool_use_checkpoint_blocker_sets_no_progress_kind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    _write(tmp_path / "docs" / "prd.md", "P1 task details\n")
    transport = WorkflowTransportConfig(
        name="mimo_tool_use",
        kind="http",
        driver="openai_compatible",
        interface="tool_use",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.tool_use"],
    )
    monkeypatch.setenv("WORKFLOW_MIMO_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_ZERO_CONTENT_LIMIT", "1")
    monkeypatch.setenv("WORKFLOW_PLANNER_TOOL_CHECKPOINT_CALLS", "1")
    diagnostics: dict[str, Any] = {}
    calls = {"count": 0}

    def _fake_post_chat(**kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": json.dumps({"path": "docs/prd.md"}),
                                    },
                                }
                            ],
                        },
                    }
                ]
            }
        blocker = {"status": "blocked", "reason": "insufficient_evidence", "evidence": ["docs/prd.md"]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(blocker)}}]}

    monkeypatch.setattr(planning_agent._tool_use_transport, "post_chat", _fake_post_chat)

    payload, error = generate_plan(
        executable="",
        task_direction="plan with blocker",
        context_text="ctx",
        model="mimo-v2.5-pro",
        transport=transport,
        diagnostics_out=diagnostics,
        project_root=tmp_path,
    )

    assert payload is None
    assert "decision checkpoint" in error
    assert diagnostics["planner_error_kind"] == "tool_use_no_progress"


def test_generate_plan_per_attempt_timeout_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import kodawari.autopilot.planning.planning_agent as planning_agent

    transport = WorkflowTransportConfig(
        name="mimo_chat",
        kind="http",
        driver="openai_compatible",
        interface="chat",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_MIMO_KEY",
        provides=["interface.chat"],
    )
    captured: dict[str, Any] = {}

    def _fake_call(**kwargs: Any) -> ChatCallResult:
        captured.update(kwargs)
        return ChatCallResult(
            ok=True,
            raw_text=json.dumps({"choices": [{"message": {"content": json.dumps({"summary": "x", "tasks": [_task()]})}}]}),
            kind="ok",
        )

    monkeypatch.setattr(planning_agent, "call_openai_chat", _fake_call)
    monkeypatch.setenv("WORKFLOW_PLANNER_PER_ATTEMPT_TIMEOUT", "45")
    monkeypatch.setenv("WORKFLOW_PLANNER_HTTP_MAX_RETRIES", "1")
    monkeypatch.setenv("WORKFLOW_PLANNER_MAX_TOKENS", "4096")
    monkeypatch.setenv("WORKFLOW_PLANNER_RESPONSE_FORMAT_JSON", "0")

    generate_plan(
        executable="",
        task_direction="plan",
        context_text="ctx",
        model="mimo-v2.5-pro",
        timeout_seconds=600,
        transport=transport,
        project_root=tmp_path,
    )

    assert captured["timeout_seconds"] == 45
    assert captured["max_retries"] == 1
    assert captured["max_tokens"] == 4096
    assert captured["response_format"] is None
    assert captured["total_timeout_seconds"] == 600


def test_review_plan_uses_http_chat_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    import kodawari.autopilot.planning.plan_reviewer as plan_reviewer

    transport = WorkflowTransportConfig(
        name="reviewer_chat",
        kind="http",
        driver="openai_compatible",
        interface="chat",
        api_format="openai_chat",
        base_url="https://example.test/v1",
        api_key_env="WORKFLOW_REVIEWER_KEY",
        provides=["interface.chat"],
    )

    def _fake_call(**kwargs: Any) -> ChatCallResult:
        assert kwargs["transport"] is transport
        assert kwargs["response_format"] == {"type": "json_object"}
        return ChatCallResult(
            ok=True,
            raw_text=json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "score": 10,
                                        "approved": True,
                                        "findings": [],
                                        "contradictions": [],
                                        "assessment": "ok",
                                    }
                                )
                            }
                        }
                    ]
                }
            ),
            kind="ok",
        )

    monkeypatch.setattr(plan_reviewer, "call_openai_chat", _fake_call)

    payload, error = review_plan(
        executable="",
        plan_payload={"summary": "x", "tasks": []},
        task_direction="review",
        context_text="ctx",
        model="review-model",
        transport=transport,
    )

    assert error == ""
    assert payload is not None
    assert payload["approved"] is True



# ---------------------------------------------------------------------------
# Phase C: JIT context tool loop tests
# ---------------------------------------------------------------------------


class TestJitContextCommand:
    def test_build_command_default_includes_allowed_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JIT defaults to ON for trusted local planning workspaces."""
        from kodawari.autopilot.planning.planning_agent import _build_command
        monkeypatch.delenv("WORKFLOW_PLANNER_JIT_CONTEXT", raising=False)
        cmd = _build_command(executable="claude", model="")
        assert "--allowedTools" in cmd

    def test_build_command_jit_enabled_includes_allowed_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_command
        monkeypatch.setenv("WORKFLOW_PLANNER_JIT_CONTEXT", "1")
        cmd = _build_command(executable="claude", model="")
        assert "--allowedTools" in cmd
        idx = cmd.index("--allowedTools")
        allowed = str(cmd[idx + 1])
        assert "Read" in allowed
        assert "Grep" in allowed
        assert "Glob" in allowed
        assert "Edit" not in allowed
        assert "Write" not in allowed
        assert "Bash" not in allowed

    def test_build_command_jit_disabled_omits_allowed_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_command
        monkeypatch.setenv("WORKFLOW_PLANNER_JIT_CONTEXT", "0")
        cmd = _build_command(executable="claude", model="")
        assert "--allowedTools" not in cmd

    def test_build_command_codex_driver_uses_codex_exec_read_only(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_command

        cmd = _build_command(executable="codex", model="gpt-5.5", driver="codex_cli")

        assert cmd[:4] == ["codex", "exec", "--skip-git-repo-check", "--sandbox"]
        assert "read-only" in cmd
        assert cmd[-2:] == ["--model", "gpt-5.5"]

    def test_build_command_wraps_windows_claude_cmd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_command

        monkeypatch.setattr("kodawari.autopilot.core.subprocess_compat._is_windows", lambda: True)
        monkeypatch.delenv("WORKFLOW_PLANNER_MAX_TURNS", raising=False)

        cmd = _build_command(executable=r"C:\npm\claude.cmd", model="", driver="claude_cli")

        assert cmd[:3] == ["cmd.exe", "/c", r"C:\npm\claude.cmd"]
        assert cmd[3:8] == ["-p", "--output-format", "json", "--max-turns", "20"]

    def test_generate_plan_runs_in_project_root_when_jit_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from kodawari.autopilot.planning import planning_agent

        expected_plan = {
            "summary": "Plan from Claude stdout",
            "tasks": [{"task_id": "T1", "files_to_change": ["README.md"], "new_files": []}],
        }
        seen: dict[str, Any] = {}

        def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            seen["command"] = list(command)
            seen["cwd"] = kwargs.get("cwd")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(expected_plan), stderr="")

        monkeypatch.setenv("WORKFLOW_PLANNER_JIT_CONTEXT", "0")
        monkeypatch.setattr(planning_agent.subprocess, "run", fake_run)

        plan, error = planning_agent.generate_plan(
            executable="claude",
            task_direction="t",
            context_text="ctx",
            driver="claude_cli",
            project_root=tmp_path,
        )

        assert error == ""
        assert plan == expected_plan
        assert seen["cwd"] == str(tmp_path.resolve())
        assert "--allowedTools" not in seen["command"]

    def test_generate_plan_codex_ignores_nonfatal_plugin_403_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from kodawari.autopilot.planning import planning_agent

        expected_plan = {
            "summary": "Plan from Codex stdout",
            "tasks": [
                {
                    "task_id": "T1",
                    "files_to_change": ["backend/main.py"],
                    "new_files": [],
                    "test_plan": "pytest -q",
                    "verify_cmd": "pytest -q",
                }
            ],
        }

        def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout=json.dumps(expected_plan),
                stderr=(
                    "WARN codex_core::plugins::startup_sync: "
                    "startup remote plugin sync failed with status 403 Forbidden"
                ),
            )

        monkeypatch.setattr(planning_agent.subprocess, "run", fake_run)

        plan, error = planning_agent.generate_plan(
            executable="codex",
            task_direction="t",
            context_text="ctx",
            driver="codex_cli",
            model="gpt-5.5",
            project_root=tmp_path,
        )

        assert error == ""
        assert plan == expected_plan

    def test_build_prompt_includes_tool_hint_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt
        monkeypatch.delenv("WORKFLOW_PLANNER_JIT_CONTEXT", raising=False)
        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
        )
        assert "Read, Grep, and Glob" in prompt
        assert "read-only planner" in prompt

    def test_build_prompt_omits_tool_hint_when_jit_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt
        monkeypatch.setenv("WORKFLOW_PLANNER_JIT_CONTEXT", "false")
        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
        )
        assert "Read, Grep, and Glob" not in prompt

    def test_build_prompt_includes_tool_hint_when_jit_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt
        monkeypatch.setenv("WORKFLOW_PLANNER_JIT_CONTEXT", "1")
        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
        )
        assert "Read, Grep, and Glob" in prompt
        assert "read-only planner" in prompt

    def test_build_prompt_declares_prd_as_authoritative(self) -> None:
        """Regression: planner previously invented route paths and flipped error
        contracts because the PRD was treated as just another doc. The prompt
        must now explicitly name the PRD Excerpt as the single source of truth
        for routes, signatures, error-handling, and response shapes.
        """
        from kodawari.autopilot.planning.planning_agent import _build_prompt

        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
        )
        assert "PRD authority" in prompt
        assert "single authoritative source" in prompt
        assert "route paths" in prompt
        assert "function names" in prompt
        assert "error-handling contracts" in prompt
        # Must explicitly forbid the most common drift: flipping raise/return
        assert "raise X" in prompt and "return []" in prompt
        # Out-of-scope fencing: items the PRD says not to do must not leak into tasks
        assert "out_of_scope" in prompt


class TestBuildPromptWorkspaceRoot:
    """1A: planner prompt must explicitly state the active workspace root."""

    def test_workspace_root_injected_when_project_root_given(
        self, tmp_path: Path
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt
        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx with reference to /other/project/root",
            previous_findings=None,
            round_number=1,
            project_root=tmp_path,
        )
        assert "ACTIVE WORKSPACE ROOT:" in prompt
        assert str(tmp_path.resolve()) in prompt
        # Must explicitly tell planner to ignore paths from CLAUDE.md
        assert "CLAUDE.md" in prompt
        assert "NOT the active workspace" in prompt

    def test_workspace_root_fallback_message_when_no_project_root(self) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt
        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
            project_root=None,
        )
        assert "ACTIVE WORKSPACE ROOT:" in prompt
        assert "<current working directory>" in prompt


class TestBuildPromptForbidMetaTasks:
    """2A: planner prompt must forbid standalone verification/meta tasks."""

    def test_meta_task_constraint_present(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt
        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
            project_root=tmp_path,
        )
        # Constraint body
        assert "NEVER create a standalone verification" in prompt
        # Names the concrete bad examples
        assert "check_code_redlines.py" in prompt
        # Explains the correct placement
        assert "verify_recipes" in prompt
        assert "write/both tasks files_to_change MUST be non-empty" in prompt
        assert "verification-only/no-op tasks may use files_to_change=[]" in prompt


class TestBuildPromptBundleImplAndTests:
    """Source + test must travel together in the same task's files_to_change.

    Matches review_precheck._test_scope_available() which checks per-task scope.
    Splitting impl into T1 and test into T2 causes REVIEW_SCOPE_CONFLICT.
    """

    def test_bundle_constraint_present(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt
        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
            project_root=tmp_path,
        )
        # The rule name / intent
        assert "BUNDLE implementation + tests in the SAME task" in prompt
        # Explains the reason (so planner can judge edge cases)
        assert "review precheck" in prompt.lower()
        # Names the exception (test-only follow-up coverage)
        assert "test-only" in prompt


class TestBuildPromptStructuredContracts:
    def test_prompt_includes_configured_planner_profile_overlay(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt

        profile_path = tmp_path / ".claude" / "workflow" / "prompts.yaml"
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        profile_path.write_text(
            textwrap.dedent(
                """
                profiles:
                  planner_kernel:
                    text: Planner kernel profile.
                  planner_overlays:
                    mimo:
                      text: Mimo should produce an early task graph.
                """
            ),
            encoding="utf-8",
        )

        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
            project_root=tmp_path,
            model="mimo-v2.5-pro",
            transport_name="mimo_api",
        )

        assert "Prompt profile directives (planner/mimo):" in prompt
        assert "Planner kernel profile." in prompt
        assert "Mimo should produce an early task graph." in prompt

    def test_prompt_requires_structured_contract_fields(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt

        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
            project_root=tmp_path,
        )

        assert "provides" in prompt
        assert "requires" in prompt
        assert "api_contracts" in prompt
        assert "Structured consistency contracts" in prompt
        assert "table.column" in prompt
        assert "response_shape" in prompt

    def test_revision_prompt_includes_previous_plan_and_change_log_contract(
        self, tmp_path: Path
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt

        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=[
                {
                    "severity": "blocking",
                    "description": "T1 response shape conflicts with T2",
                }
            ],
            previous_plan={"summary": "previous plan", "tasks": [{"task_id": "T1"}]},
            round_number=2,
            project_root=tmp_path,
        )

        assert "Previous plan:" in prompt
        assert "previous plan" in prompt
        assert "change_log" in prompt
        assert "silent rewrites" in prompt


class TestExistingTestMutationContracts:
    def test_prompt_requires_existing_test_mutation_fields(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _build_prompt

        prompt = _build_prompt(
            task_direction="t",
            context_text="ctx",
            previous_findings=None,
            round_number=1,
            project_root=tmp_path,
        )

        assert "Existing-test mutation contracts" in prompt
        assert "related_existing_tests" in prompt
        assert "read_only_files" in prompt
        assert "executor's explicit read context" in prompt
        assert "allowed_test_mutations" in prompt
        assert "behavior_changes" in prompt
        assert "from -> to" in prompt
        assert "keep <test path> passing" in prompt
        assert "VERIFY-ONLY" in prompt

    def test_route_change_requires_targeted_existing_test_scope(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_plan

        route = tmp_path / "backend" / "api" / "v1" / "routes" / "social_routes.py"
        test_file = tmp_path / "tests" / "test_social_routes.py"
        route.parent.mkdir(parents=True)
        test_file.parent.mkdir(parents=True)
        route.write_text("def route():\n    pass\n", encoding="utf-8")
        test_file.write_text("def test_social_routes():\n    assert True\n", encoding="utf-8")

        plan = {
            "tasks": [
                {
                    "task_id": "T1",
                    "task_name": "Social API behavior",
                    "layer_owner": "route",
                    "surface": "rest_api",
                    "files_to_change": ["backend/api/v1/routes/social_routes.py"],
                    "new_files": [],
                    "invariants": ["preserve auth"],
                    "test_plan": "python -m pytest tests/test_social_routes.py -q",
                    "verify_cmd": "python -m pytest tests/test_social_routes.py -q",
                    "depends_on": [],
                    "provides": [],
                    "requires": [],
                    "api_contracts": [],
                }
            ],
            "verify_recipes": [],
        }

        errors = _validate_plan(plan, project_root=tmp_path)

        assert any("related_existing_tests missing targeted existing tests" in item for item in errors)

    def test_route_change_accepts_related_existing_test_scope(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_plan

        route = tmp_path / "backend" / "api" / "v1" / "routes" / "social_routes.py"
        test_file = tmp_path / "tests" / "test_social_routes.py"
        route.parent.mkdir(parents=True)
        test_file.parent.mkdir(parents=True)
        route.write_text("def route():\n    pass\n", encoding="utf-8")
        test_file.write_text("def test_social_routes():\n    assert True\n", encoding="utf-8")

        plan = {
            "tasks": [
                {
                    "task_id": "T1",
                    "task_name": "Social API behavior",
                    "layer_owner": "route",
                    "surface": "rest_api",
                    "files_to_change": ["backend/api/v1/routes/social_routes.py"],
                    "new_files": [],
                    "related_existing_tests": ["tests/test_social_routes.py"],
                    "invariants": ["preserve auth"],
                    "test_plan": "python -m pytest tests/test_social_routes.py -q",
                    "verify_cmd": "python -m pytest tests/test_social_routes.py -q",
                    "depends_on": [],
                    "provides": [],
                    "requires": [],
                    "api_contracts": [],
                }
            ],
            "verify_recipes": [],
        }

        errors = _validate_plan(plan, project_root=tmp_path)

        assert not [item for item in errors if "related_existing_tests missing" in item]


class TestValidateVerifyCommands:
    """1C: validator fail-fasts on verify commands that escape the workspace."""

    def test_verify_recipe_within_workspace_is_ok(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_verify_commands
        # Use a path that's actually under tmp_path
        internal = tmp_path / "tests"
        internal.mkdir()
        plan = {
            "verify_recipes": [
                {"command": f"cd {tmp_path} && python -m pytest tests/ -q"}
            ],
            "tasks": [],
        }
        errors = _validate_verify_commands(plan, project_root=tmp_path)
        assert errors == []

    def test_verify_recipe_outside_workspace_detected(
        self, tmp_path: Path
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_verify_commands
        # Simulate the real regression: planner copy-pasted path from CLAUDE.md
        plan = {
            "verify_recipes": [
                {
                    "command": (
                        "cd E:/code_rebuild/newsapp && "
                        "python -m pytest tests/test_t001_workspace_smoke.py -q"
                    )
                }
            ],
            "tasks": [],
        }
        errors = _validate_verify_commands(plan, project_root=tmp_path)
        assert len(errors) == 1
        assert "verify_recipes[0].command" in errors[0]
        assert "E:/code_rebuild/newsapp" in errors[0]

    def test_task_verify_cmd_outside_workspace_detected(
        self, tmp_path: Path
    ) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_verify_commands
        plan = {
            "verify_recipes": [],
            "tasks": [
                {
                    "task_id": "T1",
                    "verify_cmd": "cd E:/code_rebuild/newsapp && pytest -q",
                }
            ],
        }
        errors = _validate_verify_commands(plan, project_root=tmp_path)
        assert len(errors) == 1
        assert "tasks[1].verify_cmd" in errors[0]

    def test_relative_path_in_command_is_ok(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_verify_commands
        plan = {
            "verify_recipes": [{"command": "python -m pytest tests/ -q"}],
            "tasks": [{"task_id": "T1", "verify_cmd": "pytest tests/ -q"}],
        }
        errors = _validate_verify_commands(plan, project_root=tmp_path)
        assert errors == []

    def test_multiple_outside_paths_in_one_command(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_verify_commands
        plan = {
            "verify_recipes": [
                {
                    "command": (
                        "cp E:/code_rebuild/other/a.py F:/backup/b.py && pytest"
                    )
                }
            ],
            "tasks": [],
        }
        errors = _validate_verify_commands(plan, project_root=tmp_path)
        assert len(errors) == 1
        assert "E:/code_rebuild/other/a.py" in errors[0]
        assert "F:/backup/b.py" in errors[0]

    def test_quoted_absolute_path_detected(self, tmp_path: Path) -> None:
        from kodawari.autopilot.planning.planning_agent import _validate_verify_commands
        plan = {
            "verify_recipes": [
                {"command": 'python "E:/code_rebuild/newsapp/run.py"'}
            ],
            "tasks": [],
        }
        errors = _validate_verify_commands(plan, project_root=tmp_path)
        assert len(errors) == 1

    def test_validate_plan_surfaces_verify_command_error(
        self, tmp_path: Path
    ) -> None:
        """End-to-end: _validate_plan must include verify-command errors."""
        from kodawari.autopilot.planning.planning_agent import _validate_plan
        # Create a valid file so the files_to_change check passes
        (tmp_path / "backend").mkdir()
        (tmp_path / "backend" / "main.py").write_text("pass", encoding="utf-8")
        plan = {
            "tasks": [
                {
                    "task_id": "T1",
                    "task_name": "Task T1",
                    "layer_owner": "service",
                    "surface": "rest_api",
                    "files_to_change": ["backend/main.py"],
                    "new_files": [],
                    "invariants": ["no regression"],
                    "test_plan": "pytest tests/ -q",
                    "verify_cmd": "cd E:/code_rebuild/newsapp && pytest",
                    "depends_on": [],
                    "provides": [],
                    "requires": [],
                    "api_contracts": [],
                }
            ],
            "verify_recipes": [],
        }
        errors = _validate_plan(plan, project_root=tmp_path)
        # The verify_cmd path error must appear; other structural checks should pass
        verify_errors = [e for e in errors if "verify_cmd" in e]
        assert len(verify_errors) == 1

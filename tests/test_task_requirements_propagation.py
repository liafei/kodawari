"""Tests for task_requirements field propagation through execution pipeline.

Verifies that the full requirements text reaches executor prompts without
touching task_scope (which controls authorization and routing).
"""

from __future__ import annotations

from typing import Any

import pytest

from kodawari.autopilot import execution_artifacts, execution_claude_code, execution_codex_cli
from kodawari.autopilot.engine_support import is_authorized_to_modify


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MULTILINE_REQS = "Add a function X that:\n- reads from stdin\n- writes to stdout\n- handles EOF"


def _base_context(tmp_path: Any, requirements: str = "") -> dict[str, Any]:
    planning_dir = tmp_path / "planning" / "feat"
    planning_dir.mkdir(parents=True, exist_ok=True)
    return {
        "project_root": str(tmp_path),
        "planning_dir": str(planning_dir),
        "task_id": "T001",
        "requested_action": "implement",
        "task_scope": "Add function X",
        "requirements": requirements,
        "task_invariants": [],
    }


# ---------------------------------------------------------------------------
# 1. execution_request schema contains task_requirements
# ---------------------------------------------------------------------------

def test_build_execution_request_includes_task_requirements(tmp_path: Any) -> None:
    ctx = _base_context(tmp_path, requirements=_MULTILINE_REQS)
    payload = execution_artifacts.build_execution_request(
        feature="feat",
        task="T001: implement",
        context=ctx,
        backend="claude_code",
        command="",
        allowed_files=["src/main.py"],
    )
    assert "task_requirements" in payload
    assert payload["task_requirements"] == _MULTILINE_REQS


def test_build_execution_request_task_requirements_empty_when_no_requirements(tmp_path: Any) -> None:
    ctx = _base_context(tmp_path, requirements="")
    payload = execution_artifacts.build_execution_request(
        feature="feat",
        task="T001: implement",
        context=ctx,
        backend="claude_code",
        command="",
        allowed_files=["src/main.py"],
    )
    assert payload.get("task_requirements", "") == ""


def test_task_requirements_capped_at_8192_chars(tmp_path: Any) -> None:
    long_reqs = "x" * 10000
    ctx = _base_context(tmp_path, requirements=long_reqs)
    payload = execution_artifacts.build_execution_request(
        feature="feat",
        task="T001: implement",
        context=ctx,
        backend="claude_code",
        command="",
        allowed_files=["src/main.py"],
    )
    assert len(payload["task_requirements"]) <= 8192


# ---------------------------------------------------------------------------
# 2. task_scope is NOT expanded (authorization regression guard)
# ---------------------------------------------------------------------------

def test_task_scope_unchanged_when_requirements_present(tmp_path: Any) -> None:
    ctx = _base_context(tmp_path, requirements=_MULTILINE_REQS)
    payload = execution_artifacts.build_execution_request(
        feature="feat",
        task="T001: implement",
        context=ctx,
        backend="claude_code",
        command="",
        allowed_files=["src/main.py"],
    )
    assert payload["task_scope"] == "Add function X"


def test_is_authorized_ignores_requirements_text(tmp_path: Any) -> None:
    # requirements mentions a protected path — must NOT grant authorization
    reqs_with_protected_path = "Do not modify tests/conftest.py"
    ctx = _base_context(tmp_path, requirements=reqs_with_protected_path)
    payload = execution_artifacts.build_execution_request(
        feature="feat",
        task="T001: implement",
        context=ctx,
        backend="claude_code",
        command="",
        allowed_files=["src/main.py"],
    )
    # is_authorized_to_modify only sees task_label + task_scope, not requirements
    authorized = is_authorized_to_modify(
        "tests/conftest.py",
        task_label=payload["task"],
        task_scope=payload["task_scope"],
    )
    assert not authorized, "requirements text must not grant file authorization"


# ---------------------------------------------------------------------------
# 3. Claude Code prompt renders Requirements block
# ---------------------------------------------------------------------------

def _claude_code_prompt(request_payload: dict[str, Any]) -> str:
    return execution_claude_code._request_prompt(request_payload=request_payload)


def test_claude_code_prompt_includes_full_requirements() -> None:
    payload = {
        "task": "T001: implement",
        "feature": "feat",
        "task_id": "T001",
        "archetype": "standard",
        "capabilities": [],
        "surface": "backend",
        "files_to_change": ["src/main.py"],
        "task_scope": "Add function X",
        "task_requirements": _MULTILINE_REQS,
        "invariants": [],
        "verify_cmd": "",
        "request_path": "",
        "execution_root": "",
    }
    prompt = _claude_code_prompt(payload)
    assert "Requirements:" in prompt
    assert "reads from stdin" in prompt
    assert "writes to stdout" in prompt
    assert "handles EOF" in prompt


def test_claude_code_prompt_omits_requirements_block_when_empty() -> None:
    payload = {
        "task": "T001: implement",
        "feature": "feat",
        "task_id": "T001",
        "archetype": "standard",
        "capabilities": [],
        "surface": "backend",
        "files_to_change": ["src/main.py"],
        "task_scope": "Add function X",
        "task_requirements": "",
        "invariants": [],
        "verify_cmd": "",
        "request_path": "",
        "execution_root": "",
    }
    prompt = _claude_code_prompt(payload)
    assert "Requirements:" not in prompt


def test_claude_code_scope_still_present_alongside_requirements() -> None:
    payload = {
        "task": "T001: implement",
        "feature": "feat",
        "task_id": "T001",
        "archetype": "standard",
        "capabilities": [],
        "surface": "backend",
        "files_to_change": ["src/main.py"],
        "task_scope": "Add function X",
        "task_requirements": _MULTILINE_REQS,
        "invariants": [],
        "verify_cmd": "",
        "request_path": "",
        "execution_root": "",
    }
    prompt = _claude_code_prompt(payload)
    assert "Scope: Add function X" in prompt
    assert "Requirements:" in prompt


# ---------------------------------------------------------------------------
# 4. Codex CLI prompt renders Requirements block
# ---------------------------------------------------------------------------

def _codex_prompt(request_payload: dict[str, Any]) -> str:
    return execution_codex_cli._request_prompt(request_payload=request_payload)


def test_codex_prompt_includes_full_requirements() -> None:
    payload = {
        "task": "T001: implement",
        "feature": "feat",
        "task_id": "T001",
        "archetype": "standard",
        "capabilities": [],
        "surface": "backend",
        "files_to_change": ["src/main.py"],
        "task_scope": "Add function X",
        "task_requirements": _MULTILINE_REQS,
        "invariants": [],
        "verify_cmd": "",
        "request_path": "",
    }
    prompt = _codex_prompt(payload)
    assert "Requirements:" in prompt
    assert "reads from stdin" in prompt


def test_codex_prompt_omits_requirements_block_when_empty() -> None:
    payload = {
        "task": "T001: implement",
        "feature": "feat",
        "task_id": "T001",
        "archetype": "standard",
        "capabilities": [],
        "surface": "backend",
        "files_to_change": ["src/main.py"],
        "task_scope": "Add function X",
        "task_requirements": "",
        "invariants": [],
        "verify_cmd": "",
        "request_path": "",
    }
    prompt = _codex_prompt(payload)
    assert "Requirements:" not in prompt

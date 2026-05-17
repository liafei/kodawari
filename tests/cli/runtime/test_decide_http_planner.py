"""Unit tests for the HTTP-planner-aware decide path.

Covers three fixes landed for fully-autonomous gate_complexity escalation:

* Fix A — ``_call_planner_via_role`` uses the project's planner role transport
  (HTTP) instead of the legacy ``claude -p`` subprocess that hangs in
  headless environments.
* Fix B — ``auto_decide_pending`` writes the recovery card + response files
  inline so the next autopilot start can resume without operator interaction.
* Fix C — the planner prompt template carries the hard complexity targets
  and the "replace, don't add" rule.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from kodawari.cli.runtime.decide_cmd import (
    _build_planner_prompt,
    _call_planner_via_role,
    _planner_complexity_target,
    auto_decide_pending,
)


# ---------------------------------------------------------------------------
# Fix C — prompt template assertions
# ---------------------------------------------------------------------------


def test_prompt_includes_hard_complexity_targets() -> None:
    prompt = _build_planner_prompt(
        task_id="T9",
        detector_hint="gate_complexity",
        failure_summary="complexity 14 exceeds 10",
        violation_info={"symbol": "foo", "actual": 14, "limit": 10, "path": "p.py"},
        function_source="def foo(): pass",
        invariants=["public signature stable"],
        files_to_change=["p.py"],
    )
    assert "complexity ≤ 8" in prompt or "complexity <= 8" in prompt
    assert "ADDING helpers without REDUCING" in prompt
    assert "AST complexity check" in prompt
    assert "Concrete helper list" in prompt
    assert "New body sketch" in prompt
    assert "Replace-don't-add rule" in prompt


def test_complexity_target_picks_strict_value() -> None:
    assert _planner_complexity_target(10) == 8
    assert _planner_complexity_target(7) == 5
    assert _planner_complexity_target(4) == 4
    # Non-numeric / missing limit falls back to a safe default.
    assert _planner_complexity_target("?") == 8
    assert _planner_complexity_target(None) == 8


# ---------------------------------------------------------------------------
# Fix A — _call_planner_via_role
# ---------------------------------------------------------------------------


def _models_yaml_with_http_planner() -> str:
    return """schema_version: "models.v2"
transports:
  fake_http:
    kind: http
    driver: openai_compatible
    interface: tool_use
    api_format: openai_chat
    base_url: https://fake.invalid/v1
    api_key_env: TEST_FAKE_KEY
    quota_group: fake
    provides: [interface.tool_use, repo.read_file, repo.write_file]
compatibility:
  - {models: [fake-model], transports: [fake_http], interfaces: [tool_use], api_formats: [openai_chat]}
roles:
  planner:
    transport: fake_http
    model: fake-model
    requires: [interface.tool_use]
    on_unavailable: fail
"""


def _scaffold_project_with_models_yaml(root: Path, *, yaml: str) -> Path:
    (root / ".claude" / "workflow").mkdir(parents=True)
    (root / ".claude" / "workflow" / "models.yaml").write_text(yaml, encoding="utf-8")
    return root


def test_call_planner_via_role_uses_http_when_planner_is_http(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _scaffold_project_with_models_yaml(Path(tmp), yaml=_models_yaml_with_http_planner())
        monkeypatch.setenv("TEST_FAKE_KEY", "sk-test")

        captured: dict = {}

        class _FakeResp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *exc: object) -> None:
                pass

            def read(self) -> bytes:
                return self._body

        def _fake_urlopen(req, timeout=None):  # noqa: ANN001
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(
                json.dumps(
                    {"choices": [{"message": {"content": '{"options":[{"title":"x","description":"y"}]}'}}]}
                ).encode("utf-8")
            )

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        out = _call_planner_via_role("hello", project_root=root)

    assert out is not None
    assert '"options"' in out
    assert captured["url"] == "https://fake.invalid/v1/chat/completions"
    assert captured["body"]["model"] == "fake-model"
    assert captured["headers"].get("Authorization") == "Bearer sk-test"


def test_call_planner_via_role_returns_none_when_no_planner_role() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # models.yaml without any planner role at all.
        root = _scaffold_project_with_models_yaml(
            Path(tmp), yaml='schema_version: "models.v2"\ntransports: {}\nroles: {}\n'
        )
        assert _call_planner_via_role("hi", project_root=root) is None


def test_call_planner_via_role_returns_none_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _scaffold_project_with_models_yaml(Path(tmp), yaml=_models_yaml_with_http_planner())
        monkeypatch.delenv("TEST_FAKE_KEY", raising=False)
        assert _call_planner_via_role("hi", project_root=root) is None


def test_call_planner_via_role_returns_none_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    with tempfile.TemporaryDirectory() as tmp:
        root = _scaffold_project_with_models_yaml(Path(tmp), yaml=_models_yaml_with_http_planner())
        monkeypatch.setenv("TEST_FAKE_KEY", "sk-test")
        monkeypatch.setattr(
            "urllib.request.urlopen",
            mock.Mock(side_effect=urllib.error.URLError("boom")),
        )
        assert _call_planner_via_role("hi", project_root=root) is None


# ---------------------------------------------------------------------------
# Fix B — auto_decide_pending
# ---------------------------------------------------------------------------


def _seed_planning_dir(root: Path, *, with_request: bool = True) -> Path:
    pdir = root / "planning" / "demo_feature"
    pdir.mkdir(parents=True)
    # Original task card the auto-decide reads to find files_to_change/invariants.
    (pdir / "TASK_CARD_T1.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "files_to_change": ["foo.py"],
                "invariants": ["public API stays"],
                "test_plan": "pytest -q",
            }
        ),
        encoding="utf-8",
    )
    if with_request:
        (pdir / ".executor_redesign_request.json").write_text(
            json.dumps(
                {
                    "task_id": "T1",
                    "failure_summary": "foo.py: Function bar complexity 14 exceeds 10",
                    "detector_hint": "gate_complexity",
                    "escalation_count": 1,
                }
            ),
            encoding="utf-8",
        )
    # Autopilot state stub so _rewind_state_to_task has something to write to.
    (pdir / ".autopilot_state.json").write_text(
        json.dumps(
            {
                "completed_tasks": [],
                "current_stage": "IMPLEMENT",
                "cycle": 3,
                "active_task": "T1",
                "last_stage_status": "executor_recovery_escalated",
                "last_error": "complexity",
            }
        ),
        encoding="utf-8",
    )
    return pdir


def test_auto_decide_writes_recovery_card_and_response(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _scaffold_project_with_models_yaml(Path(tmp), yaml=_models_yaml_with_http_planner())
        pdir = _seed_planning_dir(root)
        monkeypatch.setenv("TEST_FAKE_KEY", "sk-test")
        monkeypatch.delenv("WORKFLOW_AUTO_DECIDE", raising=False)  # default ON

        class _FakeResp:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self) -> "_FakeResp":
                return self

            def __exit__(self, *exc: object) -> None:
                pass

            def read(self) -> bytes:
                return self._body

        planner_options = {
            "options": [
                {
                    "title": "Split bar into 3 helpers",
                    "description": "Extract _is_eligible, _normalize_row, _build_payload — each ≤ 5 complexity.",
                },
                {
                    "title": "Inline dispatch table",
                    "description": "Replace if/elif chain with a dict lookup.",
                },
            ]
        }
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda req, timeout=None: _FakeResp(  # noqa: ARG005
                json.dumps(
                    {"choices": [{"message": {"content": json.dumps(planner_options)}}]}
                ).encode("utf-8")
            ),
        )

        ok = auto_decide_pending(pdir, project_root=root)
        assert ok is True

        # Audit response
        audit = json.loads((pdir / ".executor_redesign_response.json").read_text(encoding="utf-8"))
        assert audit["action"] == "accept"
        assert audit["option_index"] == 0
        assert audit["auto_accepted_via"] == "auto_decide_pending"
        assert audit["option"]["title"] == "Split bar into 3 helpers"

        # Sticky decision carries the must_fix list
        sticky = json.loads((pdir / ".user_redesign_decision.json").read_text(encoding="utf-8"))
        assert sticky["task_id"] == "T1"
        assert sticky["chosen_title"] == "Split bar into 3 helpers"
        assert isinstance(sticky["must_fix"], list)
        assert sticky["must_fix"], "must_fix should be populated from the recovery card"

        # Recovery card + active card both written
        assert (pdir / ".execution_recovery_card.json").exists()
        assert (pdir / "TASK_CARD_ACTIVE.json").exists()

        # Unified response so find_pending_request stops surfacing it
        unified = json.loads((pdir / ".executor_decision_response.json").read_text(encoding="utf-8"))
        assert unified["phase"] == "executor"
        assert unified["escalation_kind"] == "GATE_REFACTOR_NEEDED"
        assert unified["action"] == "accept"

        # The request file is consumed
        assert not (pdir / ".executor_redesign_request.json").exists()

        # State was rewound: cycle=0, stage=INIT, completed_tasks scrubbed of T1
        state = json.loads((pdir / ".autopilot_state.json").read_text(encoding="utf-8"))
        assert state["cycle"] == 0
        assert state["current_stage"] == "INIT"
        assert state["last_stage_status"] == "executor_redesign_accepted"


def test_auto_decide_no_op_when_no_request() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _scaffold_project_with_models_yaml(Path(tmp), yaml=_models_yaml_with_http_planner())
        pdir = _seed_planning_dir(root, with_request=False)
        # Should return False without raising even though there's no request file.
        assert auto_decide_pending(pdir, project_root=root) is False


def test_auto_decide_disabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = _scaffold_project_with_models_yaml(Path(tmp), yaml=_models_yaml_with_http_planner())
        pdir = _seed_planning_dir(root)
        monkeypatch.setenv("WORKFLOW_AUTO_DECIDE", "0")
        # urlopen should never be called when disabled.
        sentinel = mock.Mock(side_effect=AssertionError("urlopen must not be called"))
        monkeypatch.setattr("urllib.request.urlopen", sentinel)
        assert auto_decide_pending(pdir, project_root=root) is False
        assert (pdir / ".executor_redesign_request.json").exists(), "request must not be consumed"

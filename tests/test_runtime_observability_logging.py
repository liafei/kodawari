from __future__ import annotations

import builtins
import importlib
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from kodawari.autopilot import engine_context_mixin, engine_support, gate_round, hook_lifecycle, state
from kodawari.cli import autopilot_cmd, autopilot_workflow_runtime, gate_state_sync, status_cmd
from kodawari.cli.autopilot_workflow_runtime import _safe_select_instinct_hints


def test_gate_state_sync_logs_state_load_failure(tmp_path: Path, caplog: object, monkeypatch: object) -> None:
    planning_dir = tmp_path / "planning" / "feature"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / ".autopilot_state.json").write_text("{}", encoding="utf-8")

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("state load boom")

    monkeypatch.setattr(gate_state_sync.AutopilotState, "load", _raise)
    with caplog.at_level(logging.WARNING):
        gate_state_sync.sync_gate_side_effects(planning_dir, {"total_status": "PASS"})

    assert "failed to load autopilot state during gate sync" in caplog.text


def test_autopilot_workflow_runtime_logs_instinct_selector_failure(tmp_path: Path, caplog: object) -> None:
    def _selector(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("instinct boom")

    with caplog.at_level(logging.WARNING):
        payload = _safe_select_instinct_hints(_selector, tmp_path)

    assert payload == []
    assert "instinct hint selection failed during autopilot workflow runtime" in caplog.text


def test_autopilot_workflow_runtime_logs_instinct_selector_import_failure(
    tmp_path: Path,
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kodawari.instincts":
            raise RuntimeError("instinct import boom")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    with caplog.at_level(logging.WARNING):
        payload = autopilot_workflow_runtime._load_instinct_hints(tmp_path)

    assert payload == []
    assert "instinct selector unavailable during autopilot workflow runtime" in caplog.text


def test_status_cmd_logs_state_hydration_failure(caplog: object) -> None:
    class _BrokenState:
        @classmethod
        def from_dict(cls, payload: dict[str, object]) -> "_BrokenState":
            raise RuntimeError("hydrate boom")

    with caplog.at_level(logging.WARNING):
        state = status_cmd._state_from_payload(_BrokenState, {"feature": "demo"})

    assert state is None
    assert "failed to hydrate autopilot state model from status payload" in caplog.text


def test_status_cmd_logs_unified_status_failure(caplog: object) -> None:
    class _BrokenState:
        def get_unified_status(self) -> dict[str, object]:
            raise RuntimeError("unified boom")

    with caplog.at_level(logging.WARNING):
        payload = status_cmd._state_unified_status(_BrokenState())

    assert payload is None
    assert "failed to read unified autopilot status from state model" in caplog.text


def test_status_cmd_logs_state_model_import_failure_and_uses_fallback_status(
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kodawari.autopilot.state":
            raise RuntimeError("state import boom")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    with caplog.at_level(logging.WARNING):
        payload = status_cmd._build_unified_autopilot_status(
            {
                "current_stage": "VERIFY",
                "active_task": "T001: Verify demo",
                "active_subtask": "T001.1",
                "subtasks": {
                    "T001.1": {
                        "status": "FAILED",
                        "error": "fixture missing",
                    }
                },
                "final_status": "",
                "stop_reason": "",
            }
        )

    assert payload["current_phase"] == "VERIFY"
    assert payload["current_task_id"] == "T001"
    assert payload["blocking_reason"] == "fixture missing"
    assert payload["next_action"] == "Repair the failed subtask and rerun scoped verify"
    assert "autopilot state model unavailable while building status payload" in caplog.text


def test_autopilot_cmd_logs_state_reload_failure(tmp_path: Path, caplog: object) -> None:
    state_path = tmp_path / ".autopilot_state.json"
    state_path.write_text("{}", encoding="utf-8")

    class _BrokenState:
        def __init__(self, feature: str, project_root: Path) -> None:
            self.feature = feature
            self.project_root = project_root

        @classmethod
        def load(cls, path: Path) -> "_BrokenState":
            del path
            raise RuntimeError("state load boom")

    with caplog.at_level(logging.WARNING):
        payload = autopilot_cmd._load_or_init_state(
            state_path=state_path,
            feature="demo",
            project_root=tmp_path,
            state_cls=_BrokenState,
        )

    assert isinstance(payload, _BrokenState)
    assert payload.feature == "demo"
    assert "failed to load existing autopilot state; reinitializing" in caplog.text


class _DummyEngineContext(engine_context_mixin.EngineContextMixin):
    def __init__(self, project_root: Path) -> None:
        self.config = SimpleNamespace(project_root=project_root)


def test_engine_context_logs_instinct_import_failure(
    tmp_path: Path,
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kodawari.instincts":
            raise RuntimeError("instinct import boom")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    context = _DummyEngineContext(tmp_path)
    with caplog.at_level(logging.WARNING):
        payload = context._load_learned_instinct_hints(limit=1, min_confidence=0.5)

    assert payload == []
    assert "instinct selector unavailable while building implementation context" in caplog.text


def test_engine_context_logs_instinct_selector_failure(
    tmp_path: Path,
    caplog: object,
    monkeypatch: object,
) -> None:
    module = ModuleType("kodawari.instincts")

    def _raise(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("selector boom")

    module.select_instinct_hints = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kodawari.instincts", module)
    context = _DummyEngineContext(tmp_path)
    with caplog.at_level(logging.WARNING):
        payload = context._load_learned_instinct_hints(limit=1, min_confidence=0.5)

    assert payload == []
    assert "instinct hint load failed while building implementation context" in caplog.text


def test_engine_support_logs_pattern_registry_fallback(
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kodawari.patterns":
            raise RuntimeError("patterns import boom")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    with caplog.at_level(logging.WARNING):
        registry = engine_support.build_default_pattern_registry()

    assert registry.__class__.__name__ == "_FallbackPatternRegistry"
    assert "falling back to minimal pattern registry" in caplog.text


def test_engine_support_logs_adapter_fallback(
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__
    _blocked = frozenset({
        "kodawari.autopilot.local_adapter",
        "kodawari.autopilot.execution.local_adapter",
    })

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name in _blocked:
            raise RuntimeError("adapter import boom")
        return original_import(name, *args, **kwargs)

    # Remove cached modules so the deferred import inside build_default_adapter is re-attempted.
    for key in list(sys.modules.keys()):
        if "local_adapter" in key:
            monkeypatch.delitem(sys.modules, key, raising=False)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    with caplog.at_level(logging.WARNING):
        adapter = engine_support.build_default_adapter()

    assert adapter.check_health() == (False, "fallback-adapter-unavailable")
    assert "falling back to minimal adapter" in caplog.text


def test_gate_round_logs_incremental_compact_refresh_failure(caplog: object) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("compact boom")

    engine = SimpleNamespace(_refresh_semantic_compact=_raise)
    runtime = SimpleNamespace()
    with caplog.at_level(logging.WARNING):
        gate_round._refresh_incremental_compact(
            engine,
            runtime,
            reason="gate_blocked",
            trigger_event="post_gate",
        )

    assert "incremental semantic compact refresh failed after gate block" in caplog.text


def test_hook_lifecycle_logs_instinct_module_failure(
    tmp_path: Path,
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kodawari.instincts":
            raise RuntimeError("instinct import boom")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    with caplog.at_level(logging.WARNING):
        payload = hook_lifecycle._load_instinct_hints(
            root=tmp_path,
            hints_limit=3,
            min_confidence=0.5,
        )

    assert payload["status"] == "module_unavailable"
    assert "instinct module unavailable while building compact payload" in caplog.text


def test_hook_lifecycle_logs_instinct_hint_load_failure(
    tmp_path: Path,
    caplog: object,
    monkeypatch: object,
) -> None:
    module = ModuleType("kodawari.instincts")

    def _raise(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("hint boom")

    module.select_instinct_hints = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kodawari.instincts", module)
    with caplog.at_level(logging.WARNING):
        payload = hook_lifecycle._load_instinct_hints(
            root=tmp_path,
            hints_limit=3,
            min_confidence=0.5,
        )

    assert payload["status"] == "load_failed"
    assert payload["error"] == "hint boom"
    assert "instinct hint load failed while building compact payload" in caplog.text


def test_state_logs_instinct_ingest_import_failure(
    tmp_path: Path,
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kodawari.instincts":
            raise RuntimeError("instinct import boom")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    autopilot_state = state.AutopilotState(feature="demo", project_root=tmp_path)
    event = state.ErrorEvent(
        timestamp="2026-03-27T00:00:00+00:00",
        phase="VERIFY",
        action="verify",
        category="verify",
        message="verify failed",
    )
    with caplog.at_level(logging.WARNING):
        autopilot_state._ingest_error_learning(event)

    assert "instinct error-ingestion module unavailable" in caplog.text


def test_state_logs_instinct_ingest_failure(
    tmp_path: Path,
    caplog: object,
    monkeypatch: object,
) -> None:
    module = ModuleType("kodawari.instincts")

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("ingest boom")

    module.ingest_error_event = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kodawari.instincts", module)
    autopilot_state = state.AutopilotState(feature="demo", project_root=tmp_path)
    event = state.ErrorEvent(
        timestamp="2026-03-27T00:00:00+00:00",
        phase="VERIFY",
        action="verify",
        category="verify",
        message="verify failed",
    )
    with caplog.at_level(logging.WARNING):
        autopilot_state._ingest_error_learning(event)

    assert "instinct error-ingestion failed" in caplog.text


def test_state_logs_collaboration_model_import_failure_and_uses_fallback_decision_model(
    caplog: object,
    monkeypatch: object,
) -> None:
    original_import = builtins.__import__

    def _blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "kodawari.autopilot.core.collaboration":
            raise RuntimeError("collaboration import boom")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)
    try:
        with caplog.at_level(logging.WARNING):
            reloaded_state = importlib.reload(state)
    finally:
        monkeypatch.setattr(builtins, "__import__", original_import)
        importlib.reload(state)

    decision = reloaded_state.ArchitectureDecision(
        decision_id="ADR-001",
        decision="Fallback boundary",
        rationale="Keep runtime status available",
    )

    assert decision.to_dict()["id"] == "ADR-001"
    assert "collaboration decision model unavailable; using fallback architecture decision" in caplog.text

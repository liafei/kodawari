"""TDD tests for P2 Phase 3A — Pipeline-as-Code configuration.

RED phase: all tests must FAIL before production code exists.
GREEN phase: implement production code until all pass.

Test groups:
  TestPipelineLoader        — YAML loading / parse errors
  TestMatchExpression       — match expression evaluation
  TestPresetResolution      — preset → behaviour mapping
  TestIntegration           — no-config fallback + CLI flag overrides
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pipeline_yaml(root: Path, content: str) -> Path:
    """Write .claude/workflow/workflow_pipeline.yaml and return path."""
    p = root / ".claude" / "workflow" / "workflow_pipeline.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_VALID_YAML = """\
    schema_version: "pipeline.v1"
    pipelines:
      docs_only:
        match: "all_files_match('docs/**', '*.md', 'README*')"
        preset: skip_review
        max_cycles: 2
      strict:
        match: "any_file_matches('**/auth_*.py', '**/credential_*.py')"
        preset: strict_review
        max_cycles: 10
      default:
        match: "true"
        preset: default
"""

# ---------------------------------------------------------------------------
# TestPipelineLoader
# ---------------------------------------------------------------------------


class TestPipelineLoader:
    """Tests for load_pipeline_config()."""

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        """No workflow_pipeline.yaml → returns None (backward compat)."""
        from kodawari.autopilot.pipeline_config import load_pipeline_config

        result = load_pipeline_config(tmp_path)
        assert result is None

    def test_load_valid_yaml_three_presets(self, tmp_path: Path) -> None:
        """Valid YAML with three pipelines → PipelineConfig with 3 entries."""
        from kodawari.autopilot.pipeline_config import load_pipeline_config, PipelineConfig

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        result = load_pipeline_config(tmp_path)
        assert result is not None
        assert isinstance(result, PipelineConfig)
        assert len(result.pipelines) == 3
        names = [p.name for p in result.pipelines]
        assert names == ["docs_only", "strict", "default"]

    def test_bad_yaml_returns_none(self, tmp_path: Path) -> None:
        """Malformed YAML → silently returns None (no exception propagation)."""
        from kodawari.autopilot.pipeline_config import load_pipeline_config

        bad = tmp_path / ".claude" / "workflow" / "workflow_pipeline.yaml"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("key: [unclosed bracket\n", encoding="utf-8")
        result = load_pipeline_config(tmp_path)
        assert result is None

    def test_load_populates_pipeline_entry_fields(self, tmp_path: Path) -> None:
        """PipelineEntry has name, match_expr, preset, max_cycles populated."""
        from kodawari.autopilot.pipeline_config import load_pipeline_config

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        cfg = load_pipeline_config(tmp_path)
        assert cfg is not None
        docs = cfg.pipelines[0]
        assert docs.name == "docs_only"
        assert "all_files_match" in docs.match_expr
        assert docs.preset == "skip_review"
        assert docs.max_cycles == 2

        strict = cfg.pipelines[1]
        assert strict.preset == "strict_review"
        assert strict.max_cycles == 10

        default = cfg.pipelines[2]
        assert default.preset == "default"
        # max_cycles may be None when not specified
        assert default.max_cycles is None or isinstance(default.max_cycles, int)


# ---------------------------------------------------------------------------
# TestMatchExpression
# ---------------------------------------------------------------------------


class TestMatchExpression:
    """Tests for resolve_pipeline() match logic."""

    def test_all_files_match_docs(self, tmp_path: Path) -> None:
        """all_files_match with docs globs → matches when all files are docs."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        entry = resolve_pipeline(tmp_path, ["docs/guide.md", "README.md"])
        assert entry is not None
        assert entry.preset == "skip_review"

    def test_all_files_match_fails_when_mixed(self, tmp_path: Path) -> None:
        """all_files_match fails when one file is not a doc."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        # docs/guide.md matches, src/auth_module.py does not → not all match
        # Should fall through to "strict" (auth file triggers any_file_matches)
        entry = resolve_pipeline(tmp_path, ["docs/guide.md", "src/auth_code.py"])
        # auth_code.py does not match auth_*.py exactly, so falls through to default
        assert entry is not None
        # Should NOT be docs_only since not ALL files are docs
        assert entry.preset != "skip_review"

    def test_any_file_matches_auth(self, tmp_path: Path) -> None:
        """any_file_matches with auth pattern → matches when any auth file present."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        entry = resolve_pipeline(tmp_path, ["src/auth_login.py", "tests/test_auth.py"])
        assert entry is not None
        assert entry.preset == "strict_review"

    def test_any_file_matches_credential(self, tmp_path: Path) -> None:
        """any_file_matches with credential pattern."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        entry = resolve_pipeline(tmp_path, ["services/credential_store.py"])
        assert entry is not None
        assert entry.preset == "strict_review"

    def test_true_matches_everything(self, tmp_path: Path) -> None:
        """match='true' always matches (used as default catch-all)."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        entry = resolve_pipeline(tmp_path, ["src/some_random_file.py"])
        assert entry is not None
        assert entry.preset == "default"

    def test_no_match_returns_none(self, tmp_path: Path) -> None:
        """When no pipeline matches, return None."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        # YAML with no default catch-all
        yaml_no_default = """\
            schema_version: "pipeline.v1"
            pipelines:
              strict:
                match: "any_file_matches('**/auth_*.py')"
                preset: strict_review
                max_cycles: 10
        """
        _write_pipeline_yaml(tmp_path, yaml_no_default)
        # File that doesn't match auth pattern
        entry = resolve_pipeline(tmp_path, ["src/models.py"])
        assert entry is None

    def test_empty_changed_files_resolves_default(self, tmp_path: Path) -> None:
        """Empty changed_files list still resolves 'true' pipeline."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        entry = resolve_pipeline(tmp_path, [])
        assert entry is not None
        assert entry.preset == "default"

    def test_first_match_wins(self, tmp_path: Path) -> None:
        """First matching pipeline is returned even if later ones also match."""
        from kodawari.autopilot.pipeline_config import resolve_pipeline

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        # README.md matches docs_only but also would match 'true' — docs_only is first
        entry = resolve_pipeline(tmp_path, ["README.md"])
        assert entry is not None
        assert entry.preset == "skip_review"


# ---------------------------------------------------------------------------
# TestPresetResolution
# ---------------------------------------------------------------------------


class TestPresetResolution:
    """Tests for preset behaviour wiring (engine integration)."""

    # --- helpers -----------------------------------------------------------

    def _make_engine(
        self,
        tmp_path: Path,
        *,
        changed_files: list[str] | None = None,
        adapter_override: Any = None,
    ) -> Any:
        """Build a minimal AutopilotEngine with task_card files pre-set."""
        from kodawari.autopilot.engine import AutopilotEngine
        from kodawari.autopilot.engine_support import AutopilotConfig

        cfg = AutopilotConfig(
            project_root=tmp_path,
            feature="test-feature",
            max_cycles=3,
        )
        engine = AutopilotEngine(config=cfg)
        if changed_files is not None:
            engine.state.changed_files = set(changed_files)
            engine._task_card_payload = {"files_to_change": list(changed_files)}
        if adapter_override is not None:
            engine.adapter = adapter_override
        return engine

    # --- test_docs_only_uses_skip_review -----------------------------------

    def test_docs_only_uses_skip_review(self, tmp_path: Path) -> None:
        """docs_only preset → run_single_pass_loop() is called."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(tmp_path, changed_files=["docs/guide.md"])

        with patch.object(esm, "run_single_pass_loop", return_value={"stopped": True, "reason": "PROCEED_TO_GATE"}) as mock_single, \
             patch.object(esm, "run_peer_review_loop", return_value={"stopped": True, "reason": "OK"}) as mock_peer:
            result = engine.run_collaboration_loop("test-task")
            assert mock_single.called
            assert not mock_peer.called
            assert result["reason"] == "PROCEED_TO_GATE"

    # --- test_strict_uses_dual_review --------------------------------------

    def test_strict_uses_dual_review(self, tmp_path: Path) -> None:
        """strict preset → run_peer_review_loop() called with loop_config_override."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(tmp_path, changed_files=["src/auth_login.py"])

        with patch.object(esm, "run_peer_review_loop", return_value={"stopped": True, "reason": "DONE"}) as mock_peer, \
             patch.object(esm, "run_single_pass_loop", return_value={"stopped": True}) as mock_single:
            result = engine.run_collaboration_loop("test-task")
            assert mock_peer.called
            assert not mock_single.called

    # --- test_default_uses_standard_flow -----------------------------------

    def test_default_uses_standard_flow(self, tmp_path: Path) -> None:
        """default preset → normal run_peer_review_loop() path."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(tmp_path, changed_files=["src/models.py"])

        with patch.object(esm, "run_peer_review_loop", return_value={"stopped": True, "reason": "OK"}) as mock_peer:
            result = engine.run_collaboration_loop("test-task")
            assert mock_peer.called

    # --- test_pipeline_options_do_not_leak_to_next_run --------------------

    def test_pipeline_options_do_not_leak_to_next_run(self, tmp_path: Path) -> None:
        """Engine config is unchanged after a pipeline run (no mutation)."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(tmp_path, changed_files=["src/auth_login.py"])

        original_enforce = engine.config.enforce_dual_review
        original_real_opus = engine.config.real_peer_review
        original_require = engine.config.require_real_peer_review

        with patch.object(esm, "run_peer_review_loop", return_value={"stopped": True}), \
             patch.object(esm, "run_single_pass_loop", return_value={"stopped": True}):
            engine.run_collaboration_loop("test-task")

        assert engine.config.enforce_dual_review == original_enforce
        assert engine.config.real_peer_review == original_real_opus
        assert engine.config.require_real_peer_review == original_require

    # --- test_docs_only_skips_verify_and_gate ------------------------------

    def test_docs_only_skips_verify_and_gate(self, tmp_path: Path) -> None:
        """docs_only: run_single_pass_loop called with actions_override skipping VERIFY/RULES_GATE."""
        import kodawari.autopilot.engine_session_mixin as esm
        from kodawari.autopilot.collaboration import CollaborationAction

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(tmp_path, changed_files=["docs/guide.md"])

        captured_kwargs: dict[str, Any] = {}

        def _capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
            captured_kwargs.update(kwargs)
            return {"stopped": True, "reason": "PROCEED_TO_GATE"}

        with patch.object(esm, "run_single_pass_loop", side_effect=_capture):
            engine.run_collaboration_loop("test-task")

        actions = captured_kwargs.get("actions_override")
        assert actions is not None, "actions_override must be passed for skip_review preset"
        action_values = [a if isinstance(a, str) else a.value for a in actions]
        assert CollaborationAction.VERIFY.value not in action_values, "VERIFY must be skipped"
        assert CollaborationAction.RULES_GATE.value not in action_values, "RULES_GATE must be skipped"
        # FINISH should be in the sequence
        assert CollaborationAction.FINISH.value in action_values, "FINISH must be in actions"

    # --- test_strict_review_enforced_in_review_mixin ----------------------

    def test_strict_review_enforced_in_review_mixin(self, tmp_path: Path) -> None:
        """strict preset: _review_evidence_enforced() returns True via runtime.config_override."""
        from kodawari.autopilot.engine_support import _LoopRuntime
        from kodawari.autopilot.collaboration import build_collaboration_context, build_peer_review_policy

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(tmp_path, changed_files=["src/auth_login.py"])

        # Build a runtime with config_override simulating strict_review
        ctx = build_collaboration_context("TEST", task_label="test", task_scope=None)
        runtime = _LoopRuntime(
            task_label="test",
            task_scope=None,
            task_id="TEST",
            context=ctx,
            peer_review_policy=build_peer_review_policy(max_rounds=3),
            pre_compact_payload={},
            config_override={"enforce_dual_review": True},
        )

        result = engine._review_evidence_enforced(runtime=runtime)
        assert result is True

    # --- test_strict_review_override_reaches_adapter ----------------------

    def test_strict_review_override_reaches_adapter(self, tmp_path: Path) -> None:
        """strict_review loop: adapter.override_review_config() is called with real_peer_review=True."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        mock_adapter = MagicMock()
        mock_adapter.override_review_config.return_value = {"real_peer_review": False, "require_real_peer_review": False}
        mock_adapter.restore_review_config.return_value = None

        engine = self._make_engine(tmp_path, changed_files=["src/auth_login.py"], adapter_override=mock_adapter)

        with patch.object(esm, "run_peer_review_loop", return_value={"stopped": True}):
            engine.run_collaboration_loop("test-task")

        # override_review_config must be called during strict preset
        mock_adapter.override_review_config.assert_called_once()
        call_kwargs = mock_adapter.override_review_config.call_args.kwargs
        assert call_kwargs.get("real_peer_review") is True

    # --- test_adapter_review_config_restored_after_loop -------------------

    def test_adapter_review_config_restored_after_loop(self, tmp_path: Path) -> None:
        """strict_review loop: adapter.restore_review_config() called with original values after loop."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        original_values = {"real_peer_review": False, "require_real_peer_review": False}
        mock_adapter = MagicMock()
        mock_adapter.override_review_config.return_value = original_values
        mock_adapter.restore_review_config.return_value = None

        engine = self._make_engine(tmp_path, changed_files=["src/auth_login.py"], adapter_override=mock_adapter)

        with patch.object(esm, "run_peer_review_loop", return_value={"stopped": True}):
            engine.run_collaboration_loop("test-task")

        # restore_review_config must be called with the original dict
        mock_adapter.restore_review_config.assert_called_once_with(original_values)


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------


class TestIntegration:
    """Integration tests: no-config fallback + CLI flag override."""

    def _make_engine(
        self,
        tmp_path: Path,
        *,
        changed_files: list[str] | None = None,
        enable_peer_review: bool = True,
    ) -> Any:
        from kodawari.autopilot.engine import AutopilotEngine
        from kodawari.autopilot.engine_support import AutopilotConfig

        cfg = AutopilotConfig(
            project_root=tmp_path,
            feature="test-feature",
            max_cycles=3,
        )
        engine = AutopilotEngine(config=cfg)
        if changed_files is not None:
            engine.state.changed_files = set(changed_files)
            engine._task_card_payload = {"files_to_change": list(changed_files)}
        return engine

    def test_no_config_fallback_to_default(self, tmp_path: Path) -> None:
        """No workflow_pipeline.yaml → existing behaviour preserved (run_peer_review_loop)."""
        import kodawari.autopilot.engine_session_mixin as esm

        # No YAML file written to tmp_path
        engine = self._make_engine(tmp_path, changed_files=["src/models.py"])

        with patch.object(esm, "run_peer_review_loop", return_value={"stopped": True, "reason": "OK"}) as mock_peer, \
             patch.object(esm, "run_single_pass_loop", return_value={"stopped": True}) as mock_single:
            result = engine.run_collaboration_loop("test-task", enable_peer_review=True)
            assert mock_peer.called
            assert not mock_single.called

    def test_cli_flags_override_pipeline_preset(self, tmp_path: Path) -> None:
        """enable_peer_review=False overrides pipeline preset (CLI flag wins)."""
        import kodawari.autopilot.engine_session_mixin as esm

        # docs_only pipeline would normally be chosen, but enable_peer_review=False
        # is a CLI-level override that takes precedence.
        # Note: docs_only would ALSO call run_single_pass_loop, so this test validates
        # that run_single_pass_loop is called regardless of which code path takes it.
        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(tmp_path, changed_files=["docs/guide.md"])

        with patch.object(esm, "run_single_pass_loop", return_value={"stopped": True, "reason": "X"}) as mock_single, \
             patch.object(esm, "run_peer_review_loop", return_value={"stopped": True}) as mock_peer:
            # enable_peer_review=False should invoke single_pass (either via pipeline or CLI flag)
            result = engine.run_collaboration_loop("test-task", enable_peer_review=False)
            assert mock_single.called

    def test_collaboration_action_finish_exists(self) -> None:
        """CollaborationAction.FINISH enum member exists with value 'finish'."""
        from kodawari.autopilot.collaboration import CollaborationAction

        assert hasattr(CollaborationAction, "FINISH")
        assert CollaborationAction.FINISH.value == "finish"

    def test_loop_runtime_config_override_field(self) -> None:
        """_LoopRuntime has config_override field defaulting to None."""
        from kodawari.autopilot.engine_support import _LoopRuntime
        from kodawari.autopilot.collaboration import build_collaboration_context, build_peer_review_policy

        ctx = build_collaboration_context("T", task_label="t", task_scope=None)
        runtime = _LoopRuntime(
            task_label="t",
            task_scope=None,
            task_id="T",
            context=ctx,
            peer_review_policy=build_peer_review_policy(max_rounds=3),
            pre_compact_payload={},
        )
        assert hasattr(runtime, "config_override")
        assert runtime.config_override is None

    def test_local_adapter_override_review_config_noop_when_no_attr(self, tmp_path: Path) -> None:
        """If adapter has no override_review_config, engine does not crash."""
        import kodawari.autopilot.engine_session_mixin as esm
        from kodawari.autopilot.engine import AutopilotEngine
        from kodawari.autopilot.engine_support import AutopilotConfig
        from kodawari.autopilot.pipeline_config import PipelineEntry

        cfg = AutopilotConfig(
            project_root=tmp_path,
            feature="test",
            max_cycles=3,
        )
        engine = AutopilotEngine(config=cfg)
        engine.state.changed_files = {"src/auth_login.py"}
        engine._task_card_payload = {"files_to_change": ["src/auth_login.py"]}

        # Replace adapter with a minimal object that has NO override_review_config
        class _MinimalAdapter:
            def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
                return {"status": "done", "changes": []}

        engine.adapter = _MinimalAdapter()

        # Simulate strict_review pipeline directly via patching resolve_pipeline
        strict_entry = PipelineEntry(
            name="strict",
            match_expr="any_file_matches('**/auth_*.py')",
            preset="strict_review",
            max_cycles=10,
        )
        with patch.object(esm, "resolve_pipeline", return_value=strict_entry), \
             patch.object(esm, "run_peer_review_loop", return_value={"stopped": True, "reason": "OK"}):
            # Must not raise even without override_review_config
            result = engine.run_collaboration_loop("test-task")
            assert result["stopped"] is True


# ---------------------------------------------------------------------------
# GPT peer-review fixes
# ---------------------------------------------------------------------------


class TestPipelineFixesFromReview:
    """Tests for GPT findings 3-5 — pipeline source, CLI priority, FINISH reason."""

    def _make_engine(
        self,
        tmp_path: Path,
        *,
        changed_files: list[str] | None = None,
        task_card: dict[str, Any] | None = None,
    ) -> Any:
        from kodawari.autopilot.engine import AutopilotEngine
        from kodawari.autopilot.engine_support import AutopilotConfig

        cfg = AutopilotConfig(project_root=tmp_path, feature="test-feat", max_cycles=3)
        engine = AutopilotEngine(config=cfg)
        if changed_files is not None:
            engine.state.changed_files = set(changed_files)
        if task_card is not None:
            engine._task_card_payload = task_card
        return engine

    # Fix 3: Pipeline should resolve from task_card files, not accumulated state
    def test_pipeline_resolves_from_task_card_not_state(self, tmp_path: Path) -> None:
        """First run: state.changed_files empty, task_card has docs → docs_only matches."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(
            tmp_path,
            changed_files=[],  # empty state — first run
            task_card={"files_to_change": ["docs/guide.md", "README.md"]},
        )

        with patch.object(esm, "run_single_pass_loop", return_value={"stopped": True, "reason": "PIPELINE_FINISH"}) as mock_single, \
             patch.object(esm, "run_peer_review_loop", return_value={"stopped": True}) as mock_peer:
            engine.run_collaboration_loop("task-1")
            assert mock_single.called, "docs_only pipeline should fire from task_card files"
            assert not mock_peer.called

    def test_multi_task_no_cross_contamination(self, tmp_path: Path) -> None:
        """Task 2 with docs files should not be affected by task 1's auth file state."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(
            tmp_path,
            changed_files=["src/auth_login.py"],  # stale from task 1
            task_card={"files_to_change": ["docs/usage.md"]},  # task 2 = docs
        )

        with patch.object(esm, "run_single_pass_loop", return_value={"stopped": True, "reason": "PIPELINE_FINISH"}) as mock_single, \
             patch.object(esm, "run_peer_review_loop", return_value={"stopped": True}) as mock_peer:
            engine.run_collaboration_loop("task-2")
            assert mock_single.called, "task_card docs should match docs_only, not stale auth state"
            assert not mock_peer.called

    # Fix 4: CLI enable_peer_review=False must override strict_review pipeline
    def test_cli_peer_review_false_overrides_strict_pipeline(self, tmp_path: Path) -> None:
        """enable_peer_review=False + strict_review match → still run_single_pass_loop."""
        import kodawari.autopilot.engine_session_mixin as esm

        _write_pipeline_yaml(tmp_path, _VALID_YAML)
        engine = self._make_engine(
            tmp_path,
            task_card={"files_to_change": ["src/auth_login.py"]},
        )

        with patch.object(esm, "run_single_pass_loop", return_value={"stopped": True, "reason": "OK"}) as mock_single, \
             patch.object(esm, "run_peer_review_loop", return_value={"stopped": True}) as mock_peer:
            engine.run_collaboration_loop("task-1", enable_peer_review=False)
            assert mock_single.called, "CLI enable_peer_review=False must take priority"
            assert not mock_peer.called, "strict_review must NOT override explicit CLI flag"

    # Fix 5: FINISH reason should be PIPELINE_FINISH, not PROCEED_TO_GATE
    def test_finish_reason_is_pipeline_finish(self, tmp_path: Path) -> None:
        """_run_finish_round should use reason='PIPELINE_FINISH'."""
        from kodawari.autopilot.engine_support import _LoopRuntime
        from kodawari.autopilot.collaboration import (
            CollaborationAction,
            build_collaboration_context,
            build_peer_review_policy,
            build_round_record,
        )

        engine = self._make_engine(tmp_path)
        ctx = build_collaboration_context("T", task_label="t", task_scope=None)
        runtime = _LoopRuntime(
            task_label="t", task_scope=None, task_id="T",
            context=ctx,
            peer_review_policy=build_peer_review_policy(max_rounds=3),
            pre_compact_payload={},
        )
        record = build_round_record(
            round_index=1, cycle=1, task_id="T", task_label="t",
            action=CollaborationAction.FINISH, actor=CollaborationAction.FINISH,
            context=ctx,
        )
        result = engine._run_finish_round(
            runtime=runtime, action=CollaborationAction.FINISH, round_record=record,
        )
        assert result["reason"] == "PIPELINE_FINISH", (
            f"Expected PIPELINE_FINISH, got {result.get('reason')}"
        )

    def test_single_pass_finish_action_uses_pipeline_finish_reason(self, tmp_path: Path) -> None:
        """run_single_pass_loop must dispatch FINISH (not short-circuit to PROCEED_TO_GATE)."""
        from kodawari.autopilot.collaboration import CollaborationAction
        from kodawari.autopilot.loop_runner import run_single_pass_loop

        engine = self._make_engine(tmp_path)
        result = run_single_pass_loop(
            engine,
            task_label="t",
            task_scope=None,
            actions_override=(CollaborationAction.FINISH,),
        )
        assert result["reason"] == "PIPELINE_FINISH", (
            f"Expected PIPELINE_FINISH, got {result.get('reason')}"
        )

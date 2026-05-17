"""Tests for autopilot rollback-on-failure (P1).

TDD protocol: written BEFORE implementation so all tests start RED.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_checkpoint(tmp_path: Path, *, cycle: int = 1):
    """Import and capture a checkpoint inside tmp_path."""
    from kodawari.autopilot.rollback import RollbackCheckpoint
    return RollbackCheckpoint.capture(
        project_root=tmp_path,
        target_files=[],
        cycle=cycle,
    )


# ===========================================================================
# TestRollbackCheckpoint
# ===========================================================================

class TestRollbackCheckpoint:

    # --- capture ---

    def test_capture_snapshots_existing_file(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint

        target = tmp_path / "foo.py"
        target.write_bytes(b"original content")

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["foo.py"],
            cycle=1,
        )

        assert "foo.py" in cp.snapshots
        snap = cp.snapshots["foo.py"]
        assert snap.existed is True
        assert snap.content == b"original content"

    def test_capture_records_nonexistent_file_as_not_existed(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["new_file.py"],
            cycle=2,
        )

        assert "new_file.py" in cp.snapshots
        snap = cp.snapshots["new_file.py"]
        assert snap.existed is False
        assert snap.content == b""

    # --- rollback: restore ---

    def test_rollback_restores_modified_file(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint

        target = tmp_path / "foo.py"
        target.write_bytes(b"original content")

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["foo.py"],
            cycle=1,
        )

        # Simulate implement: modify file
        target.write_bytes(b"modified content")

        result = cp.rollback(project_root=tmp_path, changed_files=["foo.py"])

        assert "foo.py" in result["reverted"]
        assert target.read_bytes() == b"original content"

    # --- rollback: remove newly created file ---

    def test_rollback_removes_new_file_created_during_implement(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["new_file.py"],
            cycle=1,
        )

        # Simulate implement: create file that didn't exist before
        new_file = tmp_path / "new_file.py"
        new_file.write_bytes(b"new content")

        result = cp.rollback(project_root=tmp_path, changed_files=["new_file.py"])

        assert "new_file.py" in result["removed"]
        assert not new_file.exists()

    # --- security: path containment ---

    def test_rollback_skips_path_outside_root(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint, FileSnapshot

        # Manually craft a checkpoint with an escape path in the snapshot
        cp = RollbackCheckpoint(
            cycle=1,
            pre_dirty_files=set(),
            snapshots={
                "../../etc/passwd": FileSnapshot(
                    path="../../etc/passwd", existed=True, content=b"original"
                )
            },
        )

        result = cp.rollback(project_root=tmp_path, changed_files=["../../etc/passwd"])

        assert "../../etc/passwd" in result["skipped"]
        assert result["reverted"] == []
        assert result["removed"] == []

    def test_capture_skips_path_outside_root(self, tmp_path: Path):
        """capture() itself must skip any target_file that escapes project_root."""
        from kodawari.autopilot.rollback import RollbackCheckpoint

        # Put an actual file one level above tmp_path
        parent = tmp_path.parent
        escape_target = "../escape.py"

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=[escape_target],
            cycle=1,
        )

        # The escape path must not appear in snapshots
        assert escape_target not in cp.snapshots

    # --- file not in snapshot → skipped ---

    def test_rollback_file_not_in_snapshot_goes_to_skipped(self, tmp_path: Path):
        """Files changed during implement but not snapshotted go to skipped."""
        from kodawari.autopilot.rollback import RollbackCheckpoint

        # Capture with no target files
        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=[],
            cycle=1,
        )

        # Create a file that was changed but not snapshotted
        extra = tmp_path / "extra.py"
        extra.write_bytes(b"extra content")

        result = cp.rollback(project_root=tmp_path, changed_files=["extra.py"])

        assert "extra.py" in result["skipped"]
        # Must NOT be reverted or removed — no git checkout
        assert "extra.py" not in result["reverted"]
        assert "extra.py" not in result["removed"]
        # File must still exist (we did NOT delete it)
        assert extra.exists()

    # --- git status detects unreported changes ---

    def test_rollback_uses_git_status_for_unreported_changes(self, tmp_path: Path):
        """Rollback discovers files changed by implement but not in changed_files list."""
        from kodawari.autopilot.rollback import RollbackCheckpoint

        unreported_file = "unreported.py"
        target = tmp_path / unreported_file
        target.write_bytes(b"unreported content")

        # Snapshot includes the unreported file
        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=[unreported_file],
            cycle=1,
        )
        cp.pre_dirty_files = set()  # ensure it wasn't dirty before

        # Mock git status to return the unreported file as dirty
        porcelain_output = f" M {unreported_file}\n"

        with patch("kodawari.autopilot.rollback.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.stdout = porcelain_output
            mock_proc.returncode = 0
            mock_run.return_value = mock_proc

            # Modify file to simulate implement change
            target.write_bytes(b"modified unreported")

            # changed_files does NOT include unreported_file
            result = cp.rollback(project_root=tmp_path, changed_files=[])

        # The file should have been discovered via git status and processed
        # (reverted or skipped depending on snapshot membership)
        all_handled = result["reverted"] + result["removed"] + result["skipped"]
        assert unreported_file in all_handled

    def test_capture_works_in_non_git_repo(self, tmp_path: Path):
        """capture() must not crash when git is unavailable (non-git dir or no git binary)."""
        from kodawari.autopilot.rollback import RollbackCheckpoint

        target = tmp_path / "foo.py"
        target.write_bytes(b"content")

        with patch(
            "kodawari.autopilot.rollback.subprocess.run",
            side_effect=OSError("git not found"),
        ):
            cp = RollbackCheckpoint.capture(
                project_root=tmp_path,
                target_files=["foo.py"],
                cycle=1,
            )

        # Should still snapshot the file; pre_dirty_files falls back to empty
        assert "foo.py" in cp.snapshots
        assert cp.pre_dirty_files == set()

    # --- extra_dirty_found counter ---

    def test_rollback_counts_extra_dirty_found(self, tmp_path: Path):
        """extra_dirty_found = number of files discovered via git beyond changed_files."""
        from kodawari.autopilot.rollback import RollbackCheckpoint

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["a.py", "b.py"],
            cycle=1,
        )
        cp.pre_dirty_files = set()  # nothing was dirty before

        # a.py: reported in changed_files (not extra)
        # b.py: discovered by git status only (extra)
        git_output = " M a.py\n M b.py\n"

        with patch("kodawari.autopilot.rollback.subprocess.run") as mock_run:
            mock_proc = MagicMock()
            mock_proc.stdout = git_output
            mock_proc.returncode = 0
            mock_run.return_value = mock_proc

            result = cp.rollback(project_root=tmp_path, changed_files=["a.py"])

        # b.py is extra_dirty (in git output but not in changed_files)
        assert result["extra_dirty_found"] >= 1

    # --- parent directory recreation ---

    def test_rollback_recreates_parent_directory(self, tmp_path: Path):
        """rollback() must recreate missing parent directories when restoring files."""
        from kodawari.autopilot.rollback import RollbackCheckpoint, FileSnapshot

        nested_rel = "subdir/nested.py"
        nested_full = tmp_path / "subdir" / "nested.py"
        nested_full.parent.mkdir(parents=True)
        nested_full.write_bytes(b"original")

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=[nested_rel],
            cycle=1,
        )

        # Simulate: parent dir was deleted during implement
        import shutil
        shutil.rmtree(str(nested_full.parent))
        assert not nested_full.parent.exists()

        result = cp.rollback(project_root=tmp_path, changed_files=[nested_rel])

        assert nested_rel in result["reverted"]
        assert nested_full.read_bytes() == b"original"

    # --- return dict structure ---

    def test_rollback_returns_correct_keys(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=[],
            cycle=3,
        )
        result = cp.rollback(project_root=tmp_path, changed_files=[])

        assert set(result.keys()) == {"reverted", "removed", "skipped", "cycle", "extra_dirty_found", "dirty_scan_available"}
        assert result["cycle"] == 3

    def test_rollback_non_git_does_not_crash(self, tmp_path: Path):
        """rollback() must not crash if git is unavailable during the dirty-files scan."""
        from kodawari.autopilot.rollback import RollbackCheckpoint

        target = tmp_path / "foo.py"
        target.write_bytes(b"original")

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["foo.py"],
            cycle=1,
        )
        cp.pre_dirty_files = set()

        target.write_bytes(b"modified")

        with patch(
            "kodawari.autopilot.rollback.subprocess.run",
            side_effect=OSError("no git"),
        ):
            result = cp.rollback(project_root=tmp_path, changed_files=["foo.py"])

        assert "foo.py" in result["reverted"]
        assert target.read_bytes() == b"original"
        assert result["dirty_scan_available"] is False

    def test_rollback_normalizes_windows_style_changed_paths(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint

        nested = tmp_path / "dir" / "file.py"
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b"original")

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["dir/file.py"],
            cycle=1,
        )
        nested.write_bytes(b"modified")

        result = cp.rollback(project_root=tmp_path, changed_files=["dir\\file.py"])

        assert "dir/file.py" in result["reverted"]
        assert nested.read_bytes() == b"original"

    def test_rollback_underreported_change_is_not_clean_when_dirty_scan_unavailable(self, tmp_path: Path):
        from kodawari.autopilot.rollback import RollbackCheckpoint
        from kodawari.autopilot.gate_round import _rollback_is_clean

        target = tmp_path / "foo.py"
        target.write_bytes(b"original")

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["foo.py"],
            cycle=1,
        )
        target.write_bytes(b"modified")

        with patch(
            "kodawari.autopilot.rollback.subprocess.run",
            side_effect=OSError("no git"),
        ):
            result = cp.rollback(project_root=tmp_path, changed_files=[])

        assert result["dirty_scan_available"] is False
        assert _rollback_is_clean(result) is False
        assert target.read_bytes() == b"modified"


# ===========================================================================
# TestRollbackConfig
# ===========================================================================

class TestRollbackConfig:

    def test_rollback_on_failure_default_is_false(self):
        """AutopilotConfig.rollback_on_failure must default to False."""
        from kodawari.autopilot.engine_support import AutopilotConfig
        from pathlib import Path

        config = AutopilotConfig(project_root=Path("."), feature="test")
        assert config.rollback_on_failure is False

    def test_rollback_on_failure_can_be_enabled(self):
        """AutopilotConfig.rollback_on_failure can be set to True."""
        from kodawari.autopilot.engine_support import AutopilotConfig
        from pathlib import Path

        config = AutopilotConfig(project_root=Path("."), feature="test", rollback_on_failure=True)
        assert config.rollback_on_failure is True

    def test_max_verify_retries_default(self):
        """AutopilotConfig.max_verify_retries must default to 2."""
        from kodawari.autopilot.engine_support import AutopilotConfig
        from pathlib import Path

        config = AutopilotConfig(project_root=Path("."), feature="test")
        assert config.max_verify_retries == 2

    def test_loop_runtime_has_rollback_checkpoint_field(self):
        """_LoopRuntime must have rollback_checkpoint defaulting to None."""
        from kodawari.autopilot.engine_support import _LoopRuntime
        from kodawari.autopilot.collaboration import build_collaboration_context

        ctx = build_collaboration_context(task_id="T001", task_label="test")
        runtime = _LoopRuntime(
            task_label="test",
            task_scope=None,
            task_id="T001",
            context=ctx,
            peer_review_policy={},
            pre_compact_payload={},
        )
        assert hasattr(runtime, "rollback_checkpoint")
        assert runtime.rollback_checkpoint is None


# ===========================================================================
# TestRollbackHelpers (gate_round helpers)
# ===========================================================================

class TestRollbackHelpers:

    def test_rollback_enabled_returns_false_when_config_false(self):
        from kodawari.autopilot.gate_round import _rollback_enabled

        engine = MagicMock()
        engine.config.rollback_on_failure = False
        assert _rollback_enabled(engine) is False

    def test_rollback_enabled_returns_true_when_config_true(self):
        from kodawari.autopilot.gate_round import _rollback_enabled

        engine = MagicMock()
        engine.config.rollback_on_failure = True
        assert _rollback_enabled(engine) is True

    def test_rollback_is_clean_true_when_no_skipped_and_no_extra_dirty(self):
        from kodawari.autopilot.gate_round import _rollback_is_clean

        result = {"skipped": [], "extra_dirty_found": 0, "reverted": ["a.py"], "removed": [], "cycle": 1}
        assert _rollback_is_clean(result) is True

    def test_rollback_is_clean_false_when_skipped_nonempty(self):
        from kodawari.autopilot.gate_round import _rollback_is_clean

        result = {"skipped": ["x.py"], "extra_dirty_found": 0, "reverted": [], "removed": [], "cycle": 1}
        assert _rollback_is_clean(result) is False

    def test_rollback_is_clean_false_when_extra_dirty(self):
        from kodawari.autopilot.gate_round import _rollback_is_clean

        result = {"skipped": [], "extra_dirty_found": 2, "reverted": [], "removed": [], "cycle": 1}
        assert _rollback_is_clean(result) is False

    def test_rollback_is_clean_false_when_none(self):
        from kodawari.autopilot.gate_round import _rollback_is_clean

        assert _rollback_is_clean(None) is False

    def test_rollback_is_clean_false_when_dirty_scan_unavailable(self):
        from kodawari.autopilot.gate_round import _rollback_is_clean

        result = {
            "skipped": [],
            "extra_dirty_found": 0,
            "reverted": ["a.py"],
            "removed": [],
            "cycle": 1,
            "dirty_scan_available": False,
        }
        assert _rollback_is_clean(result) is False

    def test_verify_retry_budget_remaining_true_when_no_failures(self):
        from kodawari.autopilot.gate_round import _verify_retry_budget_remaining

        engine = MagicMock()
        engine.config.max_verify_retries = 2

        runtime = MagicMock()
        runtime.round_records = []

        assert _verify_retry_budget_remaining(engine, runtime) is True

    def test_verify_retry_budget_remaining_false_when_exhausted(self):
        from kodawari.autopilot.gate_round import _verify_retry_budget_remaining

        engine = MagicMock()
        engine.config.max_verify_retries = 2

        runtime = MagicMock()
        runtime.round_records = [
            {"stage_status": "blocked", "details": {"verify_check": {}}},
            {"stage_status": "blocked", "details": {"verify_check": {}}},
        ]

        assert _verify_retry_budget_remaining(engine, runtime) is False

    def test_maybe_rollback_returns_none_when_no_checkpoint(self):
        from kodawari.autopilot.gate_round import _maybe_rollback

        engine = MagicMock()
        runtime = MagicMock(spec=[])  # no rollback_checkpoint attribute
        # getattr with default None
        result = _maybe_rollback(engine, runtime)
        assert result is None

    def test_maybe_rollback_calls_checkpoint_rollback(self, tmp_path: Path):
        from kodawari.autopilot.gate_round import _maybe_rollback
        from kodawari.autopilot.rollback import RollbackCheckpoint

        target = tmp_path / "foo.py"
        target.write_bytes(b"original")

        cp = RollbackCheckpoint.capture(
            project_root=tmp_path,
            target_files=["foo.py"],
            cycle=1,
        )
        target.write_bytes(b"modified")

        engine = MagicMock()
        engine.config.project_root = tmp_path
        engine.state.changed_files = {"foo.py"}

        runtime = MagicMock()
        runtime.rollback_checkpoint = cp
        runtime.last_changed_files = ["foo.py"]

        result = _maybe_rollback(engine, runtime)

        assert result is not None
        assert "reverted" in result
        assert "foo.py" in result["reverted"]
        # checkpoint is cleared after rollback
        assert runtime.rollback_checkpoint is None
        # last_changed_files cleared
        assert runtime.last_changed_files == []


# ===========================================================================
# TestCheckpointCaptureInEngine
# ===========================================================================

class TestCheckpointCaptureInEngine:
    """Integration-level: verify _run_codex_round stores checkpoint when enabled."""

    def _make_engine(self, tmp_path: Path, *, rollback_on_failure: bool):
        """Build a minimal mock engine that has _run_codex_round from mixin."""
        from kodawari.autopilot.engine_support import AutopilotConfig, _LoopRuntime
        from kodawari.autopilot.engine_implementation_mixin import EngineImplementationMixin
        from kodawari.autopilot.engine.engine_recovery_mixin import EngineRecoveryMixin
        from kodawari.autopilot.collaboration import (
            CollaborationAction,
            CollaborationContext,
        )

        class _FakeEngine(EngineRecoveryMixin, EngineImplementationMixin):
            def __init__(self):
                self.config = AutopilotConfig(
                    project_root=tmp_path,
                    feature="test",
                    rollback_on_failure=rollback_on_failure,
                    protected_files_check_enabled=False,
                )
                self.state = MagicMock()
                self.state.cycle = 1
                self.state.changed_files = set()
                self.state.current_stage = None
                self.state.last_stage_status = ""
                self.state.tokens_used = 0
                self.adapter = MagicMock()
                self.adapter.implement.return_value = {
                    "status": "done",
                    "changes": [],
                }
                self._task_card_payload = {}
                self._planning_dir = tmp_path / ".planning"
                self._planning_dir.mkdir(exist_ok=True)

            def _build_implementation_context(self, task_label, task_scope):
                return {"feature": "test", "task": task_label}

            def _task_card_files(self):
                return []

            def _maybe_emit_hook(self, *args, **kwargs):
                pass

            def _finish_loop(self, runtime, **kwargs):
                return {"status": "finished", **kwargs}

        engine = _FakeEngine()
        return engine

    def test_checkpoint_captured_when_rollback_enabled(self, tmp_path: Path):
        from kodawari.autopilot.engine_support import _LoopRuntime
        from kodawari.autopilot.collaboration import (
            CollaborationAction,
            build_collaboration_context,
        )

        engine = self._make_engine(tmp_path, rollback_on_failure=True)
        ctx = build_collaboration_context(task_id="T001", task_label="test task")
        runtime = _LoopRuntime(
            task_label="test task",
            task_scope=None,
            task_id="T001",
            context=ctx,
            peer_review_policy={},
            pre_compact_payload={"instinct_hints": []},
        )
        round_record: dict[str, Any] = {"round": 1}

        with patch("kodawari.autopilot.engine_implementation_mixin.snapshot_dirty_files", return_value=set()):
            engine._run_codex_round(
                runtime=runtime,
                action=CollaborationAction.CODEX_IMPLEMENT,
                round_record=round_record,
            )

        # checkpoint should have been stored on runtime
        assert runtime.rollback_checkpoint is not None

    def test_no_checkpoint_when_rollback_disabled(self, tmp_path: Path):
        from kodawari.autopilot.engine_support import _LoopRuntime
        from kodawari.autopilot.collaboration import (
            CollaborationAction,
            build_collaboration_context,
        )

        engine = self._make_engine(tmp_path, rollback_on_failure=False)
        ctx = build_collaboration_context(task_id="T001", task_label="test task")
        runtime = _LoopRuntime(
            task_label="test task",
            task_scope=None,
            task_id="T001",
            context=ctx,
            peer_review_policy={},
            pre_compact_payload={"instinct_hints": []},
        )
        round_record: dict[str, Any] = {"round": 1}

        with patch("kodawari.autopilot.engine_implementation_mixin.snapshot_dirty_files", return_value=set()):
            engine._run_codex_round(
                runtime=runtime,
                action=CollaborationAction.CODEX_IMPLEMENT,
                round_record=round_record,
            )

        # When disabled, no checkpoint should be stored
        assert getattr(runtime, "rollback_checkpoint", None) is None

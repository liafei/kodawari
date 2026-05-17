"""P0 trust-boundary tests for autopilot engine policy-file protection.

Five scenarios specified in docs/Harness吸收方案1.0.md §2.5a:

  1. test_protected_files_blocks_policy_yaml_change
  2. test_unreported_policy_change_detected_by_git_diff
  3. test_pre_dirty_policy_file_modified_detected_by_content_hash
  4. test_pre_dirty_non_policy_file_not_false_positive
  5. test_non_git_repo_falls_back_to_adapter_report_only

Trust model: the autopilot agent (Codex) must not be able to modify
.claude/workflow/*.yaml policy files during the implement stage.  Three
protection layers are required:
  Layer 1 – adapter-reported changed_files → _check_protected_files()
  Layer 2 – git-diff unreported files (git status --porcelain pre/post)
  Layer 3 – content-hash comparison of policy files pre/post implement

Human committers are NOT in scope for this protection (that is a CODEOWNERS
/ branch-protection concern).  Only autopilot-agent behaviour is guarded here.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from kodawari.autopilot.engine import AutopilotEngine
from kodawari.autopilot.engine_support import AutopilotConfig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLICY_REL = ".claude/workflow/gate_policy.yaml"
_POLICY_V1 = b"rules:\n  max_complexity: 6\n"
_POLICY_V2 = b"rules:\n  max_complexity: 999\n"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_policy_file(project_root: Path, content: bytes = _POLICY_V1) -> Path:
    """Create .claude/workflow/gate_policy.yaml and return its path."""
    p = project_root / ".claude" / "workflow" / "gate_policy.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _git_init(project_root: Path) -> None:
    """Initialise a git repo with author config (no-op on failure)."""
    try:
        subprocess.run(
            ["git", "init", "-q"],
            cwd=str(project_root),
            check=True,
            capture_output=True,
        )
        for key, val in [
            ("user.email", "test@example.com"),
            ("user.name", "Test"),
        ]:
            subprocess.run(
                ["git", "config", key, val],
                cwd=str(project_root),
                check=True,
                capture_output=True,
            )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


def _git_commit_all(project_root: Path) -> None:
    """Stage and commit everything so the working tree is clean."""
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(project_root),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial", "--allow-empty"],
            cwd=str(project_root),
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


def _engine(project_root: Path, adapter: Any) -> AutopilotEngine:
    config = AutopilotConfig(
        project_root=project_root,
        feature="newsapp",
        max_cycles=8,
    )
    return AutopilotEngine(config, adapter=adapter)


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class _AdapterReportsPolicyChange:
    """Adapter that honestly reports modifying the policy yaml (layer-1 trigger)."""

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task, context
        return {
            "status": "done",
            "changes": [_POLICY_REL, "src/feature.py"],
        }


class _AdapterUnreportedPolicyChange:
    """Writes the policy file to disk but omits it from reported changes.

    Simulates a rogue Codex agent that tries to bypass layer-1 protection
    by silently writing the policy file without advertising it.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task, context
        policy = self._root / ".claude" / "workflow" / "gate_policy.yaml"
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_bytes(_POLICY_V2)
        # Policy file is deliberately absent from reported changes
        return {
            "status": "done",
            "changes": ["src/feature.py"],
        }


class _AdapterModifiesPreDirtyPolicyFile:
    """Overwrites an already-dirty policy file during implement (layer-3 trigger).

    The file was modified before this round (pre-dirty), so git diff cannot
    see it as 'newly dirty'.  The content-hash layer must catch the change.
    """

    def __init__(self, project_root: Path) -> None:
        self._root = project_root

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task, context
        policy = self._root / ".claude" / "workflow" / "gate_policy.yaml"
        policy.write_bytes(_POLICY_V2)
        # Does not report the policy file
        return {
            "status": "done",
            "changes": ["src/feature.py"],
        }


class _AdapterReportsNonPolicyFile:
    """Adapter that only touches ordinary source files (no policy contact)."""

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/feature.py", "tests/test_feature.py"],
        }


class _AdapterSuccessNoPolicyTouch:
    """Adapter that succeeds without touching any policy files."""

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/feature.py"],
        }


# ---------------------------------------------------------------------------
# Test 1 – Layer 1: adapter-reported change is blocked
# ---------------------------------------------------------------------------


def test_protected_files_blocks_policy_yaml_change(tmp_path: Path) -> None:
    """Layer-1 guard: when the adapter honestly reports modifying
    .claude/workflow/gate_policy.yaml, the engine must block with
    reason=PROTECTED_FILE_BLOCK before any self-review or gate stage.
    """
    _make_policy_file(tmp_path)
    engine = _engine(tmp_path, _AdapterReportsPolicyChange())

    result = engine.run_collaboration_loop(
        task_label="T100: Add feature",
        task_scope="implement new endpoint",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROTECTED_FILE_BLOCK", (
        f"Expected PROTECTED_FILE_BLOCK but got {result['reason']!r}"
    )
    assert result["loop_outcome"]["blocked"] is True
    assert result["loop_outcome"]["stop_reason"] == "HARD_ERROR"
    implement_round = next(
        r for r in result["rounds"] if r["stage"] == "IMPLEMENT"
    )
    assert implement_round["stage_status"] == "blocked"
    # Error message must mention the blocked file
    assert _POLICY_REL in (implement_round.get("last_error") or "")


# ---------------------------------------------------------------------------
# Test 2 – Layer 2: unreported policy change detected via git diff
# ---------------------------------------------------------------------------


def test_unreported_policy_change_detected_by_git_diff(tmp_path: Path) -> None:
    """Layer-2 guard: policy file written to disk but NOT reported by adapter.

    The engine must run `git status --porcelain` before and after implement
    to discover the newly-dirty file and block with reason=PROTECTED_FILE_BLOCK.

    This test requires a real git repo so that the dirty-file snapshot works.
    """
    _git_init(tmp_path)
    _make_policy_file(tmp_path, _POLICY_V1)
    _git_commit_all(tmp_path)   # clean state: policy file is tracked + committed

    engine = _engine(tmp_path, _AdapterUnreportedPolicyChange(tmp_path))

    result = engine.run_collaboration_loop(
        task_label="T101: Silently tamper policy",
        task_scope="implement feature, unreported policy change",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROTECTED_FILE_BLOCK", (
        f"Expected PROTECTED_FILE_BLOCK (git-diff layer) but got {result['reason']!r}. "
        "The engine must detect unreported policy-file modifications via git status."
    )
    assert result["loop_outcome"]["blocked"] is True
    implement_round = next(
        r for r in result["rounds"] if r["stage"] == "IMPLEMENT"
    )
    assert implement_round["stage_status"] == "blocked"


# ---------------------------------------------------------------------------
# Test 3 – Layer 3: pre-dirty policy file modified again detected by hash
# ---------------------------------------------------------------------------


def test_pre_dirty_policy_file_modified_detected_by_content_hash(
    tmp_path: Path,
) -> None:
    """Layer-3 guard: policy file is already dirty before the implement round,
    then modified again during implement.

    A pure git-diff approach (post_dirty - pre_dirty) cannot catch this because
    the file was dirty both before and after.  The content-hash comparison
    (pre-hashes vs post-hashes) must catch the modification.

    The engine must return a clean structured result with reason=PROTECTED_FILE_BLOCK
    rather than propagating a raw PolicyHashViolation exception.
    """
    _git_init(tmp_path)
    _make_policy_file(tmp_path, _POLICY_V1)
    _git_commit_all(tmp_path)           # V1 committed, tree clean
    # Simulate pre-existing local edit (file is dirty before implement starts)
    policy_path = tmp_path / ".claude" / "workflow" / "gate_policy.yaml"
    policy_path.write_bytes(b"rules:\n  max_complexity: 8\n")  # V1.5

    engine = _engine(tmp_path, _AdapterModifiesPreDirtyPolicyFile(tmp_path))

    result = engine.run_collaboration_loop(
        task_label="T102: Quietly raise complexity limit",
        task_scope="implement feature while re-tampering pre-dirty policy file",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROTECTED_FILE_BLOCK", (
        f"Expected PROTECTED_FILE_BLOCK (content-hash layer) but got {result['reason']!r}. "
        "Engine must catch PolicyHashViolation and return a structured blocked result, "
        "not propagate an unhandled exception."
    )
    assert result["loop_outcome"]["blocked"] is True
    assert result["loop_outcome"]["stop_reason"] == "HARD_ERROR"


# ---------------------------------------------------------------------------
# Test 4 – No false positive: pre-dirty non-policy file must not be blocked
# ---------------------------------------------------------------------------


def test_pre_dirty_non_policy_file_not_false_positive(tmp_path: Path) -> None:
    """Trust-boundary guards must be scoped to .claude/workflow/*.yaml only.

    A source file (src/feature.py) that was already dirty before the round and
    is reported by the adapter must NOT trigger a block.  The run must succeed.
    """
    _git_init(tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    feature = src_dir / "feature.py"
    feature.write_text("# original\n", encoding="utf-8")
    _git_commit_all(tmp_path)
    # Make the source file pre-dirty (edit before implement round)
    feature.write_text("# pre-dirty modification\n", encoding="utf-8")

    engine = _engine(tmp_path, _AdapterReportsNonPolicyFile())

    result = engine.run_collaboration_loop(
        task_label="T103: Normal feature implementation",
        task_scope="src/feature.py and tests/test_feature.py only",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE", (
        f"Expected PROCEED_TO_GATE (no false positive) but got {result['reason']!r}"
    )
    assert result["loop_outcome"]["blocked"] is False


# ---------------------------------------------------------------------------
# Test 5 – Non-git repo falls back to adapter-report-only detection
# ---------------------------------------------------------------------------


def test_non_git_repo_falls_back_to_adapter_report_only(tmp_path: Path) -> None:
    """In a directory that is not a git repository the engine must not crash
    when the git-diff layer is unavailable.

    Layer 2 (git diff) must silently fall back to a no-op.
    Layer 1 (adapter-reported changes) and layer 3 (content hash) remain active.

    This test uses an adapter that touches only non-policy files so that neither
    layer 1 nor layer 3 triggers, and the run completes successfully.  The goal
    is to confirm the graceful fallback path does not raise an unexpected error.
    """
    # tmp_path is never git-init'd
    engine = _engine(tmp_path, _AdapterSuccessNoPolicyTouch())

    result = engine.run_collaboration_loop(
        task_label="T104: Feature in non-git project",
        task_scope="implement without touching policy files",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE", (
        f"Expected PROCEED_TO_GATE (graceful non-git fallback) but got {result['reason']!r}"
    )
    assert result["loop_outcome"]["blocked"] is False


# ---------------------------------------------------------------------------
# Test 6 – Policy hash block must NOT be bypassed via task label / scope text
# ---------------------------------------------------------------------------


def test_policy_file_blocked_even_when_task_mentions_it(tmp_path: Path) -> None:
    """Hash-detected policy violations must be a hard block, immune to is_authorized_to_modify.

    is_authorized_to_modify() returns True when the file path appears in
    task_label or task_scope.  For content-hash detected changes, the engine
    must block BEFORE consulting that function — no authorization escape hatch.
    """
    _make_policy_file(tmp_path)
    adapter = _AdapterModifiesPreDirtyPolicyFile(tmp_path)
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        max_cycles=8,
    )
    engine = AutopilotEngine(config, adapter=adapter)

    result = engine.run_collaboration_loop(
        # Explicitly mention the policy file path in both label and scope
        task_label=f"T200: update {_POLICY_REL} gate thresholds",
        task_scope=f"modify {_POLICY_REL} to relax constraints",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROTECTED_FILE_BLOCK", (
        f"Expected PROTECTED_FILE_BLOCK but got {result['reason']!r}. "
        "Hash-detected policy violations must not be bypassed via task label/scope."
    )
    assert result["loop_outcome"]["blocked"] is True

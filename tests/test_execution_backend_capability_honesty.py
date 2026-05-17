from __future__ import annotations

import pytest

from kodawari.autopilot import execution_backend


@pytest.mark.parametrize(
    "backend_name",
    [
        execution_backend.CLAUDE_CODE_BACKEND,
        execution_backend.CODEX_CLI_BACKEND,
    ],
)
def test_native_cli_backend_capabilities_are_honest(backend_name: str) -> None:
    descriptor = execution_backend.execution_backend_descriptor(backend_name)
    capabilities = execution_backend.execution_backend_capabilities(backend_name)
    truth = execution_backend.execution_backend_capability_truth(backend_name)

    assert capabilities == descriptor.capabilities()
    assert capabilities["implemented"] is True
    assert capabilities["executor_selectable"] is True
    assert capabilities["self_review_selectable"] is False
    assert capabilities["supports_agent_teams"] is False
    assert capabilities["supports_hooks"] is False
    assert capabilities["supports_memory"] is False
    assert capabilities["supports_deterministic_changed_files"] is True
    assert truth["implemented"]["descriptor_value"] is True
    assert truth["implemented"]["runtime_state"] == "verified"
    assert truth["supports_deterministic_changed_files"]["runtime_state"] == "verified"
    if backend_name == execution_backend.CLAUDE_CODE_BACKEND:
        assert capabilities["supports_worktree_isolation"] is True
        assert truth["supports_worktree_isolation"]["runtime_state"] == "verified"
        assert "directory-isolated execution workspace" in truth["supports_worktree_isolation"]["note"]
    else:
        # Hardening wave 2026-04-16: codex_cli isolation is now DEFAULT ON.
        # Descriptor reflects default_on; opt-out via WORKFLOW_CODEX_ISOLATION=0.
        assert capabilities["supports_worktree_isolation"] is True
        assert truth["supports_worktree_isolation"]["runtime_state"] == "default_on"
        assert "workflow_codex_isolation" in truth["supports_worktree_isolation"]["note"].lower()


def test_claude_code_truth_distinguishes_kernel_only_surfaces_from_native_capabilities() -> None:
    truth = execution_backend.execution_backend_capability_truth(execution_backend.CLAUDE_CODE_BACKEND)

    assert truth["supports_hooks"]["descriptor_value"] is False
    assert truth["supports_hooks"]["runtime_state"] == "kernel_only"
    assert "preflight guard" in truth["supports_hooks"]["note"]
    assert truth["supports_memory"]["descriptor_value"] is False
    assert truth["supports_memory"]["runtime_state"] == "kernel_only"
    assert "semantic_compact.json" in truth["supports_memory"]["note"]

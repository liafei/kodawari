"""Tests for declarative three-tier permission policy (Phase D)."""

from __future__ import annotations

import json
from pathlib import Path

from kodawari.autopilot.permission_policy import (
    PermissionTier,
    evaluate_permission,
    load_permission_policy,
)


def test_default_policy_loads() -> None:
    policy = load_permission_policy()
    assert policy["policy_name"] == "default"
    assert policy["schema_version"].startswith("autopilot.permission_policy")
    assert policy["rules"]  # non-empty


def test_read_is_allow_for_normal_paths() -> None:
    decision = evaluate_permission(tool="Read", path="any/path.py")
    assert decision.tier is PermissionTier.ALLOW
    assert "Read-only" in decision.reason


def test_grep_and_glob_are_allow() -> None:
    assert evaluate_permission(tool="Grep", path="x").tier is PermissionTier.ALLOW
    assert evaluate_permission(tool="Glob", path="x").tier is PermissionTier.ALLOW


def test_read_secret_paths_are_blocked() -> None:
    assert evaluate_permission(tool="Read", path=".env").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Read", path="secrets/id_rsa").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Grep", path="config/.env.local").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Glob", path="infra/.env.production").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Grep", path="keys/id_ed25519").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Glob", path="secrets/private.pem").tier is PermissionTier.BLOCK


def test_edit_env_is_block() -> None:
    decision = evaluate_permission(tool="Edit", path="backend/.env")
    assert decision.tier is PermissionTier.BLOCK
    assert "secret" in decision.reason.lower() or "env" in decision.reason.lower()


def test_edit_env_variant_is_block() -> None:
    assert evaluate_permission(tool="Edit", path=".env.local").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Edit", path="config/.env.production").tier is PermissionTier.BLOCK


def test_write_env_is_block() -> None:
    decision = evaluate_permission(tool="Write", path="repo/.env.production")
    assert decision.tier is PermissionTier.BLOCK


def test_edit_pem_key_is_block() -> None:
    assert evaluate_permission(tool="Edit", path="secrets/app.pem").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Edit", path="secrets/app.key").tier is PermissionTier.BLOCK


def test_edit_ssh_key_is_block() -> None:
    assert evaluate_permission(tool="Edit", path="~/.ssh/id_rsa").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Edit", path="/root/.ssh/id_ed25519").tier is PermissionTier.BLOCK


def test_edit_normal_py_is_prompt() -> None:
    decision = evaluate_permission(tool="Edit", path="src/kodawari/app.py")
    assert decision.tier is PermissionTier.PROMPT
    assert "scope" in decision.reason.lower()


def test_edit_tsx_is_prompt() -> None:
    decision = evaluate_permission(tool="Edit", path="mobile/www/app.tsx")
    assert decision.tier is PermissionTier.PROMPT


def test_write_any_is_prompt() -> None:
    decision = evaluate_permission(tool="Write", path="docs/new_file.md")
    assert decision.tier is PermissionTier.PROMPT


def test_unknown_tool_defaults_to_prompt() -> None:
    decision = evaluate_permission(tool="FancyNewTool", path="anything")
    assert decision.tier is PermissionTier.PROMPT
    assert "default" in decision.reason.lower() or "no matching" in decision.reason.lower()


def test_block_precedence_over_prompt(tmp_path: Path) -> None:
    policy_path = tmp_path / "custom.yaml"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "autopilot.permission_policy.v1",
                "policy_name": "custom",
                "prompt": [
                    {"tool": "Edit", "path_glob": "**/*.py", "reason": "py edit"},
                ],
                "block": [
                    {"tool": "Edit", "path_glob": "**/secret.py", "reason": "secret.py blocked"},
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = load_permission_policy(policy_path)
    decision = evaluate_permission(tool="Edit", path="src/secret.py", policy=policy)
    assert decision.tier is PermissionTier.BLOCK
    assert "secret.py" in decision.reason


def test_wildcard_tool_rule(tmp_path: Path) -> None:
    policy_path = tmp_path / "wildcard.yaml"
    policy_path.write_text(
        json.dumps(
            {
                "schema_version": "autopilot.permission_policy.v1",
                "policy_name": "wildcard",
                "block": [
                    {"tool": "*", "path_glob": "**/*.secret", "reason": ".secret files off limits"}
                ],
            }
        ),
        encoding="utf-8",
    )
    policy = load_permission_policy(policy_path)
    assert evaluate_permission(tool="Edit", path="a/b/c.secret", policy=policy).tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Write", path="a/b/c.secret", policy=policy).tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Bash", path="a/b/c.secret", policy=policy).tier is PermissionTier.BLOCK


def test_windows_backslash_paths_normalized() -> None:
    decision = evaluate_permission(tool="Edit", path=r"backend\api\.env")
    assert decision.tier is PermissionTier.BLOCK


def test_secret_path_matching_is_case_insensitive() -> None:
    assert evaluate_permission(tool="Edit", path=".ENV").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Write", path="SECRETS/ID_RSA").tier is PermissionTier.BLOCK


def test_empty_policy_defaults_to_prompt(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text(
        json.dumps({"schema_version": "autopilot.permission_policy.v1", "policy_name": "empty"}),
        encoding="utf-8",
    )
    policy = load_permission_policy(empty)
    decision = evaluate_permission(tool="Edit", path="any.py", policy=policy)
    assert decision.tier is PermissionTier.PROMPT


def test_missing_policy_file_returns_empty() -> None:
    missing = Path("/nonexistent/path/to/policy.yaml")
    policy = load_permission_policy(missing)
    assert policy["rules"] == []
    # and evaluate defaults to prompt
    decision = evaluate_permission(tool="Edit", path="any.py", policy=policy)
    assert decision.tier is PermissionTier.PROMPT


def test_write_pem_and_key_is_block() -> None:
    """Write tool must be blocked for *.pem and *.key — not just Edit."""
    assert evaluate_permission(tool="Write", path="certs/server.pem").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Write", path="ssl/private.key").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Write", path="deep/nested/app.pem").tier is PermissionTier.BLOCK
    assert evaluate_permission(tool="Write", path="tls.key").tier is PermissionTier.BLOCK


def test_new_secret_patterns_are_blocked() -> None:
    """Expanded secret patterns must be blocked for all tools."""
    secret_paths = [
        "certs/app.p12",
        "deploy/key.pfx",
        "java/keystore.jks",
        "conf/app.keystore",
        ".npmrc",
        "home/.pypirc",
        "gcp/credentials.json",
        "auth/token.json",
    ]
    for path in secret_paths:
        for tool in ("Read", "Grep", "Glob", "Edit", "Write"):
            decision = evaluate_permission(tool=tool, path=path)
            assert decision.tier is PermissionTier.BLOCK, (
                f"{tool} on {path} should be BLOCK, got {decision.tier}"
            )


class TestFindBlockedWrites:
    """Phase E: post-execution helper returning dict entries for blocked paths.

    Used by engine_implementation_mixin after codex finishes to catch
    BLOCK-tier writes in the non-isolation path (isolation mode already
    filters via execution_isolation.sync_isolated_workspace_to_project_root).
    """

    def test_empty_list_when_all_paths_allowed(self) -> None:
        from kodawari.autopilot.permission_policy import find_blocked_writes
        result = find_blocked_writes([
            "backend/main.py",
            "tests/test_foo.py",
            "docs/README.md",
        ])
        assert result == []

    def test_blocks_secret_file_writes(self) -> None:
        from kodawari.autopilot.permission_policy import find_blocked_writes
        result = find_blocked_writes([".env", "secrets/id_rsa", "config/app.pem"])
        paths = [r["path"] for r in result]
        assert ".env" in paths
        assert "secrets/id_rsa" in paths
        assert "config/app.pem" in paths
        # Each entry carries tool + reason + rule_path_glob
        for entry in result:
            assert entry["tool"] in ("Write", "Edit")
            assert entry["reason"]
            assert entry["rule_path_glob"]

    def test_mixed_allowed_and_blocked(self) -> None:
        from kodawari.autopilot.permission_policy import find_blocked_writes
        result = find_blocked_writes([
            "backend/main.py",       # allowed
            ".env.production",       # blocked
            "tests/test_bar.py",     # allowed
            "credentials.json",      # blocked
        ])
        paths = [r["path"] for r in result]
        assert ".env.production" in paths
        assert "credentials.json" in paths
        assert "backend/main.py" not in paths
        assert "tests/test_bar.py" not in paths

    def test_one_entry_per_path_even_if_multiple_tools_block(self) -> None:
        """If both Write and Edit block the same path, emit ONE entry per path."""
        from kodawari.autopilot.permission_policy import find_blocked_writes
        result = find_blocked_writes([".env"])
        assert len([r for r in result if r["path"] == ".env"]) == 1

    def test_empty_and_whitespace_paths_skipped(self) -> None:
        from kodawari.autopilot.permission_policy import find_blocked_writes
        assert find_blocked_writes(["", "   ", None]) == []  # type: ignore[list-item]

    def test_custom_policy_overrides_default(self) -> None:
        from kodawari.autopilot.permission_policy import find_blocked_writes
        # Empty custom policy → PROMPT default → no BLOCKs
        empty = {"rules": [], "policy_name": "custom"}
        assert find_blocked_writes([".env"], policy=empty) == []

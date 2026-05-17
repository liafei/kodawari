"""Tests for the shared execution isolation workspace helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kodawari.autopilot.execution_isolation import (
    prepare_isolation_workspace,
    sync_isolated_workspace_to_project_root,
)
from kodawari.autopilot import execution_claude_code, execution_codex_cli


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_prepare_isolation_workspace_copies_project_snapshot(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "def main():\n    return 1\n")
    _write(project_root / "README.md", "# repo")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="codex_cli",
        request_payload={"task_id": "T1"},
    )

    assert workspace.is_dir()
    assert workspace.parent.name == "codex_cli"
    assert "t1-" in workspace.name.lower()
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "def main():\n    return 1\n"
    assert (workspace / "README.md").read_text(encoding="utf-8") == "# repo"


def test_prepare_isolation_workspace_skips_planning_dir_and_git(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(planning_dir / "TASK_CARD_ACTIVE.json", "{}")
    (project_root / ".git").mkdir()
    _write(project_root / ".git" / "HEAD", "ref: refs/heads/main")
    _write(project_root / "app" / "main.py", "x")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="codex_cli",
        request_payload={"task_id": "T2"},
    )

    # planning dir is skipped (because it lives inside project_root)
    assert not (workspace / "planning").exists()
    # .git is skipped by default
    assert not (workspace / ".git").exists()
    # normal files propagate
    assert (workspace / "app" / "main.py").exists()


def test_sync_allowed_files_only(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "original\n")
    _write(project_root / "app" / "other.py", "untouched\n")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="codex_cli",
        request_payload={"task_id": "T3"},
    )
    # Simulate backend edits both files inside the workspace
    _write(workspace / "app" / "main.py", "modified\n")
    _write(workspace / "app" / "other.py", "also modified (OUT OF SCOPE)\n")

    sync_isolated_workspace_to_project_root(
        project_root=project_root,
        execution_root=workspace,
        allowed_files=["app/main.py"],
    )

    assert (project_root / "app" / "main.py").read_text(encoding="utf-8") == "modified\n"
    # Out-of-scope edit stays trapped in workspace
    assert (project_root / "app" / "other.py").read_text(encoding="utf-8") == "untouched\n"


def test_sync_propagates_deletion(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "original\n")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="codex_cli",
        request_payload={"task_id": "T4"},
    )
    # Simulate backend deleting the file inside the workspace
    (workspace / "app" / "main.py").unlink()

    sync_isolated_workspace_to_project_root(
        project_root=project_root,
        execution_root=workspace,
        allowed_files=["app/main.py"],
    )

    assert not (project_root / "app" / "main.py").exists()


def test_workspace_slug_is_sanitized(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "README.md", "x")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="codex_cli",
        request_payload={"task_id": "T5: Fancy/Name with spaces"},
    )

    # Only alphanumeric + dashes, lowercase, task hex suffix
    assert workspace.name.replace("-", "").replace("_", "").isalnum()


def test_prepare_isolation_workspace_skips_secret_files(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / ".env", "SECRET=1\n")
    _write(project_root / "keys" / "id_rsa", "private\n")
    _write(project_root / "app" / "main.py", "print('ok')\n")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="codex_cli",
        request_payload={"task_id": "T6"},
    )

    assert not (workspace / ".env").exists()
    assert not (workspace / "keys" / "id_rsa").exists()
    assert (workspace / "app" / "main.py").exists()


def test_sync_ignores_path_traversal_entries(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "safe\n")
    outside = tmp_path / "outside.txt"
    _write(outside, "outside\n")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="codex_cli",
        request_payload={"task_id": "T7"},
    )
    _write(workspace / "app" / "main.py", "changed\n")
    _write((workspace / ".." / ".." / "outside.txt").resolve(), "hacked\n")

    sync_isolated_workspace_to_project_root(
        project_root=project_root,
        execution_root=workspace,
        allowed_files=["../outside.txt", "app/main.py"],
    )

    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert (project_root / "app" / "main.py").read_text(encoding="utf-8") == "changed\n"


def test_copytree_does_not_follow_symlinks(tmp_path: Path) -> None:
    """Symlinks inside the project must be preserved as symlinks, not dereferenced."""
    if os.name == "nt":
        # Creating symlinks on Windows requires special privileges; skip if unavailable.
        try:
            test_link = tmp_path / "_test_link"
            test_link.symlink_to(tmp_path)
            test_link.unlink()
        except OSError:
            import pytest
            pytest.skip("symlink creation not permitted on this Windows host")

    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "ok\n")

    # Create a symlink pointing outside project_root
    outside_secret = tmp_path / "outside_secret.txt"
    _write(outside_secret, "TOP_SECRET\n")
    (project_root / "sneaky_link").symlink_to(outside_secret)

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="test",
        request_payload={"task_id": "T-symlink"},
    )

    link_in_workspace = workspace / "sneaky_link"
    if link_in_workspace.exists() or link_in_workspace.is_symlink():
        # If copied, it should still be a symlink, NOT the dereferenced content
        assert link_in_workspace.is_symlink(), (
            "symlink was followed and dereferenced into the isolation workspace"
        )
    # The real secret content must NOT be readable as a plain file in the workspace
    assert not (link_in_workspace.exists() and link_in_workspace.is_file() and not link_in_workspace.is_symlink())


def test_prepare_isolation_workspace_skips_top_level_symlink(tmp_path: Path) -> None:
    if os.name == "nt":
        try:
            test_link = tmp_path / "_test_top_level_link"
            test_link.symlink_to(tmp_path)
            test_link.unlink()
        except OSError:
            import pytest
            pytest.skip("symlink creation not permitted on this Windows host")

    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "ok\n")

    outside_payload = tmp_path / "outside_payload.txt"
    _write(outside_payload, "DO_NOT_COPY\n")
    (project_root / "top_symlink").symlink_to(outside_payload)

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="test",
        request_payload={"task_id": "T-top-link"},
    )

    assert not (workspace / "top_symlink").exists()


def test_prepare_isolation_workspace_skips_nested_symlink(tmp_path: Path) -> None:
    if os.name == "nt":
        try:
            test_link = tmp_path / "_test_nested_link"
            test_link.symlink_to(tmp_path)
            test_link.unlink()
        except OSError:
            import pytest
            pytest.skip("symlink creation not permitted on this Windows host")

    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "ok\n")

    outside_payload = tmp_path / "outside_secret.txt"
    _write(outside_payload, "DO_NOT_COPY\n")
    nested_dir = project_root / "app" / "links"
    nested_dir.mkdir(parents=True)
    (nested_dir / "outside_link").symlink_to(outside_payload)

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="test",
        request_payload={"task_id": "T-nested-link"},
    )

    assert not (workspace / "app" / "links" / "outside_link").exists()


def test_claude_code_planning_dir_rejects_outside_root(tmp_path: Path) -> None:
    """planning_dir outside project_root must fall back to default."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    outside = tmp_path / "evil_planning"
    outside.mkdir()
    payload = {"planning_dir": str(outside)}
    result = execution_claude_code._planning_dir(payload, project_root=project_root)  # type: ignore[attr-defined]
    assert result.is_relative_to(project_root.resolve()), (
        f"planning_dir {result} should be inside project_root {project_root}"
    )


def test_codex_cli_planning_dir_rejects_outside_root(tmp_path: Path) -> None:
    """planning_dir outside project_root must fall back to default."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    outside = tmp_path / "evil_planning"
    outside.mkdir()
    payload = {"planning_dir": str(outside)}
    result = execution_codex_cli._codex_planning_dir(payload, project_root=project_root)  # type: ignore[attr-defined]
    assert result.is_relative_to(project_root.resolve()), (
        f"planning_dir {result} should be inside project_root {project_root}"
    )


def test_codex_cli_guard_blocks_dangerous_command_override(tmp_path: Path) -> None:
    """codex_cli _run_codex_command must raise when override command matches deny rules."""
    from types import SimpleNamespace
    import pytest

    config = SimpleNamespace(
        command="git push --force origin main",
        executable="codex",
        timeout_seconds=60,
    )
    project_root = tmp_path / "repo"
    project_root.mkdir()
    request_path = project_root / "planning" / "req.json"
    request_path.parent.mkdir(parents=True)
    _write(request_path, "{}")

    with pytest.raises(execution_codex_cli.CodexCliPreflightGuardBlocked) as exc_info:
        execution_codex_cli._run_codex_command(  # type: ignore[attr-defined]
            config=config,
            request_payload={"project_root": str(project_root), "task": "test"},
            request_path=request_path,
        )
    assert exc_info.value.decision.action == "deny"


def test_codex_cli_guard_blocked_payload_includes_guard_decision(tmp_path: Path) -> None:
    from types import SimpleNamespace

    project_root = tmp_path / "repo"
    project_root.mkdir()
    request_path = project_root / "planning" / "request.json"
    request_path.parent.mkdir(parents=True)
    _write(request_path, "{}")

    result = execution_codex_cli.materialize_codex_cli_result(
        config=SimpleNamespace(
            command="git push --force origin main",
            executable="codex",
            timeout_seconds=60,
            isolation_workspace=False,
        ),
        request_path=request_path,
        request_payload={
            "feature": "demo",
            "task": "t1",
            "project_root": str(project_root),
            "files_to_change": [],
        },
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "EXECUTION_GUARD_DENY"
    assert result["guard_decision"]["action"] == "deny"
    assert result["guard_decision"]["policy"]


def test_sync_skips_permission_blocked_paths(tmp_path: Path) -> None:
    """Files blocked by permission policy must not propagate during sync-back."""
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "original\n")
    _write(project_root / ".env", "SECRET=old\n")

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="test",
        request_payload={"task_id": "T-perm"},
    )
    # .env is not copied to workspace (secret hygiene), but simulate the scenario
    # where a backend somehow creates a .env in the workspace
    _write(workspace / ".env", "SECRET=leaked\n")
    _write(workspace / "app" / "main.py", "changed\n")

    sync_isolated_workspace_to_project_root(
        project_root=project_root,
        execution_root=workspace,
        allowed_files=[".env", "app/main.py"],
    )

    # .env must NOT be synced back (permission-blocked)
    assert (project_root / ".env").read_text(encoding="utf-8") == "SECRET=old\n"
    # Normal file syncs fine
    assert (project_root / "app" / "main.py").read_text(encoding="utf-8") == "changed\n"


def test_codex_isolation_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """codex_cli isolation must be ON by default (env unset, no config flag)."""
    from types import SimpleNamespace
    monkeypatch.delenv("WORKFLOW_CODEX_ISOLATION", raising=False)
    config = SimpleNamespace()  # no isolation_workspace attribute
    assert execution_codex_cli._codex_isolation_enabled(config) is True  # type: ignore[attr-defined]


def test_codex_isolation_off_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """WORKFLOW_CODEX_ISOLATION=0 must disable isolation."""
    from types import SimpleNamespace
    monkeypatch.setenv("WORKFLOW_CODEX_ISOLATION", "0")
    config = SimpleNamespace()
    assert execution_codex_cli._codex_isolation_enabled(config) is False  # type: ignore[attr-defined]


def test_codex_isolation_off_via_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """isolation_workspace=False on config must disable isolation regardless of env."""
    from types import SimpleNamespace
    monkeypatch.setenv("WORKFLOW_CODEX_ISOLATION", "1")
    config = SimpleNamespace(isolation_workspace=False)
    assert execution_codex_cli._codex_isolation_enabled(config) is False  # type: ignore[attr-defined]


def test_codex_isolation_on_via_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """isolation_workspace=True must enable isolation even if env says off."""
    from types import SimpleNamespace
    monkeypatch.setenv("WORKFLOW_CODEX_ISOLATION", "0")
    config = SimpleNamespace(isolation_workspace=True)
    assert execution_codex_cli._codex_isolation_enabled(config) is True  # type: ignore[attr-defined]


def test_new_secret_patterns_not_copied(tmp_path: Path) -> None:
    """Expanded secret patterns (.p12, .npmrc, credentials.json, etc.) must be skipped."""
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True)
    _write(project_root / "app" / "main.py", "ok\n")
    _write(project_root / "certs" / "server.p12", "binary")
    _write(project_root / "deploy" / "key.pfx", "binary")
    _write(project_root / "java" / "app.jks", "binary")
    _write(project_root / ".npmrc", "//registry:_authToken=secret")
    _write(project_root / ".pypirc", "[pypi]\npassword=secret")
    _write(project_root / "credentials.json", '{"type":"service_account"}')
    _write(project_root / "token.json", '{"access_token":"x"}')

    workspace = prepare_isolation_workspace(
        planning_dir=planning_dir,
        project_root=project_root,
        backend_name="test",
        request_payload={"task_id": "T-secrets"},
    )

    assert (workspace / "app" / "main.py").exists()
    assert not (workspace / ".npmrc").exists()
    assert not (workspace / ".pypirc").exists()
    assert not (workspace / "credentials.json").exists()
    assert not (workspace / "token.json").exists()
    # Nested secret files should also be filtered by copytree_ignore
    assert not (workspace / "certs" / "server.p12").exists()
    assert not (workspace / "deploy" / "key.pfx").exists()
    assert not (workspace / "java" / "app.jks").exists()


def test_codex_isolation_request_file_written_to_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The isolation-aware request file must be written to the workspace.

    When isolation is enabled, materialize_codex_cli_result must write an
    updated .execution_request.json inside the workspace so the subprocess
    reads project_root = workspace_path (not original project_root).
    """
    import json
    from types import SimpleNamespace

    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("orig\n", encoding="utf-8")

    seen: dict = {}

    def _fake_run(command, **kwargs):
        # Read what file path is in the prompt (Request path: ...)
        import re
        prompt = kwargs.get("input", "")
        m = re.search(r"^Request path: (.+)$", prompt, re.MULTILINE)
        if m:
            req_path = m.group(1).strip()
            req = json.loads(open(req_path, encoding="utf-8").read())
            seen["project_root_in_request"] = req.get("project_root", "")
            # Simulate codex writing to the workspace project_root
            ws = open(req_path, encoding="utf-8")  # noqa: just for path
            ws.close()
            ws_root = req.get("project_root", "")
            target = (Path(ws_root) / "app" / "main.py")
            if target.exists():
                target.write_text("modified\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("WORKFLOW_CODEX_ISOLATION", "1")
    monkeypatch.setattr(execution_codex_cli.shutil, "which", lambda name: "codex" if name == "codex" else None)
    monkeypatch.setattr(execution_codex_cli.subprocess, "run", _fake_run)

    result = execution_codex_cli.materialize_codex_cli_result(
        config=SimpleNamespace(
            command="",
            executable="codex",
            timeout_seconds=60,
            isolation_workspace=True,
        ),
        request_path=planning_dir / ".execution_request.json",
        request_payload={
            "project_root": str(project_root),
            "feature": "demo",
            "task": "test",
            "task_id": "T-req",
            "files_to_change": ["app/main.py"],
        },
    )

    # The request file the subprocess sees must have project_root = workspace, not original
    assert "project_root_in_request" in seen
    ws_root = seen["project_root_in_request"]
    assert ws_root != str(project_root.resolve()), (
        "subprocess must receive workspace project_root, not original"
    )
    # The request_path field inside the written file must point to the workspace
    # copy, not the original planning dir.  A stale request_path causes the
    # subprocess to read the original file (old project_root) on a second pass.
    ws_request_file = Path(ws_root) / ".execution_request.json"
    import json as _json
    req_on_disk = _json.loads(ws_request_file.read_text(encoding="utf-8"))
    assert req_on_disk.get("request_path") == str(ws_request_file), (
        f"request_path in workspace file must point to workspace, got {req_on_disk.get('request_path')!r}"
    )
    # The result should be PASS because we wrote to the workspace path
    assert result["status"] == "PASS", f"expected PASS, got: {result}"
    # The source file in project_root must have been synced from workspace
    assert source_file.read_text(encoding="utf-8") == "modified\n"


def test_codex_isolation_subprocess_env_uses_workspace_runtime_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex_cli must point HOME/CODEX_HOME to writable workspace-local paths."""
    import json
    from types import SimpleNamespace

    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    source_file = project_root / "app" / "main.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("orig\n", encoding="utf-8")

    seen: dict[str, str] = {}

    def _fake_run(command, **kwargs):
        del command
        import re

        prompt = str(kwargs.get("input") or "")
        match = re.search(r"^Request path: (.+)$", prompt, re.MULTILINE)
        assert match is not None, "prompt must include Request path for workspace handoff"
        request_file = Path(match.group(1).strip()).resolve()
        request_payload = json.loads(request_file.read_text(encoding="utf-8"))
        workspace_root = Path(str(request_payload.get("project_root") or "")).resolve()
        env = dict(kwargs.get("env") or {})
        seen["home"] = str(env.get("HOME") or "")
        seen["userprofile"] = str(env.get("USERPROFILE") or "")
        seen["codex_home"] = str(env.get("CODEX_HOME") or "")

        assert seen["home"], "HOME must be set for codex child process"
        assert seen["codex_home"], "CODEX_HOME must be set for codex child process"
        assert Path(seen["home"]).resolve().is_relative_to(workspace_root)
        assert Path(seen["codex_home"]).resolve().is_relative_to(workspace_root)
        if seen["userprofile"]:
            assert Path(seen["userprofile"]).resolve().is_relative_to(workspace_root)

        target = workspace_root / "app" / "main.py"
        target.write_text("modified\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setenv("WORKFLOW_CODEX_ISOLATION", "1")
    monkeypatch.setattr(execution_codex_cli.shutil, "which", lambda name: "codex" if name == "codex" else None)
    monkeypatch.setattr(execution_codex_cli.subprocess, "run", _fake_run)

    result = execution_codex_cli.materialize_codex_cli_result(
        config=SimpleNamespace(
            command="",
            executable="codex",
            timeout_seconds=60,
            isolation_workspace=True,
        ),
        request_path=planning_dir / ".execution_request.json",
        request_payload={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "feature": "demo",
            "task": "test",
            "task_id": "T-env",
            "files_to_change": ["app/main.py"],
        },
    )

    assert result["status"] == "PASS", result
    assert source_file.read_text(encoding="utf-8") == "modified\n"

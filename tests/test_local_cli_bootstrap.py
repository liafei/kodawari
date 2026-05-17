from pathlib import Path


def test_repo_local_bootstrap_script_exists_and_declares_venv_install() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    bootstrap = repo_root / "scripts" / "bootstrap_kodawari.ps1"
    assert bootstrap.exists()
    text = bootstrap.read_text(encoding="utf-8")
    assert ".workflow_runtime" in text
    assert ".venv" in text
    assert "-m venv" in text
    assert "pip install" in text
    assert "kodawari.exe" in text
    assert "Get-Command" in text
    assert "WARNING: current shell 'kodawari' resolves to" in text
    assert ".\\scripts\\kodawari.ps1" in text
    assert "kodawari telemetry --help" in text


def test_repo_docs_mention_repo_local_canonical_entry() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    entry_doc = (repo_root / "项目说明.md").read_text(encoding="utf-8")
    overview_doc = (repo_root / "docs" / "architecture" / "一、平台现状、架构与兼容总览.md").read_text(encoding="utf-8")
    ops_doc = (repo_root / "docs" / "operations" / "二、运行操作、门禁规则与后续路线.md").read_text(encoding="utf-8")

    assert "bootstrap_kodawari.ps1" in ops_doc
    assert ".\\scripts\\kodawari.ps1 gate --help" in ops_doc
    assert ".\\scripts\\kodawari.ps1 telemetry --help" in ops_doc
    assert ".\\.workflow_runtime\\local-env\\.venv\\Scripts\\kodawari.exe gate --help" in ops_doc
    assert "scripts\\kodawari.ps1" in entry_doc
    assert "kodawari compact" in overview_doc
    assert "kodawari quick-develop" in overview_doc
    assert "兼容命令族" in overview_doc


def test_repo_wrapper_sets_repo_local_context_for_cli_fallback() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = repo_root / "scripts" / "kodawari.ps1"
    text = wrapper.read_text(encoding="utf-8")
    assert "WORKFLOWCTL_REPO_ROOT" in text
    assert "WORKFLOWCTL_WRAPPER" in text
    assert "WORKFLOWCTL_INVOCATION_CWD" in text
    assert "WORKFLOWCTL_CANONICAL_WRAPPER" in text
    assert '$env:PYTHONPATH = $srcPath' in text
    assert '& $venvWorkflowctl @Args' in text
    assert "kodawari.cli.main" in text


def test_repo_wrapper_preserves_invocation_cwd_for_target_project_runs() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    wrapper = repo_root / "scripts" / "kodawari.ps1"
    text = wrapper.read_text(encoding="utf-8")
    assert "WORKFLOWCTL_INVOCATION_CWD" in text
    assert "Push-Location $repoRoot" not in text
    assert 'powershell -ExecutionPolicy Bypass -File `"$bootstrapScript`"' in text


def test_cli_main_warns_when_repo_resolution_looks_mismatched() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    main_py = (repo_root / "src" / "kodawari" / "cli" / "main.py").read_text(encoding="utf-8")
    assert "_warn_if_repo_resolution_mismatch" in main_py
    assert "loaded CLI code is from" in main_py


def test_pyproject_declares_observability_runtime_dependency() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    assert "jsonschema>=" in pyproject

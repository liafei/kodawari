"""Tests for local adapter bridge behavior."""

import json
from pathlib import Path
import textwrap
import warnings

import pytest

from kodawari.autopilot.execution import local_adapter_recovery as recovery_helpers
from kodawari.autopilot.execution.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig


def _write_models_yaml(root: Path, content: str) -> None:
    path = root / ".claude" / "workflow" / "models.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")


def test_local_adapter_simulate_mode_implement_and_review() -> None:
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))
    ok, msg = adapter.check_health()
    assert ok is True
    assert "simulate" in msg

    impl = adapter.implement(
        task="T001",
        context={
            "pattern_hints": [{"pattern_id": "ranking-rules"}],
            "attempt": 1,
        },
    )
    assert impl["status"] == "done"
    assert any("test" in item for item in impl["changes"])

    review = adapter.review(
        task="T001",
        context={},
        changed_files=list(impl["changes"]),
        review_iteration=0,
    )
    assert review["approved"] is True
    assert review["review_runtime"]["mode"] == "simulate_local"
    assert review["review_runtime"]["real_requested"] is False
    assert review["review_runtime"]["fallback_used"] is False


def test_local_adapter_simulated_review_approves_verification_only_noop() -> None:
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    review = adapter.review(
        task="TVERIFY",
        context={
            "execution_constraints": {
                "verification_only_noop": True,
                "executor_must_not_edit": True,
            },
            "verify_cmd": "python -m pytest tests/test_existing.py -q",
            "task_card": {
                "files_to_change": [],
                "verify_cmd": "python -m pytest tests/test_existing.py -q",
                "execution_constraints": {
                    "verification_only_noop": True,
                    "executor_must_not_edit": True,
                },
            },
        },
        changed_files=[],
        review_iteration=0,
    )

    assert review["approved"] is True
    assert "Verification-only" in review["summary"]


def test_local_adapter_can_simulate_failure() -> None:
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))
    result = adapter.implement(
        task="T002",
        context={
            "simulate_failure": True,
            "simulate_failure_message": "fixture setup failed",
        },
    )
    assert result["status"] == "error"
    assert "fixture" in result["error"]


def test_local_adapter_uses_models_yaml_defaults(tmp_path: Path) -> None:
    _write_models_yaml(
        tmp_path,
        """
        schema_version: "models.v1"
        executor_model: gpt-5.4
        reviewer_model: gpt-5.4
        reviewer_backend: codex
        review_enabled: true
        """,
    )
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True, cwd=tmp_path))
    assert adapter.config.executor_model == "gpt-5.4"
    assert adapter.config.reviewer_model == "gpt-5.4"
    assert adapter.config.reviewer_backend == "codex"
    assert adapter.config.real_peer_review is True


def test_local_adapter_uses_models_v2_role_defaults(tmp_path: Path) -> None:
    _write_models_yaml(
        tmp_path,
        """
        schema_version: "models.v2"
        transports:
          codex_local:
            kind: subprocess
            driver: codex_cli
            interface: agent
            executable: codex
            provides: [repo.read_file, repo.grep, repo.glob, repo.write_file]
          claude_mcp_review:
            kind: subprocess
            driver: claude_cli
            interface: mcp
            host_executable: claude
            mcp_server: kodawari.autopilot.review.mcp_review_server
            provides: [repo.read_file, repo.grep, repo.glob]
          self_review_noop:
            kind: in_process
            driver: noop_test_only
            interface: noop_test_only
            provides: []
        compatibility:
          - {models: [gpt-5.5], transports: [codex_local], interfaces: [agent]}
          - {models: [claude-opus-4-7], transports: [claude_mcp_review], interfaces: [mcp]}
          - {models: [noop], transports: [self_review_noop], interfaces: [noop_test_only]}
        roles:
          executor:
            transport: codex_local
            model: gpt-5.5
            scope_mode: post_diff
            on_unavailable: fail
          self_reviewer:
            transport: self_review_noop
            model: noop
            on_unavailable: degrade_to_simulate
          impl_reviewer:
            transport: claude_mcp_review
            model: claude-opus-4-7
            on_unavailable: fail
        """,
    )

    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True, cwd=tmp_path))

    assert adapter.config.executor_backend == "codex_cli"
    assert adapter.config.executor_model == "gpt-5.5"
    assert adapter.config.self_review_backend == "noop_test_only"
    assert adapter.config.reviewer_backend == "mcp"
    assert adapter.config.reviewer_model == "claude-opus-4-7"
    assert adapter.config.real_peer_review is True
    assert adapter.config.require_real_peer_review is True


def test_local_adapter_env_overrides_models_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_models_yaml(
        tmp_path,
        """
        schema_version: "models.v1"
        executor_model: model-executor
        reviewer_model: model-reviewer
        reviewer_backend: codex
        review_enabled: true
        """,
    )
    monkeypatch.setenv("WORKFLOW_EXECUTOR_MODEL", "env-executor")
    monkeypatch.setenv("WORKFLOW_REVIEWER_MODEL", "env-reviewer")
    monkeypatch.setenv("WORKFLOW_REVIEWER_BACKEND", "cli")
    monkeypatch.setenv("WORKFLOW_REVIEW_ENABLED", "0")
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True, cwd=tmp_path))
    assert adapter.config.executor_model == "env-executor"
    assert adapter.config.reviewer_model == "env-reviewer"
    assert adapter.config.reviewer_backend == "cli"
    assert adapter.config.real_peer_review is False


def test_local_adapter_warns_when_using_legacy_reviewer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFLOW_REVIEWER_API_KEY", raising=False)
    monkeypatch.setenv("WORKFLOW_OPUS_API_KEY", "legacy-key")

    with pytest.warns(DeprecationWarning, match="WORKFLOW_OPUS_API_KEY"):
        adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    assert adapter.config.reviewer_api_key == "legacy-key"


def test_local_adapter_prefers_new_reviewer_env_without_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_REVIEWER_API_KEY", "new-key")
    monkeypatch.setenv("WORKFLOW_OPUS_API_KEY", "legacy-key")

    with warnings.catch_warnings(record=True) as warnings_record:
        warnings.simplefilter("always")
        adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    assert adapter.config.reviewer_api_key == "new-key"
    assert not [item for item in warnings_record if issubclass(item.category, DeprecationWarning)]


def test_local_adapter_requires_real_peer_review_when_enabled() -> None:
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            require_real_peer_review=True,
        )
    )
    review = adapter.review(
        task="T003",
        context={},
        changed_files=["src/app.py", "tests/test_app.py"],
        review_iteration=0,
    )
    assert review["approved"] is False
    assert "Real peer review required" in review["summary"]
    assert review["source"] == "kodawari.real_peer_review_required"
    assert review["review_runtime"]["mode"] == "real_required_failed"
    assert review["review_runtime"]["real_requested"] is True
    assert review["review_runtime"]["real_required"] is True
    assert review["review_runtime"]["fallback_used"] is False
    assert "WORKFLOW_OPUS_GATEWAY" in review["review_runtime"]["error"]["message"]


def test_local_adapter_preflight_blocks_when_real_peer_review_is_unavailable() -> None:
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            require_real_peer_review=True,
        )
    )
    preflight = adapter.peer_review_preflight(task="T003", context={})

    assert preflight["ready"] is False
    assert "WORKFLOW_OPUS_GATEWAY" in preflight["blocking_error"]
    assert preflight["review"]["review_runtime"]["mode"] == "real_required_failed"


def test_local_adapter_can_use_real_peer_review(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return (
            {
                "approved": True,
                "summary": "Real opus review approved.",
                "must_fix": [],
                "should_fix": [],
                "blocking_items": [],
                "severity": "low",
                "score": 99,
                "target_score": 95,
                "min_dimension_score": 80,
                "gate_recommendation": "PROCEED_TO_GATE",
                "reviewer": "opus",
                "source": "kodawari.real_peer_review_gateway",
                "global_consistency_verdict": "PASS",
                "local_implementation_verdict": "PASS",
                "deterministic_finding_responses": [
                    {
                        "finding_type": "missing_test_files",
                        "acknowledged": True,
                        "assessment": "No issues found in this run.",
                    }
                ],
                "evidence_refs": [
                    {
                        "artifact": ".review_bundle.json",
                        "field_path": "contract_excerpt.architecture_plan",
                        "reason": "Architecture contract checked",
                    }
                ],
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_opus_review", _fake_request)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            reviewer_base_url="https://example.test",
            reviewer_api_key="test-key",
        )
    )
    review = adapter.review(
        task="T004",
        context={"task_id": "T004"},
        changed_files=["src/app.py", "tests/test_app.py"],
        review_iteration=1,
    )
    assert review["approved"] is True
    assert review["source"] == "kodawari.real_peer_review_gateway"
    assert review["review_runtime"]["mode"] == "real_peer_review_gateway"
    assert review["review_runtime"]["real_requested"] is True
    assert review["review_runtime"]["real_required"] is False
    assert review["review_runtime"]["fallback_used"] is False
    assert review["global_consistency_verdict"] == "PASS"
    assert review["local_implementation_verdict"] == "PASS"
    assert review["deterministic_finding_responses"][0]["finding_type"] == "missing_test_files"
    assert review["evidence_refs"][0]["artifact"] == ".review_bundle.json"


def test_local_adapter_marks_simulate_fallback_when_real_gateway_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return None, "http 401"

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_opus_review", _fake_request)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            reviewer_base_url="https://example.test",
            reviewer_api_key="bad-key",
        )
    )
    review = adapter.review(
        task="T005",
        context={"task_id": "T005"},
        changed_files=["src/app.py", "tests/test_app.py"],
        review_iteration=1,
    )

    assert review["approved"] is True
    assert review["review_runtime"]["mode"] == "simulate_local"
    assert review["review_runtime"]["real_requested"] is True
    assert review["review_runtime"]["real_required"] is False
    assert review["review_runtime"]["fallback_used"] is True
    assert review["review_runtime"]["error"]["message"] == "http 401"


def test_local_adapter_auto_enables_real_review_when_api_key_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKFLOW_REVIEWER_API_KEY", "test-key")
    monkeypatch.delenv("WORKFLOW_REVIEW_ENABLED", raising=False)

    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    assert adapter.config.real_peer_review is True
    preflight = adapter.peer_review_preflight(task="T006", context={})
    assert preflight["ready"] is True
    assert preflight["reviewer_doctor_degraded"] is True
    assert "WORKFLOW_OPUS_GATEWAY" in preflight["degraded_reason"]


def test_local_adapter_preflight_writes_reviewer_health_artifact(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            cwd=project_root,
            real_peer_review=True,
            require_real_peer_review=True,
            reviewer_backend="api",
            reviewer_api_key="",
            reviewer_base_url="",
        )
    )

    preflight = adapter.peer_review_preflight(
        task="T-health",
        context={
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
        },
    )

    assert preflight["ready"] is False
    artifact = planning_dir / "reviewer_health.json"
    assert artifact.exists() is True
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["backend"] == "api"
    assert payload["available"] is False


def test_local_adapter_models_yaml_review_enabled_false_disables_api_key_auto_enable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_models_yaml(
        tmp_path,
        """
        schema_version: "models.v1"
        review_enabled: false
        """,
    )
    monkeypatch.setenv("WORKFLOW_REVIEWER_API_KEY", "test-key")
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True, cwd=tmp_path))
    assert adapter.config.real_peer_review is False


def test_local_adapter_does_not_auto_enable_real_review_when_env_explicitly_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKFLOW_REVIEWER_API_KEY", "test-key")
    monkeypatch.setenv("WORKFLOW_REVIEW_ENABLED", "0")

    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    assert adapter.config.real_peer_review is False
    preflight = adapter.peer_review_preflight(task="T007", context={})
    assert preflight["ready"] is True


def test_preflight_codex_incompatible_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """When backend=codex but executable is claude, error says 'not a codex binary'."""
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_BACKEND", "codex")
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_EXECUTABLE", "claude")
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(simulate=True, require_real_peer_review=True)
    )
    preflight = adapter.peer_review_preflight(task="T-codex-incompat", context={})
    assert preflight["ready"] is False
    assert "not a codex binary" in preflight["blocking_error"]
    assert "'claude'" in preflight["blocking_error"]


def test_preflight_codex_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When backend=codex and executable is codex but not on PATH, error says 'not found'."""
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_BACKEND", "codex")
    monkeypatch.delenv("WORKFLOW_OPUS_REVIEWER_EXECUTABLE", raising=False)
    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.codex_reviewer_available", lambda cfg: False)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(simulate=True, require_real_peer_review=True)
    )
    preflight = adapter.peer_review_preflight(task="T-codex-notfound", context={})
    assert preflight["ready"] is False
    assert "executable not found" in preflight["blocking_error"]
    assert "codex" in preflight["blocking_error"]


def test_preflight_cli_incompatible_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """When backend=cli but executable is codex, error says 'not a claude binary'."""
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_BACKEND", "cli")
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_EXECUTABLE", "codex")
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(simulate=True, require_real_peer_review=True)
    )
    preflight = adapter.peer_review_preflight(task="T-cli-incompat", context={})
    assert preflight["ready"] is False
    assert "not a claude binary" in preflight["blocking_error"]
    assert "'codex'" in preflight["blocking_error"]


def test_preflight_cli_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """When backend=cli and executable is claude but not on PATH, error says 'not found'."""
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_BACKEND", "cli")
    monkeypatch.delenv("WORKFLOW_OPUS_REVIEWER_EXECUTABLE", raising=False)
    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.cli_reviewer_available", lambda cfg: False)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(simulate=True, require_real_peer_review=True)
    )
    preflight = adapter.peer_review_preflight(task="T-cli-notfound", context={})
    assert preflight["ready"] is False
    assert "executable not found" in preflight["blocking_error"]
    assert "claude" in preflight["blocking_error"]


def test_preflight_mcp_incompatible_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """When backend=mcp but executable is python, error says 'not a claude binary'."""
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_BACKEND", "mcp")
    monkeypatch.setenv("WORKFLOW_OPUS_REVIEWER_EXECUTABLE", "python")
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(simulate=True, require_real_peer_review=True)
    )
    preflight = adapter.peer_review_preflight(task="T-mcp-incompat", context={})
    assert preflight["ready"] is False
    assert "not a claude binary" in preflight["blocking_error"]
    assert "'python'" in preflight["blocking_error"]


def test_local_adapter_accepts_executor_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_EXECUTOR_TIMEOUT_SECONDS", "345")

    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    assert adapter.config.timeout_seconds == 345


def test_local_adapter_prefers_changed_files_from_task_scope() -> None:
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))
    impl = adapter.implement(
        task="T010",
        context={
            "task_scope": "files_to_change=['app/main.py', 'app/schemas.py', 'tests/test_api.py']; test_plan=run pytest",
            "attempt": 1,
        },
    )
    assert impl["status"] == "done"
    assert impl["changes"] == ["app/main.py", "app/schemas.py", "tests/test_api.py"]


def test_local_adapter_real_review_attaches_deterministic_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "files_to_change": ["app/main.py", "tests/test_main.py"],
                "invariants": ["single source of truth"],
            }
        ),
        encoding="utf-8",
    )
    (planning_dir / "ARCHITECTURE_PLAN.json").write_text(
        json.dumps(
            {
                "module_boundaries": [
                    {"name": "backend", "roots": ["app"]},
                    {"name": "tests", "roots": ["tests"]},
                ],
                "verify_recipes": [{"surface": "api", "required": True, "command": "pytest -q"}],
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def _fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args
        captured["review_bundle"] = dict(kwargs.get("review_bundle") or {})
        return (
            {
                "approved": False,
                "summary": "Rejected by reviewer.",
                "must_fix": ["add tests"],
                "should_fix": [],
                "blocking_items": ["scope violation"],
                "severity": "high",
                "score": 40,
                "target_score": 95,
                "min_dimension_score": 80,
                "gate_recommendation": "REVIEW_FIX_REQUIRED",
                "reviewer": "opus",
                "source": "kodawari.real_peer_review_gateway",
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_opus_review", _fake_request)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            reviewer_base_url="https://example.test",
            reviewer_api_key="test-key",
            cwd=project_root,
        )
    )
    review = adapter.review(
        task="T100",
        context={
            "task_id": "T1",
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_card_files": ["app/main.py", "tests/test_main.py"],
        },
        changed_files=["app/main.py", "app/rogue.py"],
        review_iteration=1,
    )

    assert review["approved"] is False
    bundle = dict(captured["review_bundle"])
    findings = dict(bundle.get("deterministic_findings") or {})
    assert findings["schema_version"] == "review.precheck.v1"
    assert "app/rogue.py" in findings["out_of_scope_files"]
    assert "app/main.py" in findings["missing_test_files"]


def test_local_adapter_real_review_counts_verified_scoped_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    (project_root / "app").mkdir(parents=True)
    (project_root / "tests").mkdir(parents=True)
    (project_root / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (project_root / "tests" / "test_main.py").write_text("def test_handler():\n    assert True\n", encoding="utf-8")
    planning_dir.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def _fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args
        captured["review_bundle"] = dict(kwargs.get("review_bundle") or {})
        return (
            {
                "approved": True,
                "summary": "ok",
                "must_fix": [],
                "should_fix": [],
                "blocking_items": [],
                "severity": "info",
                "score": 98,
                "target_score": 95,
                "min_dimension_score": 80,
                "gate_recommendation": "APPROVED",
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_opus_review", _fake_request)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            reviewer_base_url="https://example.test",
            reviewer_api_key="test-key",
            cwd=project_root,
        )
    )
    review = adapter.review(
        task="T100",
        context={
            "task_id": "T1",
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_card_files": ["app/main.py", "tests/test_main.py"],
            "runtime_verify_check": {
                "status": "PASS",
                "passed": True,
                "command_executed": True,
                "returncode": 0,
                "verify_cmd_resolved": "python -m pytest tests/test_main.py -q",
                "verify_targets": ["tests/test_main.py"],
            },
        },
        changed_files=["app/main.py"],
        review_iteration=1,
    )

    assert review["approved"] is True
    bundle = dict(captured["review_bundle"])
    findings = dict(bundle.get("deterministic_findings") or {})
    assert findings["missing_test_files"] == []
    assert findings["verified_test_files"] == ["tests/test_main.py"]
    assert bundle["verified_test_snippets"][0]["path"] == "tests/test_main.py"


def test_local_adapter_precheck_pass_does_not_force_approve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T2",
                "files_to_change": ["app/main.py", "tests/test_main.py"],
                "invariants": ["single source of truth"],
            }
        ),
        encoding="utf-8",
    )

    def _fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return (
            {
                "approved": False,
                "summary": "Logic regression detected in diff.",
                "must_fix": ["fix failing branch condition"],
                "should_fix": [],
                "blocking_items": ["logic mismatch"],
                "severity": "high",
                "score": 70,
                "target_score": 95,
                "min_dimension_score": 80,
                "gate_recommendation": "REVIEW_FIX_REQUIRED",
                "reviewer": "opus",
                "source": "kodawari.real_peer_review_gateway",
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_opus_review", _fake_request)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            reviewer_base_url="https://example.test",
            reviewer_api_key="test-key",
            cwd=project_root,
        )
    )
    review = adapter.review(
        task="T101",
        context={
            "task_id": "T2",
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_card_files": ["app/main.py", "tests/test_main.py"],
        },
        changed_files=["app/main.py", "tests/test_main.py"],
        review_iteration=1,
    )

    assert review["approved"] is False
    assert "logic" in str(review["summary"]).lower()


def test_local_adapter_simulated_review_blocks_when_tests_are_out_of_scope() -> None:
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    review = adapter.review(
        task="T102",
        context={"task_card_files": ["app/main.py"]},
        changed_files=["app/main.py"],
        review_iteration=0,
    )

    assert review["approved"] is False
    assert review["gate_recommendation"] == "REVIEW_SCOPE_CONFLICT"
    assert "current task scope does not include any test files" in review["blocking_reason"]
    assert review["blocking_items"] == [review["blocking_reason"]]


def test_local_adapter_simulated_review_accepts_verified_scoped_test_evidence(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    (project_root / "app").mkdir(parents=True)
    (project_root / "tests").mkdir(parents=True)
    (project_root / "app" / "main.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (project_root / "tests" / "test_main.py").write_text("def test_handler():\n    assert True\n", encoding="utf-8")
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True, cwd=project_root))

    review = adapter.review(
        task="T102",
        context={
            "project_root": str(project_root),
            "task_card_files": ["app/main.py", "tests/test_main.py"],
            "runtime_verify_check": {
                "status": "PASS",
                "passed": True,
                "command_executed": True,
                "returncode": 0,
                "verify_cmd_resolved": "python -m pytest tests/test_main.py -q",
            },
        },
        changed_files=["app/main.py"],
        review_iteration=0,
    )

    assert review["approved"] is True
    assert review["gate_recommendation"] == "PROCEED_TO_GATE"


def test_local_adapter_real_review_guard_blocks_when_missing_tests_are_out_of_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "repo"
    planning_dir = project_root / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text(
        json.dumps(
            {
                "task_id": "T3",
                "files_to_change": ["app/main.py"],
                "invariants": ["single source of truth"],
            }
        ),
        encoding="utf-8",
    )

    def _fake_request(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        return (
            {
                "approved": False,
                "summary": "Reviewer wants scoped tests.",
                "must_fix": ["Must fix: add scoped tests for changed files"],
                "should_fix": [],
                "blocking_items": [],
                "severity": "high",
                "score": 76,
                "target_score": 95,
                "min_dimension_score": 80,
                "gate_recommendation": "REVIEW_FIX_REQUIRED",
                "reviewer": "opus",
                "source": "kodawari.real_peer_review_gateway",
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_opus_review", _fake_request)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            reviewer_base_url="https://example.test",
            reviewer_api_key="test-key",
            cwd=project_root,
        )
    )
    review = adapter.review(
        task="T103",
        context={
            "task_id": "T3",
            "project_root": str(project_root),
            "planning_dir": str(planning_dir),
            "task_card_files": ["app/main.py"],
        },
        changed_files=["app/main.py"],
        review_iteration=1,
    )

    assert review["approved"] is False
    assert review["gate_recommendation"] == "REVIEW_SCOPE_CONFLICT"
    assert "current task scope does not include any test files" in review["blocking_reason"]


def test_local_adapter_review_does_not_treat_substring_test_filenames_as_tests() -> None:
    """File names that merely contain the substring 'test' (e.g. 'latest_results.py',
    'contest_runner.py') must NOT be classified as test files. Otherwise review
    silently approves even when the actual test scope is missing."""
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    review = adapter.review(
        task="T250",
        context={"task_card_files": ["src/latest_results.py"]},
        changed_files=["src/latest_results.py"],
        review_iteration=0,
    )

    assert review["approved"] is False, (
        "latest_results.py contains substring 'test' but is a source file; "
        "review must not auto-approve when scoped tests are absent"
    )
    assert review["gate_recommendation"] == "REVIEW_SCOPE_CONFLICT"
    assert "current task scope does not include any test files" in review["blocking_reason"]


def test_local_adapter_review_requires_tests_when_source_only_even_with_contest_substring() -> None:
    """Similar edge case for task_card_files: 'contest_runner.py' is a source
    file, not a test, so the task scope does not include tests."""
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    review = adapter.review(
        task="T251",
        context={"task_card_files": ["app/contest_runner.py"]},
        changed_files=["app/contest_runner.py"],
        review_iteration=0,
    )

    assert review["approved"] is False
    assert review["gate_recommendation"] == "REVIEW_SCOPE_CONFLICT"


def test_local_adapter_review_recognizes_canonical_test_files_and_approves_first_round() -> None:
    """Conversely, real test files (``tests/test_foo.py``) must still be
    recognized so the review can approve when changes include tests."""
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True))

    review = adapter.review(
        task="T252",
        context={"task_card_files": ["app/foo.py", "tests/test_foo.py"]},
        changed_files=["app/foo.py", "tests/test_foo.py"],
        review_iteration=0,
    )

    assert review["approved"] is True
    assert review["gate_recommendation"] == "PROCEED_TO_GATE"


def test_local_adapter_self_review_falls_back_to_local_default_when_unset() -> None:
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True, self_review_backend=""))

    payload = adapter.self_review(
        task="T300",
        context={},
        changed_files=["app/main.py", "tests/test_main.py"],
        review_iteration=0,
    )

    assert payload["approved"] is True
    assert payload["source"] == "kodawari.self_review.local_default"


def test_local_adapter_self_review_noop_falls_back_outside_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.is_test_environment", lambda: False)
    adapter = LocalCodexAdapter(LocalCodexAdapterConfig(simulate=True, self_review_backend="noop_test_only"))

    payload = adapter.self_review(
        task="T301",
        context={},
        changed_files=["app/main.py", "tests/test_main.py"],
        review_iteration=0,
    )

    assert payload["approved"] is True
    assert payload["source"] == "kodawari.self_review.noop_fallback"


def test_local_adapter_recovery_unavailable_when_api_credentials_missing(tmp_path: Path) -> None:
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            cwd=tmp_path,
            recovery_backend="api",
            recovery_model="swap-any-model",
        )
    )

    payload = adapter.synthesize_executor_recovery(
        task="T310",
        context={
            "project_root": str(tmp_path),
            "task_card": {"files_to_change": ["src/app.py"]},
            "task_card_files": ["src/app.py"],
        },
        must_fix=["executor stalled"],
    )

    assert payload["status"] == "unavailable"
    assert payload["model"] == "swap-any-model"
    assert payload["decision"]["action"] == "escalate_to_human"
    assert "api recovery requires" in payload["decision"]["diagnosis"]


def test_local_adapter_retries_recovery_when_existing_files_request_full_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    calls: list[dict] = []

    def _fake_recovery_decision(*_args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(dict(kwargs))
        if len(calls) == 1:
            return (
                {
                    "action": "expand_scope_request",
                    "requested_files": ["src/app.py"],
                    "reason": "need full source",
                },
                "",
            )
        return (
            {
                "action": "narrow_patch_plan",
                "patch_plan": [
                    {
                        "operation": "str_replace",
                        "path": "src/app.py",
                        "old_text": "old",
                        "new_text": "new",
                    }
                ],
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_recovery_decision", _fake_recovery_decision)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            cwd=tmp_path,
            recovery_backend="codex",
            recovery_model="gpt-5.5",
            recovery_executable_codex=str(executable),
        )
    )

    payload = adapter.synthesize_executor_recovery(
        task="T311",
        context={
            "project_root": str(tmp_path),
            "task_card": {"files_to_change": ["src/app.py"]},
            "task_card_files": ["src/app.py"],
        },
        must_fix=["executor stalled"],
    )

    assert payload["status"] == "ok"
    assert payload["decision"]["action"] == "narrow_patch_plan"
    assert calls[1]["full_source_files"] == ["src/app.py"]


def test_recovery_synthesizer_config_keeps_truthy_whitespace_fallback_semantics() -> None:
    config = recovery_helpers.build_recovery_synthesizer_config(
        LocalCodexAdapterConfig(
            recovery_backend="codex",
            recovery_model=" ",
            reviewer_model="reviewer-model",
            recovery_base_url=" ",
            reviewer_base_url="https://reviewer.example",
            recovery_api_key=" ",
            reviewer_api_key="reviewer-key",
            recovery_executable_codex=" ",
            reviewer_executable_codex="codex-reviewer",
        ),
        resolved_reviewer_backend="codex",
    )

    assert config.model == ""
    assert config.base_url == ""
    assert config.api_key == ""
    assert config.executable == ""


def test_recovery_helpers_preserve_context_fallback_order(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    project_root = tmp_path / "project"
    recovery_root = tmp_path / "recovery"

    context = {
        "task_card_files": ["ctx.py", "", "  "],
        "project_root": str(project_root),
        "recovery_source_root": str(recovery_root),
        "previous_recovery_decisions": [{"action": "narrow_patch_plan"}],
        "previous_execution_result": {"status": "BLOCKED"},
    }

    assert recovery_helpers._recovery_allowed_files(context, {"files_to_change": ["card.py"]}) == ["ctx.py"]
    assert recovery_helpers._recovery_allowed_files({}, {"files_to_change": ["card.py"]}) == ["card.py"]
    assert recovery_helpers._recovery_project_root(context, cwd) == recovery_root.resolve()
    assert recovery_helpers._recovery_project_root({"project_root": str(project_root)}, cwd) == project_root.resolve()
    assert recovery_helpers._recovery_project_root({}, cwd) == cwd.resolve()
    assert recovery_helpers._previous_recovery_context(context) == {
        "previous_recovery_decisions": [{"action": "narrow_patch_plan"}],
        "previous_execution_result": {"status": "BLOCKED"},
    }
    assert recovery_helpers._previous_recovery_context(context, retry_reason="full source")["retry_reason"] == "full source"


def test_recovery_retry_helper_returns_retry_decision_or_error(tmp_path: Path) -> None:
    config = recovery_helpers.RecoverySynthesizerConfig(backend="codex", executable="codex", model="gpt")

    def _normalize(raw, *, allowed_files):  # type: ignore[no-untyped-def]
        return {"action": raw["action"], "allowed_files": list(allowed_files)}

    def _retry_success(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return {"action": "narrow_patch_plan"}, ""

    assert recovery_helpers._maybe_retry_for_full_source(
        _retry_success,
        _normalize,
        config=config,
        task="T1",
        task_card={},
        must_fix=[],
        stall_report=None,
        allowed_files=["src/app.py"],
        context={},
        project_root=tmp_path,
        requested_existing=["src/app.py"],
        initial_decision={"action": "expand_scope_request"},
    ) == {"action": "narrow_patch_plan", "allowed_files": ["src/app.py"]}

    def _retry_error(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None, "retry failed"

    assert recovery_helpers._maybe_retry_for_full_source(
        _retry_error,
        _normalize,
        config=config,
        task="T1",
        task_card={},
        must_fix=[],
        stall_report=None,
        allowed_files=["src/app.py"],
        context={},
        project_root=tmp_path,
        requested_existing=["src/app.py"],
        initial_decision={"action": "expand_scope_request"},
    )["diagnosis"] == "retry failed"

    def _retry_empty(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return None, ""

    initial = {"action": "expand_scope_request"}
    assert recovery_helpers._maybe_retry_for_full_source(
        _retry_empty,
        _normalize,
        config=config,
        task="T1",
        task_card={},
        must_fix=[],
        stall_report=None,
        allowed_files=["src/app.py"],
        context={},
        project_root=tmp_path,
        requested_existing=["src/app.py"],
        initial_decision=initial,
    ) is initial


# --- reviewer project_root threading ---


def test_real_peer_review_cli_threads_project_root_from_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_real_peer_review_cli must pass config.cwd-resolved project_root, not None."""
    planning_dir = tmp_path / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text("{}", encoding="utf-8")

    captured: dict = {}

    def _fake_cli(config, *, task, context, changed_files, review_iteration, review_bundle, project_root=None):  # type: ignore[no-untyped-def]
        captured["project_root"] = project_root
        return (
            {
                "approved": True, "summary": "ok", "must_fix": [], "should_fix": [],
                "blocking_items": [], "severity": "low", "score": 95, "target_score": 90,
                "min_dimension_score": 80, "gate_recommendation": "PASS", "evidence": [],
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_cli_review", _fake_cli)

    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            opus_reviewer_backend="cli",
            cwd=tmp_path,
        )
    )
    adapter._real_peer_review_cli(
        task="T1: test",
        context={
            "task_id": "T1",
            "project_root": str(tmp_path),
            "planning_dir": str(planning_dir),
        },
        changed_files=["src/app.py"],
        review_iteration=0,
    )

    assert captured["project_root"] is not None
    assert str(captured["project_root"]) == str(tmp_path.resolve())


def test_real_peer_review_cli_falls_back_to_config_cwd_when_context_has_no_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When context["project_root"] is absent, must fall back to self.config.cwd."""
    planning_dir = tmp_path / "planning" / "demo"
    planning_dir.mkdir(parents=True, exist_ok=True)
    (planning_dir / "TASK_CARD_ACTIVE.json").write_text("{}", encoding="utf-8")

    captured: dict = {}

    def _fake_cli(config, *, task, context, changed_files, review_iteration, review_bundle, project_root=None):  # type: ignore[no-untyped-def]
        captured["project_root"] = project_root
        return (
            {
                "approved": True, "summary": "ok", "must_fix": [], "should_fix": [],
                "blocking_items": [], "severity": "low", "score": 95, "target_score": 90,
                "min_dimension_score": 80, "gate_recommendation": "PASS", "evidence": [],
            },
            "",
        )

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_cli_review", _fake_cli)

    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            opus_reviewer_backend="cli",
            cwd=tmp_path,
        )
    )
    adapter._real_peer_review_cli(
        task="T1: test",
        context={"task_id": "T1", "planning_dir": str(planning_dir)},  # no project_root in context
        changed_files=["src/app.py"],
        review_iteration=0,
    )

    assert captured["project_root"] is not None
    assert str(captured["project_root"]) == str(tmp_path.resolve())

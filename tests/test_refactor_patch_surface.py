from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.autopilot.execution.execution_backend import ExecutionBackendConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
TEST_ROOT = REPO_ROOT / "tests"

PATCH_SURFACE = {
    "kodawari.autopilot.execution.local_adapter.request_opus_review",
    "kodawari.autopilot.execution.local_adapter.request_codex_review",
    "kodawari.autopilot.execution.local_adapter.cli_reviewer_available",
    "kodawari.autopilot.execution.local_adapter.codex_reviewer_available",
    "kodawari.autopilot.execution.local_adapter.is_test_environment",
    "kodawari.autopilot.execution.local_adapter.request_recovery_decision",
    "kodawari.autopilot.execution.local_adapter.request_cli_review",
    "kodawari.cli.contract.autopilot_contract_bridge.run_planning_conversation",
    "kodawari.cli.contract.autopilot_contract_bridge._suggest_max_rounds",
    "kodawari.cli.contract.autopilot_contract_bridge._planning_config_from_env",
    "kodawari.cli.contract.autopilot_contract_bridge._env_blocking_severities",
    "kodawari.cli.contract.autopilot_contract_bridge._raise_if_context_scout_awaiting_decision",
    "kodawari.autopilot.execution.execution_openai_tool_use._post_chat",
    "kodawari.autopilot.execution.execution_openai_tool_use.maybe_execute_verify_command",
}

PATCH_PREFIXES = tuple(
    sorted({item.rsplit(".", 1)[0] + "." for item in PATCH_SURFACE})
)

FACADE_FILES = {
    (SRC_ROOT / "kodawari" / "autopilot" / "execution" / "local_adapter.py").resolve(),
    (SRC_ROOT / "kodawari" / "cli" / "contract" / "autopilot_contract_bridge.py").resolve(),
    (SRC_ROOT / "kodawari" / "autopilot" / "execution" / "execution_openai_tool_use.py").resolve(),
    # Refactored runtime split from execution_openai_tool_use; re-routes verify
    # calls through the public module via _verify_command_runner so monkeypatch
    # at the facade still intercepts. See tool_use_runtime._verify_command_runner.
    (SRC_ROOT / "kodawari" / "autopilot" / "execution" / "tool_use_runtime.py").resolve(),
}

DIRECT_IMPORT_ALLOWLIST = {
    # Standalone canonical consumer; not reached through the execution_openai_tool_use facade entrypoint.
    (
        "kodawari.autopilot.execution.verify_execution",
        "maybe_execute_verify_command",
        "src/kodawari/autopilot/core/runtime_checks.py",
    ),
}


def _resolve(dotted: str) -> Any:
    module_name, attr = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module_name), attr)


def _iter_string_patch_paths() -> set[str]:
    found: set[str] = set()
    for path in TEST_ROOT.rglob("test_*.py"):
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(module):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            first_arg = node.args[0]
            if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                value = first_arg.value
                if value.startswith(PATCH_PREFIXES):
                    found.add(value)
    return found


def _module_name_from_path(path: Path) -> str:
    rel = path.relative_to(SRC_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _string_attr(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _import_violations() -> list[tuple[str, int, str, str]]:
    violations: list[tuple[str, int, str, str]] = []
    blocked = {
        "kodawari.autopilot.execution.verify_execution": {"maybe_execute_verify_command"},
    }
    for path in SRC_ROOT.rglob("*.py"):
        resolved = path.resolve()
        if resolved in FACADE_FILES:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        module = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        current_module = _module_name_from_path(path)
        imports: dict[str, str] = {}
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module in blocked:
                for alias in node.names:
                    if alias.name in blocked[node.module]:
                        key = (node.module, alias.name, rel)
                        if key not in DIRECT_IMPORT_ALLOWLIST:
                            violations.append((rel, node.lineno, node.module, alias.name))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in blocked:
                        imports[alias.asname or alias.name.rsplit(".", 1)[-1]] = alias.name
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                module_name = imports.get(node.value.id)
                if module_name and node.attr in blocked.get(module_name, set()):
                    key = (module_name, node.attr, rel)
                    if key not in DIRECT_IMPORT_ALLOWLIST and current_module not in FACADE_FILES:
                        violations.append((rel, node.lineno, module_name, node.attr))
    return violations


@pytest.mark.parametrize("dotted", sorted(PATCH_SURFACE))
def test_refactor_patch_surface_attributes_exist(dotted: str) -> None:
    assert _resolve(dotted) is not None


def test_refactor_patch_surface_tracks_known_string_patches() -> None:
    assert _iter_string_patch_paths() <= PATCH_SURFACE


def test_refactor_facade_reachable_paths_do_not_bypass_patch_surface_imports() -> None:
    assert _import_violations() == []


def test_local_adapter_facade_patch_reaches_real_review(monkeypatch: pytest.MonkeyPatch) -> None:
    from kodawari.autopilot.execution.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    called: dict[str, Any] = {}

    def fake_request(*_args: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        called["kwargs"] = kwargs
        return {
            "approved": True,
            "summary": "patched review",
            "must_fix": [],
            "should_fix": [],
            "blocking_items": [],
            "severity": "low",
            "score": 99,
            "target_score": 95,
            "min_dimension_score": 80,
            "gate_recommendation": "PROCEED_TO_GATE",
            "source": "kodawari.real_peer_review_gateway",
        }, ""

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_opus_review", fake_request)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            real_peer_review=True,
            reviewer_base_url="https://example.test",
            reviewer_api_key="test-key",
        )
    )

    review = adapter.review(
        task="T-patch",
        context={"task_id": "T-patch"},
        changed_files=["src/app.py", "tests/test_app.py"],
        review_iteration=1,
    )

    assert called["kwargs"]["task"] == "T-patch"
    assert review["summary"] == "patched review"


def test_local_adapter_facade_patch_reaches_recovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from kodawari.autopilot.execution.local_adapter import LocalCodexAdapter, LocalCodexAdapterConfig

    called: dict[str, Any] = {}

    def fake_recovery(*_args: Any, **kwargs: Any) -> tuple[dict[str, Any], str]:
        called["kwargs"] = kwargs
        return {"action": "abort_with_diagnosis", "diagnosis": "patched recovery"}, ""

    monkeypatch.setattr("kodawari.autopilot.execution.local_adapter.request_recovery_decision", fake_recovery)
    adapter = LocalCodexAdapter(
        LocalCodexAdapterConfig(
            simulate=True,
            cwd=tmp_path,
            recovery_backend="api",
            recovery_base_url="https://example.test",
            recovery_api_key="test-key",
        )
    )

    result = adapter.synthesize_executor_recovery(
        task="T-recovery",
        context={
            "project_root": str(tmp_path),
            "task_card": {"files_to_change": ["src/app.py"]},
        },
        must_fix=["fix it"],
    )

    assert called["kwargs"]["task"] == "T-recovery"
    assert result["decision"]["diagnosis"] == "patched recovery"


def test_contract_bridge_private_facade_patch_reaches_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import kodawari.cli.contract.autopilot_contract_bridge as bridge

    monkeypatch.setattr(bridge, "_suggest_max_rounds", lambda **_kwargs: 7)
    config = bridge._planning_config_from_env(
        project_root=tmp_path,
        task_direction="refactor execution and recovery",
        repo_inventory={"project_layout": {"code_roots": ["src"]}},
    )

    assert config.max_rounds == 7


def _tool_response(name: str, args: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "usage": {"total_tokens": 1},
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": f"call_{idx}",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args)},
                        }
                    ]
                }
            }
        ],
    }


def _execution_config(root: Path, planning_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ExecutionBackendConfig:
    monkeypatch.setenv("WORKFLOW_FAKE_OPENAI_KEY", "test-key")
    return ExecutionBackendConfig(
        backend="openai_tool_use",
        command="",
        project_root=root,
        planning_dir=planning_dir,
        feature="feature",
        model="mimo-v2.5-pro",
        base_url="http://localhost/v1",
        api_key_env="WORKFLOW_FAKE_OPENAI_KEY",
        api_format="openai_chat",
        runtime_caps={"max_tool_iterations": 5, "max_wall_clock_seconds": 120},
    )


def test_execution_facade_patch_reaches_post_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from kodawari.autopilot.execution import execution_openai_tool_use as runner

    root = tmp_path / "repo"
    planning_dir = root / "planning"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    request_path = planning_dir / ".execution_request.json"
    request_path.write_text(
        json.dumps(
                {
                    "feature": "feature",
                    "task": "T1",
                    "project_root": str(root),
                    "planning_dir": str(planning_dir),
                "task_id": "T1",
                "requested_action": "update sample",
                "files_to_change": ["sample.txt"],
            }
        ),
        encoding="utf-8",
    )
    calls = [
        ("write_new_file", {"path": "sample.txt", "content": "after\n"}),
        ("finish_execution", {"summary": "done"}),
    ]
    seen: list[str] = []

    def fake_post_chat(**_kwargs: Any) -> dict[str, Any]:
        idx = len(seen)
        name, args = calls[idx]
        seen.append(name)
        return _tool_response(name, args, idx)

    monkeypatch.setattr(runner, "_post_chat", fake_post_chat)

    result = runner.materialize_openai_tool_use_result(
        config=_execution_config(root, planning_dir, monkeypatch),
        request_path=request_path,
        request_payload=json.loads(request_path.read_text(encoding="utf-8")),
    )

    assert seen == ["write_new_file", "finish_execution"]
    assert result["status"] == "PASS"


def test_execution_facade_patch_reaches_verify_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from kodawari.autopilot.execution import execution_openai_tool_use as runner

    root = tmp_path / "repo"
    planning_dir = root / "planning"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    request_payload = {
        "project_root": str(root),
        "planning_dir": str(planning_dir),
        "task_id": "T1",
        "requested_action": "verify sample",
        "files_to_change": ["sample.txt"],
        "verify_cmd": "pytest -q",
    }
    request_path = planning_dir / ".execution_request.json"
    request_path.write_text(json.dumps(request_payload), encoding="utf-8")
    runtime = runner.ToolUseRuntime(
        _execution_config(root, planning_dir, monkeypatch),
        request_path,
        request_payload,
        root,
        planning_dir,
        ["sample.txt"],
    )
    seen: dict[str, Any] = {}

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "PASS", "cmd": kwargs["verify_cmd"]}

    monkeypatch.setattr(runner, "maybe_execute_verify_command", fake_verify)

    try:
        assert runtime._run_verify(["sample.txt"]) == {"status": "PASS", "cmd": "pytest -q"}
    finally:
        runtime.cleanup_success()
    assert seen["verify_cmd"] == "pytest -q"

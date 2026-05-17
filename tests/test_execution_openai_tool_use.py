from __future__ import annotations

import io
import json
from pathlib import Path
import textwrap
from types import SimpleNamespace
from typing import Any
from urllib import request as urlrequest

import pytest

from kodawari.autopilot.execution import execution_openai_tool_use as runner
from kodawari.autopilot.execution.execution_backend import ExecutionBackendConfig


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _config(
    root: Path,
    planning_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    execution_protocol: str = "",
) -> ExecutionBackendConfig:
    monkeypatch.setenv("WORKFLOW_FAKE_OPENAI_KEY", "tp-super-secret-test-key")
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
        transport_name="mimo_api",
        execution_protocol=execution_protocol,
        runtime_caps={
            "max_tool_iterations": 10,
            "max_token_budget": 200000,
            "max_same_tool_calls_per_path": 5,
            "max_tool_calls_per_response": 4,
            "max_wall_clock_seconds": 120,
            "max_no_progress_iterations": 5,
            "max_verify_retries": 2,
        },
    )


def _request(root: Path, planning_dir: Path, *, verify_cmd: str = "") -> dict[str, Any]:
    return {
        "schema_version": "execution.request.v1",
        "feature": "feature",
        "task": "T001",
        "backend": "openai_tool_use",
        "project_root": str(root),
        "planning_dir": str(planning_dir),
        "task_id": "T001",
        "requested_action": "Update sample.txt.",
        "review_round": 0,
        "attempt": 1,
        "files_to_change": ["sample.txt"],
        "invariants": [],
        "task_card": {},
        "task_scope": "unit",
        "task_requirements": "Update sample.txt.",
        "verify_cmd": verify_cmd,
    }


def _runtime(
    root: Path,
    planning_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    request_payload: dict[str, Any] | None = None,
    execution_protocol: str = "exact_str_replace_v1",
    allowed_files: list[str] | None = None,
) -> runner.ToolUseRuntime:
    payload = request_payload or {**_request(root, planning_dir), "execution_protocol": execution_protocol}
    return runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch, execution_protocol=execution_protocol),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=allowed_files or ["sample.txt", "sample.py"],
    )


def _tool_response(name: str, args: dict[str, Any], idx: int) -> dict[str, Any]:
    return {
        "model": "mimo-v2.5-pro",
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


def _install_fake_post_chat(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, dict[str, Any]]]) -> None:
    index = {"value": 0}

    def _fake(**_kwargs: Any) -> dict[str, Any]:
        i = index["value"]
        index["value"] += 1
        if i >= len(calls):
            raise AssertionError("unexpected extra model call")
        name, args = calls[i]
        return _tool_response(name, args, i)

    monkeypatch.setattr(runner, "_post_chat", _fake)


class _FakeHttpResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class _FlakyOpener:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0

    def open(self, *_args: Any, **_kwargs: Any) -> _FakeHttpResponse:
        self.calls += 1
        if self.calls == 1:
            raise OSError("Remote end closed connection without response")
        return _FakeHttpResponse(self.payload)


def test_openai_tool_use_commits_only_after_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    (planning_dir / runner.STALL_REPORT_FILENAME).write_text('{"run_id":"old"}', encoding="utf-8")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("read_file", {"path": "sample.txt"}),
            ("write_new_file", {"path": "sample.txt", "content": "after\n"}),
            ("finish_execution", {"summary": "updated sample"}),
        ],
    )

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "PASS"
    assert result["changed_files"] == ["sample.txt"]
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"
    assert not (planning_dir / runner.STALL_REPORT_FILENAME).exists()
    manifest = json.loads((planning_dir / ".execution_tool_manifest.json").read_text(encoding="utf-8"))
    assert "write_new_file" in manifest["tools"]
    assert "replace_in_file" not in manifest["tools"]
    assert not Path(manifest["scratch_root"]).exists()
    log_lines = (planning_dir / ".execution_tool_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 3
    write_call = json.loads(log_lines[1])
    assert write_call["tool"] == "write_new_file"
    assert "content" not in write_call["arguments"]
    assert write_call["arguments"]["content_sha256"]


def test_post_chat_retries_transient_remote_close(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _FlakyOpener({"choices": [{"message": {"tool_calls": []}}]})
    monkeypatch.setattr(runner.urlrequest, "build_opener", lambda *_args: opener)
    monkeypatch.setattr(runner.time, "sleep", lambda *_args: None)

    payload = runner._post_chat(
        endpoint="https://example.test/v1/chat/completions",
        api_key="tp-super-secret-test-key",
        payload={"model": "mimo-v2.5-pro"},
        timeout_seconds=5,
        max_retries=1,
    )

    assert payload["choices"][0]["message"]["tool_calls"] == []
    assert opener.calls == 2


def test_post_chat_classifies_miwaf_403(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BlockedOpener:
        def open(self, *_args: Any, **_kwargs: Any) -> Any:
            raise runner.urlerror.HTTPError(
                "https://example.test/v1/chat/completions",
                403,
                "Forbidden",
                {},
                io.BytesIO("您的请求可能对网站造成安全威胁，请求已被阻断 MiWAF".encode("utf-8")),
            )

    monkeypatch.setattr(runner.urlrequest, "build_opener", lambda *_args: _BlockedOpener())

    with pytest.raises(runner.OpenAIToolUseExecutionError) as exc:
        runner._post_chat(
            endpoint="https://example.test/v1/chat/completions",
            api_key="tp-super-secret-test-key",
            payload={"model": "mimo-v2.5-pro"},
            timeout_seconds=5,
            max_retries=0,
        )

    assert exc.value.code == "HTTP_WAF_BLOCKED"
    assert "MiWAF" in exc.value.message


def test_openai_tool_use_blocks_out_of_scope_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    _install_fake_post_chat(monkeypatch, [("write_new_file", {"path": "../secret.txt", "content": "leak"})])

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] in {"PATH_OUT_OF_SCOPE", "PATH_GUARD_BLOCKED"}
    assert (root / "sample.txt").read_text(encoding="utf-8") == "before\n"
    assert not (tmp_path / "secret.txt").exists()
    log_lines = (planning_dir / ".execution_tool_calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(log_lines[0])["error_code"] in {"PATH_OUT_OF_SCOPE", "PATH_GUARD_BLOCKED"}


def test_openai_tool_use_returns_safe_error_for_out_of_scope_read_then_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("read_file_partial", {"path": "outside.py", "offset": 0, "limit": 100}),
            ("write_new_file", {"path": "sample.txt", "content": "after\n"}),
            ("finish_execution", {"summary": "updated sample"}),
        ],
    )

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "PASS"
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"
    log_lines = (planning_dir / ".execution_tool_calls.jsonl").read_text(encoding="utf-8").splitlines()
    first = json.loads(log_lines[0])
    assert first["tool"] == "read_file_partial"
    assert first["error_code"] == "PATH_OUT_OF_SCOPE"
    assert first["result"]["ok"] is False


def test_openai_tool_use_allows_read_only_task_context_without_write_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    _write(root / "tests" / "test_existing.py", "def test_existing():\n    assert True\n")
    _write(root / "backend" / "api" / "v1" / "services" / "db.py", "def get_connection():\n    pass\n")
    payload = _request(root, planning_dir)
    payload["task_card"] = {
        "related_existing_tests": ["tests/test_existing.py"],
        "do_not_change": ["backend/api/v1/services/db.py"],
    }
    _install_fake_post_chat(
        monkeypatch,
        [
            ("read_file_partial", {"path": "tests/test_existing.py", "offset": 0, "limit": 200}),
            ("read_file_partial", {"path": "backend/api/v1/services/db.py", "offset": 0, "limit": 200}),
            ("write_new_file", {"path": "sample.txt", "content": "after\n"}),
            ("finish_execution", {"summary": "updated sample"}),
        ],
    )

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["changed_files"] == ["sample.txt"]
    assert (root / "tests" / "test_existing.py").read_text(encoding="utf-8").startswith("def test_existing")
    manifest = json.loads((planning_dir / ".execution_tool_manifest.json").read_text(encoding="utf-8"))
    assert manifest["allowed_files"] == ["sample.txt"]
    assert manifest["read_only_files"] == [
        "tests/test_existing.py",
        "backend/api/v1/services/db.py",
    ]


def test_openai_tool_use_blocks_writes_to_read_only_task_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    _write(root / "tests" / "test_existing.py", "def test_existing():\n    assert True\n")
    payload = _request(root, planning_dir)
    payload["task_card"] = {"related_existing_tests": ["tests/test_existing.py"]}
    _install_fake_post_chat(
        monkeypatch,
        [("write_new_file", {"path": "tests/test_existing.py", "content": "changed\n"})],
    )

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "PATH_OUT_OF_SCOPE"
    assert (root / "tests" / "test_existing.py").read_text(encoding="utf-8").startswith("def test_existing")


def test_openai_tool_use_auto_widens_safe_read_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    _write(root / "backend" / "api" / "v1" / "services" / "target.py", "VALUE = 1\n")
    _write(root / "backend" / "api" / "v1" / "services" / "neighbor.py", "HELPER = 1\n")
    _write(root / "tests" / "test_t096_target.py", "def test_target():\n    assert True\n")
    _write(root / "tests" / "conftest.py", "pytest_plugins = []\n")
    payload = _request(root, planning_dir)
    payload["task_id"] = "T096"
    payload["task"] = "T096"
    payload["files_to_change"] = [
        "backend/api/v1/services/target.py",
        "tests/test_t096_target.py",
    ]
    _install_fake_post_chat(
        monkeypatch,
        [
            ("read_file_partial", {"path": "backend/api/v1/services/neighbor.py", "offset": 0, "limit": 200}),
            ("read_file_partial", {"path": "tests/conftest.py", "offset": 0, "limit": 200}),
            ("write_new_file", {"path": "backend/api/v1/services/target.py", "content": "VALUE = 2\n"}),
            ("finish_execution", {"summary": "updated target"}),
        ],
    )

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    manifest = json.loads((planning_dir / ".execution_tool_manifest.json").read_text(encoding="utf-8"))
    assert manifest["read_scope_widenings"][0]["path"] == "backend/api/v1/services/neighbor.py"
    assert manifest["read_scope_widenings"][1]["path"] == "tests/conftest.py"
    assert "backend/api/v1/services/neighbor.py" in manifest["read_only_files"]
    assert runner.READ_SCOPE_WIDEN_FILENAME in result["artifacts"]
    widen_lines = (planning_dir / runner.READ_SCOPE_WIDEN_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(widen_lines) == 2


def test_openai_tool_use_auto_widen_keeps_write_scope_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    _write(root / "sample.txt", "before\n")
    _write(root / "tests" / "test_t096_existing.py", "ORIGINAL = True\n")
    payload = _request(root, planning_dir)
    payload["task_id"] = "T096"
    payload["task"] = "T096"
    _install_fake_post_chat(
        monkeypatch,
        [
            ("read_file_partial", {"path": "tests/test_t096_existing.py", "offset": 0, "limit": 200}),
            ("write_new_file", {"path": "tests/test_t096_existing.py", "content": "ORIGINAL = False\n"}),
        ],
    )

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "PATH_OUT_OF_SCOPE"
    assert (root / "tests" / "test_t096_existing.py").read_text(encoding="utf-8") == "ORIGINAL = True\n"


def test_openai_tool_use_read_scope_exhaustion_writes_stall_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    _write(root / "sample.txt", "before\n")
    _write(root / "tests" / "test_t096_existing.py", "def test_existing():\n    assert True\n")
    payload = _request(root, planning_dir)
    payload["task_id"] = "T096"
    payload["task"] = "T096"
    config = _config(root, planning_dir, monkeypatch)
    config.runtime_caps["max_read_scope_widenings"] = 0
    _install_fake_post_chat(
        monkeypatch,
        [("read_file_partial", {"path": "tests/test_t096_existing.py", "offset": 0, "limit": 200})],
    )

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "READ_SCOPE_EXHAUSTED"
    assert result["stall_report"]["read_scope_exhausted"] is True
    assert runner.STALL_REPORT_FILENAME in result["artifacts"]


def test_openai_tool_use_rejects_tasks_that_require_patch_or_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")

    payload = _request(root, planning_dir)
    payload["capabilities"] = ["patch.apply", "shell.exec"]

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "OPENAI_TOOL_USE_TASK_UNSUITABLE"


def test_openai_tool_use_allows_markdown_bash_verify_fence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")

    payload = _request(root, planning_dir)
    payload["task_requirements"] = "Verification:\n```bash\npython -m pytest tests/test_sample.py -q\n```"

    assert runner._openai_tool_use_unsuitable_reasons(payload) == []


def test_openai_tool_use_read_partial_uses_bounded_slice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_bytes(b"abcdef\n")

    runtime = runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.txt"],
    )

    result = runtime.execute_tool("read_file_partial", {"path": "sample.txt", "offset": 2, "limit": 3})

    assert result["content"] == "cde"
    assert result["truncated"] is True
    assert result["size_bytes"] == 7


def test_tool_observation_progress_keeps_independent_read_parsing() -> None:
    runtime = SimpleNamespace(observed_hashes=set(), read_progress_ends={}, read_progress_windows=set())

    assert runner._tool_observation_made_progress(
        runtime,
        "read_file_partial",
        {"ok": True, "path": "sample.txt", "offset": "bad", "content_bytes": "3"},
    )
    assert runtime.read_progress_ends["sample.txt"] == 3


def test_tool_observation_progress_tracks_unique_hash_and_search_results() -> None:
    runtime = SimpleNamespace(observed_hashes=set(), read_progress_ends={}, read_progress_windows=set())

    hash_result = {"ok": True, "path": "sample.txt", "sha256": "abc"}
    assert runner._tool_observation_made_progress(runtime, "get_file_hash", hash_result)
    assert not runner._tool_observation_made_progress(runtime, "get_file_hash", hash_result)

    search_result = {
        "ok": True,
        "path": "sample.txt",
        "query": "needle",
        "sha256": "def",
        "match_count_returned": 1,
    }
    assert runner._tool_observation_made_progress(runtime, "search_file", search_result)
    assert not runner._tool_observation_made_progress(runtime, "search_file", search_result)


def test_tool_observation_progress_falls_back_to_content_bytes() -> None:
    runtime = SimpleNamespace(observed_hashes=set(), read_progress_ends={}, read_progress_windows=set())

    assert runner._tool_observation_made_progress(
        runtime,
        "read_file",
        {"ok": True, "path": "sample.txt", "offset": 2, "content_bytes": "bad", "content": "å"},
    )
    assert runtime.read_progress_ends["sample.txt"] == 4


def test_tool_observation_progress_counts_unique_shorter_read_window() -> None:
    runtime = SimpleNamespace(observed_hashes=set(), read_progress_ends={"sample.txt": 10}, read_progress_windows=set())

    assert runner._tool_observation_made_progress(
        runtime,
        "read_file_partial",
        {"ok": True, "path": "sample.txt", "offset": 0, "content_bytes": 4, "content_sha256": "slice-a"},
    )
    assert runtime.read_progress_ends["sample.txt"] == 10


def test_tool_observation_progress_ignores_repeated_read_window() -> None:
    runtime = SimpleNamespace(
        observed_hashes=set(),
        read_progress_ends={"sample.txt": 10},
        read_progress_windows={"sample.txt\0" "0\0" "4\0slice-a"},
    )

    assert not runner._tool_observation_made_progress(
        runtime,
        "read_file_partial",
        {"ok": True, "path": "sample.txt", "offset": 0, "content_bytes": 4, "content_sha256": "slice-a"},
    )
    assert runtime.read_progress_ends["sample.txt"] == 10


def test_process_single_tool_call_does_not_call_observation_when_write_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Runtime:
        changed_paths: set[str] = set()
        finish_seen = False

        def execute_tool(self, _tool_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            self.changed_paths.add("sample.txt")
            return {"ok": True, "path": "sample.txt"}

        def log_tool_call(self, **_kwargs: Any) -> None:
            return None

    class _Detector:
        def record_tool_call(self, _tool_name: str, _arguments: dict[str, Any]) -> None:
            return None

        def record_tool_result(self, _tool_name: str, _result: dict[str, Any]) -> None:
            return None

    monkeypatch.setattr(
        runner,
        "_tool_observation_made_progress",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("observation should be short-circuited")),
    )

    call = _tool_response("write_new_file", {"path": "sample.txt"}, 1)["choices"][0]["message"]["tool_calls"][0]
    outcome = runner._process_single_tool_call(_Runtime(), call, 1, _Detector())

    assert outcome.write_progress is True
    assert outcome.observation_progress is False
    assert outcome.tool_result_message is not None


def test_process_single_tool_call_returns_message_for_recoverable_error() -> None:
    class _Runtime:
        changed_paths: set[str] = set()
        finish_seen = False

        def execute_tool(self, _tool_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
            raise runner.OpenAIToolUseExecutionError("PATH_OUT_OF_SCOPE", "blocked")

        def log_tool_call(self, **_kwargs: Any) -> None:
            return None

    class _Detector:
        def record_tool_call(self, _tool_name: str, _arguments: dict[str, Any]) -> None:
            return None

        def record_tool_result(self, _tool_name: str, _result: dict[str, Any]) -> None:
            raise AssertionError("recoverable errors do not record normal tool results")

    call = _tool_response("read_file", {"path": "../secret.txt"}, 1)["choices"][0]["message"]["tool_calls"][0]
    outcome = runner._process_single_tool_call(_Runtime(), call, 1, _Detector())

    assert outcome.finish_result is None
    assert outcome.write_progress is False
    assert outcome.observation_progress is False
    assert outcome.tool_result_message is not None
    assert json.loads(outcome.tool_result_message["content"])["error_code"] == "PATH_OUT_OF_SCOPE"


def test_openai_tool_use_soft_budget_does_not_abort_when_executor_makes_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("write_new_file", {"path": "sample.txt", "content": "after\n"}),
            ("finish_execution", {"summary": "updated under soft budget pressure"}),
        ],
    )
    config = _config(root, planning_dir, monkeypatch)
    config.runtime_caps["max_token_budget"] = 1
    config.runtime_caps["max_hard_token_budget"] = 1_000_000
    config.runtime_caps["max_no_write_iterations_under_budget_pressure"] = 1

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "PASS"
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"
    assert not (planning_dir / runner.STALL_REPORT_FILENAME).exists()


def test_openai_tool_use_budget_pressure_without_writes_emits_stall_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("read_file_partial", {"path": "sample.txt", "offset": 0, "limit": 4}),
            ("read_file_partial", {"path": "sample.txt", "offset": 0, "limit": 4}),
            ("read_file_partial", {"path": "sample.txt", "offset": 0, "limit": 4}),
        ],
    )
    config = _config(root, planning_dir, monkeypatch)
    config.runtime_caps["max_token_budget"] = 1
    config.runtime_caps["max_hard_token_budget"] = 1_000_000
    config.runtime_caps["max_no_write_iterations_under_budget_pressure"] = 1
    config.runtime_caps["max_redundant_read_count"] = 20

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "EXECUTOR_STALLED_BUDGET_PRESSURE"
    stall_report = json.loads((planning_dir / runner.STALL_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert stall_report["budget_pressure"] is True
    assert stall_report["counters"]["no_write_iterations"] >= 2
    assert stall_report["decision_checkpoint"]["mode"] == "action_only"


def test_openai_tool_use_context_overflow_emits_stall_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")

    def _fake(**_kwargs: Any) -> dict[str, Any]:
        raise runner.OpenAIToolUseExecutionError(
            "EXECUTOR_STALLED_CONTEXT_OVERFLOW",
            "http 400: context_length_exceeded",
        )

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "EXECUTOR_STALLED_CONTEXT_OVERFLOW"
    stall_report = json.loads((planning_dir / runner.STALL_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert stall_report["error_code"] == "EXECUTOR_STALLED_CONTEXT_OVERFLOW"


def test_recent_tool_calls_filters_to_current_run_id(tmp_path: Path) -> None:
    log_path = tmp_path / runner.TOOL_CALL_LOG_FILENAME
    rows = [
        {"run_id": "old", "iteration": 99, "tool": "read_file_partial"},
        {"run_id": "current", "iteration": 1, "tool": "get_file_hash"},
        {"run_id": "old", "iteration": 100, "tool": "str_replace"},
        {"run_id": "current", "iteration": 2, "tool": "read_file_partial"},
        {"run_id": "current", "iteration": 3, "tool": "search_file"},
    ]
    log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    recent = runner._recent_tool_calls(log_path, limit=2, run_id="current")

    assert [item["run_id"] for item in recent] == ["current", "current"]
    assert [item["iteration"] for item in recent] == [2, 3]


def test_openai_tool_use_search_file_returns_offsets_and_excerpts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("alpha\nNeedle one\nbeta\nneedle two\n", encoding="utf-8")

    runtime = runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload={**_request(root, planning_dir), "execution_protocol": "exact_str_replace_v1"},
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.txt"],
    )

    result = runtime.execute_tool(
        "search_file",
        {"path": "sample.txt", "query": "needle", "case_sensitive": False, "max_matches": 5, "context_chars": 20},
    )

    assert result["ok"] is True
    assert result["match_count_returned"] == 2
    assert [match["line"] for match in result["matches"]] == [2, 4]
    assert "Needle one" in result["matches"][0]["excerpt"]


def test_openai_tool_use_search_file_call_budget_is_per_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["max_same_tool_calls_per_path"] = 1
    runtime = runner.ToolUseRuntime(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload={**_request(root, planning_dir), "execution_protocol": "exact_str_replace_v1"},
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.txt"],
    )

    runtime.execute_tool("search_file", {"path": "sample.txt", "query": "alpha"})
    runtime.execute_tool("search_file", {"path": "sample.txt", "query": "beta"})

    with pytest.raises(runner.OpenAIToolUseExecutionError) as exc:
        runtime.execute_tool("search_file", {"path": "sample.txt", "query": "alpha"})
    assert exc.value.code == "MAX_SAME_TOOL_CALLS_PER_PATH"


@pytest.mark.parametrize(
    ("tool_name", "method_name", "arguments"),
    [
        ("list_files_in_dir", "_list_files_in_dir", {"dir": "sample.txt"}),
        ("read_file", "_read_file", {"dir": "sample.txt"}),
        ("read_file_partial", "_read_file", {"dir": "sample.txt", "offset": 0}),
        ("get_file_hash", "_get_file_hash", {"dir": "sample.txt"}),
        ("search_file", "_search_file", {"dir": "sample.txt", "query": "alpha"}),
        ("str_replace", "_str_replace", {"dir": "sample.txt", "old_text": "a", "new_text": "b"}),
        ("write_new_file", "_write_file", {"dir": "sample.txt", "content": "b"}),
        ("delete_file", "_delete_file", {"dir": "sample.txt"}),
    ],
)
def test_execute_tool_dispatch_preserves_dir_path_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    method_name: str,
    arguments: dict[str, Any],
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    runtime = _runtime(root, planning_dir, monkeypatch)
    monkeypatch.setattr(runtime, "active_tools", lambda: [tool_name])
    captured: list[str] = []

    def _fake(path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(path)
        return {"ok": True}

    monkeypatch.setattr(runtime, method_name, _fake)

    assert runtime.execute_tool(tool_name, arguments)["ok"] is True
    assert captured == ["sample.txt"]


def test_execute_tool_checks_active_tools_before_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    runtime = _runtime(root, planning_dir, monkeypatch)
    monkeypatch.setattr(runtime, "active_tools", lambda: [])

    with pytest.raises(runner.OpenAIToolUseExecutionError) as exc:
        runtime.execute_tool("list_allowed_files", {})

    assert exc.value.code == "TOOL_FORBIDDEN"


def test_write_new_file_dispatch_reads_execution_protocol_at_call_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    runtime = _runtime(root, planning_dir, monkeypatch)
    state = {"protocol": runner.FULL_FILE_PROTOCOL}
    captured: list[bool] = []
    dispatch = runtime._tool_dispatch
    state["protocol"] = runner.EXACT_STR_REPLACE_PROTOCOL
    monkeypatch.setattr(runtime, "execution_protocol", lambda: state["protocol"])
    monkeypatch.setattr(
        runtime,
        "_write_file",
        lambda _path, _content, *, require_missing: captured.append(require_missing) or {"ok": True},
    )

    assert dispatch["write_new_file"]({"path": "sample.txt", "content": "x"})["ok"] is True
    assert captured == [True]


def test_openai_tool_use_compacts_old_read_results_before_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    config = _config(root, planning_dir, monkeypatch)
    config.runtime_caps["max_full_read_tool_results"] = 2
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
        runner._tool_result_message(
            "call_1",
            "read_file_partial",
            {"ok": True, "path": "sample.txt", "offset": 0, "content": "a" * 1000, "content_bytes": 1000, "size_bytes": 3000},
        ),
        runner._tool_result_message(
            "call_2",
            "read_file_partial",
            {"ok": True, "path": "sample.txt", "offset": 1000, "content": "b" * 1000, "content_bytes": 1000, "size_bytes": 3000},
        ),
        runner._tool_result_message(
            "call_3",
            "search_file",
            {"ok": True, "path": "sample.txt", "query": "needle", "matches": [{"offset": 2, "excerpt": "needle"}]},
        ),
    ]

    payload_messages = runner._messages_for_payload(messages, config)

    assert all("_workflow_tool_name" not in message for message in payload_messages)
    first_tool = json.loads(payload_messages[2]["content"])
    second_tool = json.loads(payload_messages[3]["content"])
    third_tool = json.loads(payload_messages[4]["content"])
    assert first_tool["content_omitted"] is True
    assert "content" not in first_tool
    assert second_tool["content"] == "b" * 1000
    assert third_tool["matches"][0]["excerpt"] == "needle"


def test_openai_tool_use_compacts_read_results_by_byte_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    config = _config(root, planning_dir, monkeypatch)
    config.runtime_caps["max_full_read_tool_results"] = 4
    config.runtime_caps["max_full_read_tool_result_bytes"] = 900
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "user"},
        runner._tool_result_message("call_1", "read_file_partial", {"ok": True, "path": "a.py", "content": "a" * 800}),
        runner._tool_result_message("call_2", "read_file_partial", {"ok": True, "path": "b.py", "content": "b" * 800}),
        runner._tool_result_message("call_3", "read_file_partial", {"ok": True, "path": "c.py", "content": "c" * 800}),
    ]

    payload_messages = runner._messages_for_payload(messages, config)

    assert json.loads(payload_messages[2]["content"])["content_omitted"] is True
    assert json.loads(payload_messages[3]["content"])["content_omitted"] is True
    assert json.loads(payload_messages[4]["content"])["content"] == "c" * 800


def test_openai_tool_use_waf_retry_compacts_tool_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    config = _config(root, planning_dir, monkeypatch)
    config.runtime_caps["max_waf_retries"] = 1
    seen_payloads: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> dict[str, Any]:
        payload = kwargs["payload"]
        seen_payloads.append(payload)
        call_index = len(seen_payloads)
        if call_index == 1:
            return _tool_response("read_file", {"path": "sample.txt"}, call_index)
        if call_index == 2:
            raise runner.OpenAIToolUseExecutionError("HTTP_WAF_BLOCKED", "http 403: MiWAF")
        if call_index == 3:
            tool_messages = [message for message in payload["messages"] if message.get("role") == "tool"]
            assert tool_messages
            assert json.loads(tool_messages[-1]["content"])["content_omitted"] is True
            return _tool_response("write_new_file", {"path": "sample.txt", "content": "after\n"}, call_index)
        return _tool_response("finish_execution", {"summary": "updated sample"}, call_index)

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "PASS"
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"
    assert len(seen_payloads) == 4


def test_openai_tool_use_compacts_bulky_request_prompt() -> None:
    request = {
        "task_requirements": "P" * 10_000,
        "must_fix": ["traceback " + "x" * 5_000],
        "task_card": {
            "recovery": {
                "base_workspace_path": "E:/project/.workflow/.executor_scratch/run/workspace",
                "must_fix": ["error " + "y" * 5_000],
            },
            "patch_plan": [
                {
                    "id": "patch_1",
                    "path": "sample.py",
                    "old_text": "old" * 1000,
                    "new_text": "new" * 1000,
                }
            ],
        },
    }

    compact = runner._request_for_prompt(request)

    assert len(compact["task_requirements"]) < 3_000
    assert len(compact["must_fix"][0]) < 1_500
    assert "base_workspace_path" not in compact["task_card"]["recovery"]
    assert "old_text" not in compact["task_card"]["patch_plan"][0]
    assert compact["task_card"]["patch_plan"][0]["old_text_bytes"] == 3000


def test_openai_tool_use_user_prompt_lists_missing_writable_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    runtime = _runtime(root, planning_dir, monkeypatch, allowed_files=["sample.txt"])

    prompt = runner._user_prompt(runtime)

    assert "Missing writable files requiring write_new_file" in prompt
    assert "sample.txt" in prompt


def test_openai_tool_use_system_prompt_includes_executor_profile_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    _write(
        root / ".claude" / "workflow" / "prompts.yaml",
        textwrap.dedent(
            """
        profiles:
          executor_kernel:
            text: Executor kernel profile.
          executor_overlays:
            mimo:
              text: Mimo executor writes before extra reads.
        """,
        ),
    )
    runtime = _runtime(root, planning_dir, monkeypatch, allowed_files=["sample.txt"])

    prompt = runner._system_prompt("exact_str_replace_v1", runtime=runtime)

    assert "Prompt profile directives (executor/mimo):" in prompt
    assert "Executor kernel profile." in prompt
    assert "Mimo executor writes before extra reads." in prompt


def test_openai_tool_use_nudge_policy_uses_prompt_profile_when_cap_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    _write(
        root / ".claude" / "workflow" / "prompts.yaml",
        textwrap.dedent(
            """
        profiles:
          nudge_policies:
            mimo:
              no_write_after_iter: 2
              missing_writable_remind_every: 2
        """,
        ),
    )
    runtime = _runtime(root, planning_dir, monkeypatch, allowed_files=["sample.txt"])

    assert runner._should_send_write_progress_nudge(runtime, iteration=1, last_sent_iteration=0) is False
    assert runner._should_send_write_progress_nudge(runtime, iteration=2, last_sent_iteration=0) is True
    assert runner._should_send_write_progress_nudge(runtime, iteration=3, last_sent_iteration=2) is False
    assert runner._should_send_write_progress_nudge(runtime, iteration=4, last_sent_iteration=2) is True


def test_openai_tool_use_explicit_runtime_cap_overrides_profile_nudge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    _write(
        root / ".claude" / "workflow" / "prompts.yaml",
        textwrap.dedent(
            """
        profiles:
          nudge_policies:
            mimo:
              no_write_after_iter: 2
        """,
        ),
    )
    runtime = _runtime(root, planning_dir, monkeypatch, allowed_files=["sample.txt"])
    runtime.config.runtime_caps["write_progress_nudge_iteration"] = 5
    runtime.config.runtime_caps["max_no_write_iterations"] = 8

    assert runner._should_send_write_progress_nudge(runtime, iteration=2, last_sent_iteration=0) is False
    assert runner._should_send_write_progress_nudge(runtime, iteration=5, last_sent_iteration=0) is True


def test_openai_tool_use_nudges_no_write_executor_before_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "context.txt").write_text("".join(f"line {idx}\n" for idx in range(200)), encoding="utf-8")
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.txt"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {"new_files": ["sample.txt"], "read_only_files": ["context.txt"]}
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["write_progress_nudge_iteration"] = 4
    config.runtime_caps["max_no_write_iterations"] = 8
    config.runtime_caps["max_no_progress_iterations"] = 10
    seen_nudge = {"value": False}
    calls = {"count": 0}

    def _fake(**kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        payload_messages = kwargs["payload"]["messages"]
        if any(
            message.get("role") == "user" and "No writable file has changed" in str(message.get("content") or "")
            for message in payload_messages
        ):
            seen_nudge["value"] = True
        if calls["count"] <= 4:
            return _tool_response(
                "read_file_partial",
                {"path": "context.txt", "offset": (calls["count"] - 1) * 100, "limit": 80},
                calls["count"],
            )
        if calls["count"] == 5:
            assert seen_nudge["value"] is True
            return _tool_response("write_new_file", {"path": "sample.txt", "content": "after\n"}, calls["count"])
        return _tool_response("finish_execution", {"summary": "created after nudge"}, calls["count"])

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert seen_nudge["value"] is True
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"


def test_openai_tool_use_blocks_when_patch_required_mode_keeps_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "context.txt").write_text("".join(f"line {idx}\n" for idx in range(200)), encoding="utf-8")
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.txt"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {"new_files": ["sample.txt"], "read_only_files": ["context.txt"]}
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["patch_plan_required_iteration"] = 2
    config.runtime_caps["max_patch_plan_required_read_iterations"] = 1
    config.runtime_caps["max_no_write_iterations"] = 8
    config.runtime_caps["max_no_progress_iterations"] = 10
    seen_patch_required = {"value": False}
    calls = {"count": 0}

    def _fake(**kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        payload_messages = kwargs["payload"]["messages"]
        if any(
            message.get("role") == "user" and "Patch-plan discipline is now required" in str(message.get("content") or "")
            for message in payload_messages
        ):
            seen_patch_required["value"] = True
        return _tool_response(
            "read_file_partial",
            {"path": "context.txt", "offset": 0, "limit": 20},
            calls["count"],
        )

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"
    assert seen_patch_required["value"] is True
    stall = json.loads((planning_dir / runner.STALL_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert stall["error_code"] == "EXECUTOR_STALLED_PATCH_PLAN_REQUIRED"


def test_openai_tool_use_patch_required_allows_new_observation_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "context.txt").write_text("".join(f"line {idx}\n" for idx in range(200)), encoding="utf-8")
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.txt"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {"new_files": ["sample.txt"], "read_only_files": ["context.txt"]}
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["patch_plan_required_iteration"] = 2
    config.runtime_caps["max_patch_plan_required_read_iterations"] = 1
    config.runtime_caps["max_no_write_iterations"] = 8
    config.runtime_caps["max_no_progress_iterations"] = 10
    seen_patch_required = {"value": False}
    calls = {"count": 0}

    def _fake(**kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        payload_messages = kwargs["payload"]["messages"]
        if any(
            message.get("role") == "user" and "Patch-plan discipline is now required" in str(message.get("content") or "")
            for message in payload_messages
        ):
            seen_patch_required["value"] = True
        if calls["count"] <= 4:
            return _tool_response(
                "read_file_partial",
                {"path": "context.txt", "offset": (calls["count"] - 1) * 20, "limit": 20},
                calls["count"],
            )
        if calls["count"] == 5:
            return _tool_response("write_new_file", {"path": "sample.txt", "content": "after\n"}, calls["count"])
        return _tool_response("finish_execution", {"summary": "created after targeted reads"}, calls["count"])

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert seen_patch_required["value"] is True
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"


def test_openai_tool_use_action_only_checkpoint_keeps_protocol_write_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    payload = {**_request(root, planning_dir), "execution_protocol": "exact_str_replace_v1"}
    runtime = _runtime(root, planning_dir, monkeypatch, request_payload=payload, execution_protocol="exact_str_replace_v1")
    runtime.action_only_mode = True

    tools = set(runtime.active_tools())
    assert {
        "list_allowed_files",
        "get_file_hash",
        "list_patch_plan",
        "apply_patch_plan_item",
        "str_replace",
        "write_new_file",
        "finish_execution",
        "declare_task_infeasible",
    } <= tools
    assert "read_file_partial" not in tools
    assert "search_file" not in tools
    assert "list_files_in_dir" not in tools

    full_runtime = _runtime(root, planning_dir, monkeypatch, request_payload=_request(root, planning_dir), execution_protocol="full_file_v1")
    full_runtime.action_only_mode = True
    full_tools = set(full_runtime.active_tools())
    assert {"list_allowed_files", "write_new_file", "delete_file", "finish_execution", "declare_task_infeasible"} <= full_tools
    assert "str_replace" not in full_tools
    assert "get_file_hash" not in full_tools


def test_openai_tool_use_action_only_checkpoint_allows_write_after_stall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "context.txt").write_text("".join(f"line {idx}\n" for idx in range(50)), encoding="utf-8")
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.txt"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {"new_files": ["sample.txt"], "read_only_files": ["context.txt"]}
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["max_no_write_iterations"] = 1
    config.runtime_caps["max_no_progress_iterations"] = 10
    config.runtime_caps["max_redundant_read_count"] = 20
    config.runtime_caps["patch_plan_required_iteration"] = 99
    config.runtime_caps["max_no_write_iterations_with_observation"] = 1
    calls = {"count": 0}
    saw_action_only = {"value": False}

    def _fake(**kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        tool_names = {
            item["function"]["name"]
            for item in list(kwargs["payload"].get("tools") or [])
            if isinstance(item, dict) and isinstance(item.get("function"), dict)
        }
        if calls["count"] <= 2:
            assert "read_file_partial" in tool_names
            return _tool_response("read_file_partial", {"path": "context.txt", "offset": 0, "limit": 20}, calls["count"])
        if calls["count"] == 3:
            saw_action_only["value"] = True
            assert "read_file_partial" not in tool_names
            assert "search_file" not in tool_names
            assert "write_new_file" in tool_names
            assert "finish_execution" in tool_names
            payload_messages = kwargs["payload"]["messages"]
            assert any("Executor decision checkpoint" in str(message.get("content") or "") for message in payload_messages)
            return _tool_response("write_new_file", {"path": "sample.txt", "content": "after\n"}, calls["count"])
        return _tool_response("finish_execution", {"summary": "created after checkpoint"}, calls["count"])

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert saw_action_only["value"] is True
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"


def test_openai_tool_use_action_only_checkpoint_refuses_more_reads_with_original_stall_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "context.txt").write_text("".join(f"line {idx}\n" for idx in range(50)), encoding="utf-8")
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.txt"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {"new_files": ["sample.txt"], "read_only_files": ["context.txt"]}
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["max_no_write_iterations"] = 1
    config.runtime_caps["max_no_progress_iterations"] = 10
    config.runtime_caps["max_redundant_read_count"] = 20
    config.runtime_caps["patch_plan_required_iteration"] = 99
    config.runtime_caps["max_no_write_iterations_with_observation"] = 1
    calls = {"count": 0}

    def _fake(**_kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        return _tool_response("read_file_partial", {"path": "context.txt", "offset": 0, "limit": 20}, calls["count"])

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "EXECUTOR_STALLED_NO_WRITE_PROGRESS"
    stall = json.loads((planning_dir / runner.STALL_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert stall["decision_checkpoint"]["mode"] == "action_only"
    assert stall["decision_checkpoint"]["attempts"] == 1


def test_openai_tool_use_blocks_allowed_symlink_outside_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        (root / "sample.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "PATH_GUARD_BLOCKED"


def test_openai_tool_use_blocks_commit_when_project_changed_during_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    index = {"value": 0}

    def _fake(**_kwargs: Any) -> dict[str, Any]:
        i = index["value"]
        index["value"] += 1
        if i == 0:
            return _tool_response("write_new_file", {"path": "sample.txt", "content": "after\n"}, i)
        (root / "sample.txt").write_text("user edit\n", encoding="utf-8")
        return _tool_response("finish_execution", {"summary": "finish"}, i)

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "PROJECT_CHANGED_DURING_EXECUTION"
    assert (root / "sample.txt").read_text(encoding="utf-8") == "user edit\n"


def test_openai_tool_use_retries_after_verify_failure_before_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    verify = "python -c \"from pathlib import Path; assert Path('sample.txt').read_text() == 'after\\n'\""
    _install_fake_post_chat(
        monkeypatch,
        [
            ("write_new_file", {"path": "sample.txt", "content": "bad\n"}),
            ("finish_execution", {"summary": "first try"}),
            ("write_new_file", {"path": "sample.txt", "content": "after\n"}),
            ("finish_execution", {"summary": "second try"}),
        ],
    )

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir, verify_cmd=verify),
    )

    assert result["status"] == "PASS"
    assert (root / "sample.txt").read_text(encoding="utf-8") == "after\n"


def test_openai_tool_use_passes_verify_timeout_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")
    config = _config(root, planning_dir, monkeypatch)
    config.runtime_caps["verify_timeout_seconds"] = 321
    captured: dict[str, Any] = {}

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        captured["timeout_seconds"] = kwargs.get("timeout_seconds")
        return {"status": "PASS", "passed": True, "summary": "ok"}

    monkeypatch.setattr(runner, "maybe_execute_verify_command", fake_verify)
    _install_fake_post_chat(
        monkeypatch,
        [
            ("write_new_file", {"path": "sample.txt", "content": "after\n"}),
            ("finish_execution", {"summary": "updated sample"}),
        ],
    )

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir, verify_cmd="pytest -q"),
    )

    assert result["status"] == "PASS"
    assert captured["timeout_seconds"] == 321


def test_openai_tool_use_verification_only_empty_scope_runs_verify_without_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    payload = _request(root, planning_dir, verify_cmd="python -m pytest tests/test_existing.py -q")
    payload["files_to_change"] = []
    payload["task_card"] = {
        "files_to_change": [],
        "verify_cmd": "python -m pytest tests/test_existing.py -q",
        "execution_constraints": {
            "verification_only_noop": True,
            "executor_must_not_edit": True,
        },
        "related_existing_tests": ["tests/test_existing.py"],
    }
    captured: dict[str, Any] = {}

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "PASS", "passed": True, "stdout_excerpt": "1 passed"}

    def fail_post_chat(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("verification-only task should not call the model transport")

    monkeypatch.setattr(runner, "maybe_execute_verify_command", fake_verify)
    monkeypatch.setattr(runner, "_post_chat", fail_post_chat)

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["changed_files"] == []
    assert result["verification_only_noop"] is True
    assert result["verify_summary"]["passed"] is True
    assert captured["changed_files"] == ["tests/test_existing.py"]


def test_openai_tool_use_accepts_preexisting_recovery_state_when_verify_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("already done\n", encoding="utf-8")
    payload = _request(root, planning_dir, verify_cmd="pytest tests/test_sample.py -q")
    payload["task_card"] = {
        "files_to_change": ["sample.txt"],
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "executor_no_write_stall_retry",
        },
    }
    captured: dict[str, Any] = {}

    def fake_verify(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "PASS", "passed": True, "stdout_excerpt": "1 passed"}

    def fail_post_chat(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("model transport should not be called")

    monkeypatch.setattr(runner, "maybe_execute_verify_command", fake_verify)
    monkeypatch.setattr(runner, "_post_chat", fail_post_chat)

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["changed_files"] == ["sample.txt"]
    assert result["verify_summary"]["passed"] is True
    assert captured["project_root"] == root.resolve()
    assert captured["changed_files"] == ["sample.txt"]


def test_openai_tool_use_exact_str_replace_protocol_patches_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("def value():\n    return 'original'\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("get_file_hash", {"path": "sample.py"}),
            (
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "return 'original'",
                    "new_text": "return 'updated'",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
            ),
            ("finish_execution", {"summary": "patched sample"}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["execution_protocol"] == "exact_str_replace_v1"
    assert result["changed_files"] == ["sample.py"]
    assert (root / "sample.py").read_text(encoding="utf-8") == "def value():\n    return 'updated'\n"
    manifest = json.loads((planning_dir / ".execution_tool_manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution_protocol"] == "exact_str_replace_v1"
    assert "list_allowed_files" in manifest["tools"]
    assert "list_files_in_dir" in manifest["tools"]
    assert "search_file" in manifest["tools"]
    assert "str_replace" in manifest["tools"]
    assert "write_new_file" in manifest["tools"]
    patch_lines = (planning_dir / runner.PATCH_ATTEMPTS_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(patch_lines) == 1
    assert json.loads(patch_lines[0])["status"] == "PASS"


def test_openai_tool_use_exact_str_replace_protocol_creates_missing_allowed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    _install_fake_post_chat(
        monkeypatch,
        [
            ("list_files_in_dir", {"dir": "backend/db/migration_sql"}),
            (
                "write_new_file",
                {
                    "path": "backend/db/migration_sql/20260429_020_admin_operation_audit.sql",
                    "content": "CREATE TABLE admin_operation_audit (id INTEGER PRIMARY KEY);\n",
                },
            ),
            ("finish_execution", {"summary": "created migration"}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["backend/db/migration_sql/20260429_020_admin_operation_audit.sql"]
    payload["execution_protocol"] = "exact_str_replace_v1"

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    created = root / "backend" / "db" / "migration_sql" / "20260429_020_admin_operation_audit.sql"
    assert result["status"] == "PASS"
    assert result["changed_files"] == ["backend/db/migration_sql/20260429_020_admin_operation_audit.sql"]
    assert created.read_text(encoding="utf-8") == "CREATE TABLE admin_operation_audit (id INTEGER PRIMARY KEY);\n"


def test_openai_tool_use_exact_str_replace_protocol_refuses_write_new_file_for_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("write_new_file", {"path": "sample.py", "content": "VALUE = 'bad'\n"}),
            (
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "VALUE = 'old'\n",
                    "new_text": "VALUE = 'new'\n",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
            ),
            ("finish_execution", {"summary": "patched sample"}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert (root / "sample.py").read_text(encoding="utf-8") == "VALUE = 'new'\n"
    log_lines = (planning_dir / runner.TOOL_CALL_LOG_FILENAME).read_text(encoding="utf-8").splitlines()
    first = json.loads(log_lines[0])
    assert first["tool"] == "write_new_file"
    assert first["result"]["error_code"] == "WRITE_NEW_FILE_EXISTS"


def test_openai_tool_use_auto_finishes_after_write_progress_stalls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("def value():\n    return 'original'\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")
    _install_fake_post_chat(
        monkeypatch,
        [
            (
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "return 'original'",
                    "new_text": "return 'updated'",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
            ),
            ("read_file_partial", {"path": "sample.py", "offset": 0, "limit": 20}),
            ("read_file_partial", {"path": "sample.py", "offset": 20, "limit": 40}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["max_no_write_iterations"] = 1
    config.runtime_caps["max_no_progress_iterations"] = 10

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["changed_files"] == ["sample.py"]
    assert result["implementer_note"]["claimed_intent"].startswith("Runtime auto-finished")
    assert (root / "sample.py").read_text(encoding="utf-8") == "def value():\n    return 'updated'\n"


def test_openai_tool_use_auto_finishes_after_http_interruption_with_scratch_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")
    calls = {"count": 0}

    def fake_post_chat(**_kwargs: Any) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            return _tool_response(
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "VALUE = 'old'",
                    "new_text": "VALUE = 'new'",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
                0,
            )
        raise runner.OpenAIToolUseExecutionError("HTTP_ERROR", "Remote end closed connection without response")

    monkeypatch.setattr(runner, "_post_chat", fake_post_chat)
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["implementer_note"]["claimed_intent"].startswith("Runtime auto-finished")
    assert result["verify_summary"]["passed"] is True
    assert (root / "sample.py").read_text(encoding="utf-8") == "VALUE = 'new'\n"


def test_openai_tool_use_exact_protocol_applies_task_card_patch_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("list_patch_plan", {}),
            ("apply_patch_plan_item", {"id": "update_sample"}),
            ("apply_patch_plan_item", {"id": "write_contract"}),
            ("finish_execution", {"summary": "applied patch plan"}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py", "tests/test_contract.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {
        "patch_plan": [
            {
                "id": "update_sample",
                "operation": "str_replace",
                "path": "sample.py",
                "old_text": "VALUE = 'old'\n",
                "new_text": "VALUE = 'new'\n",
                "precondition_sha256": sample_hash,
                "expected_occurrences": 1,
            },
            {
                "id": "write_contract",
                "operation": "write_new_file",
                "path": "tests/test_contract.py",
                "content": "def test_contract():\n    assert True\n",
            },
        ]
    }

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert (root / "sample.py").read_text(encoding="utf-8") == "VALUE = 'new'\n"
    assert (root / "tests" / "test_contract.py").read_text(encoding="utf-8") == "def test_contract():\n    assert True\n"
    manifest = json.loads((planning_dir / ".execution_tool_manifest.json").read_text(encoding="utf-8"))
    assert "list_patch_plan" in manifest["tools"]
    assert "apply_patch_plan_item" in manifest["tools"]


def test_openai_tool_use_auto_applies_recovery_patch_plan_without_model_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")

    def _unexpected_model_call(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("recovery patch plan should be applied by the guarded runtime")

    monkeypatch.setattr(runner, "_post_chat", _unexpected_model_call)
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "narrow_patch_plan",
            "must_fix": ["verify failed"],
        },
        "patch_plan": [
            {
                "id": "update_sample",
                "operation": "str_replace",
                "path": "sample.py",
                "old_text": "VALUE = 'old'\n",
                "new_text": "VALUE = 'new'\n",
                "precondition_sha256": sample_hash,
                "expected_occurrences": 1,
            }
        ],
    }

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["summary"] == "Runtime auto-applied executor recovery patch plan."
    assert (root / "sample.py").read_text(encoding="utf-8") == "VALUE = 'new'\n"
    tool_log = (planning_dir / ".execution_tool_calls.jsonl").read_text(encoding="utf-8")
    assert "runtime_patch_1" in tool_log
    assert "update_sample" in tool_log


def test_openai_tool_use_auto_recovery_keeps_partial_patch_in_scratch_for_verify_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")

    def _unexpected_model_call(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("recovery patch plan should be applied by the guarded runtime")

    monkeypatch.setattr(runner, "_post_chat", _unexpected_model_call)
    payload = _request(root, planning_dir, verify_cmd="python -c \"import sys; sys.exit(1)\"")
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "narrow_patch_plan",
            "must_fix": ["verify failed"],
        },
        "patch_plan": [
            {
                "id": "update_sample",
                "operation": "str_replace",
                "path": "sample.py",
                "old_text": "VALUE = 'old'\n",
                "new_text": "VALUE = 'new'\n",
                "precondition_sha256": sample_hash,
                "expected_occurrences": 1,
            },
            {
                "id": "bad_followup",
                "operation": "str_replace",
                "path": "sample.py",
                "old_text": "missing text\n",
                "new_text": "unreachable\n",
                "precondition_sha256": sample_hash,
                "expected_occurrences": 1,
            },
        ],
    }

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "PATCH_PLAN_PARTIAL_VERIFY_FAILED"
    assert "bad_followup" in result["blocking_reason"]
    assert "PATCH_PRECONDITION_MISMATCH" in result["blocking_reason"]
    assert (root / "sample.py").read_text(encoding="utf-8") == "VALUE = 'old'\n"
    scratch_sample = Path(result["scratch_root"]) / "workspace" / "sample.py"
    assert scratch_sample.read_text(encoding="utf-8") == "VALUE = 'new'\n"
    tool_log = (planning_dir / ".execution_tool_calls.jsonl").read_text(encoding="utf-8")
    assert "bad_followup" in tool_log
    assert "PATCH_PRECONDITION_MISMATCH" in tool_log


def test_openai_tool_use_recovery_can_continue_from_prior_scratch_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    base_workspace = root / ".workflow" / ".executor_scratch" / "previous" / "workspace"
    base_workspace.mkdir(parents=True)
    (base_workspace / "sample.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    base_hash = runner._file_hash(base_workspace / "sample.py")

    def _unexpected_model_call(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("recovery patch plan should be applied by the guarded runtime")

    monkeypatch.setattr(runner, "_post_chat", _unexpected_model_call)
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    payload["task_card"] = {
        "recovery": {
            "schema_version": "execution.recovery_card.v1",
            "source_action": "narrow_patch_plan",
            "base_workspace_path": str(base_workspace),
        },
        "patch_plan": [
            {
                "id": "finish_from_base",
                "operation": "str_replace",
                "path": "sample.py",
                "old_text": "VALUE = 'base'\n",
                "new_text": "VALUE = 'new'\n",
                "precondition_sha256": base_hash,
                "expected_occurrences": 1,
            }
        ],
    }

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert (root / "sample.py").read_text(encoding="utf-8") == "VALUE = 'new'\n"
    assert result["changed_files"] == ["sample.py"]


def test_openai_tool_use_str_replace_treats_already_applied_patch_as_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    old_hash = runner._file_hash(root / "sample.py")
    (root / "sample.py").write_text("VALUE = 'new'\n", encoding="utf-8")
    runtime = runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload={**_request(root, planning_dir), "execution_protocol": "exact_str_replace_v1"},
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.py"],
    )

    result = runtime.execute_tool(
        "str_replace",
        {
            "path": "sample.py",
            "old_text": "VALUE = 'old'\n",
            "new_text": "VALUE = 'new'\n",
            "precondition_sha256": old_hash,
            "expected_occurrences": 1,
        },
    )

    assert result["ok"] is True
    assert result["already_applied"] is True
    patch_log = (planning_dir / ".execution_patch_attempts.jsonl").read_text(encoding="utf-8")
    assert "PATCH_ALREADY_APPLIED" in patch_log


def test_openai_tool_use_str_replace_accepts_lf_patch_for_crlf_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_bytes(b"def value():\r\n    return 'old'\r\n")
    runtime = runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload={**_request(root, planning_dir), "execution_protocol": "exact_str_replace_v1"},
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.py"],
    )

    result = runtime.execute_tool(
        "str_replace",
        {
            "path": "sample.py",
            "old_text": "def value():\n    return 'old'",
            "new_text": "def value():\n    return 'new'",
            "expected_occurrences": 1,
        },
    )

    assert result["ok"] is True
    assert (runtime.workspace / "sample.py").read_bytes() == b"def value():\r\n    return 'new'\r\n"


def test_openai_tool_use_str_replace_accepts_crlf_patch_for_lf_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("def value():\n    return 'old'\n", encoding="utf-8", newline="\n")
    runtime = runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload={**_request(root, planning_dir), "execution_protocol": "exact_str_replace_v1"},
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.py"],
    )

    result = runtime.execute_tool(
        "str_replace",
        {
            "path": "sample.py",
            "old_text": "def value():\r\n    return 'old'",
            "new_text": "def value():\r\n    return 'new'",
            "expected_occurrences": 1,
        },
    )

    assert result["ok"] is True
    assert (runtime.workspace / "sample.py").read_text(encoding="utf-8") == "def value():\n    return 'new'\n"


def test_openai_tool_use_patch_plan_ignores_stale_precondition_when_exact_text_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("VALUE = 'old'\n", encoding="utf-8")
    payload = {
        **_request(root, planning_dir),
        "execution_protocol": "exact_str_replace_v1",
        "task_card": {
            "patch_plan": [
                {
                    "id": "update_sample",
                    "operation": "str_replace",
                    "path": "sample.py",
                    "old_text": "VALUE = 'old'\n",
                    "new_text": "VALUE = 'new'\n",
                    "expected_occurrences": 1,
                    "precondition_sha256": "stale-model-supplied-hash",
                }
            ]
        },
    }
    runtime = runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.py"],
    )

    result = runtime.execute_tool("apply_patch_plan_item", {"id": "update_sample"})

    assert result["ok"] is True
    assert result["precondition_mismatch_ignored"] is True
    assert (runtime.workspace / "sample.py").read_text(encoding="utf-8") == "VALUE = 'new'\n"


def test_apply_patch_plan_item_prefers_id_over_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("A = 1\nB = 1\n", encoding="utf-8")
    payload = {
        **_request(root, planning_dir),
        "execution_protocol": "exact_str_replace_v1",
        "task_card": {
            "patch_plan": [
                {"id": "first", "operation": "str_replace", "path": "sample.py", "old_text": "A = 1\n", "new_text": "A = 2\n"},
                {"id": "second", "operation": "str_replace", "path": "sample.py", "old_text": "B = 1\n", "new_text": "B = 2\n"},
            ]
        },
    }
    runtime = _runtime(root, planning_dir, monkeypatch, request_payload=payload)

    result = runtime.execute_tool("apply_patch_plan_item", {"id": "second", "index": 0})

    assert result["id"] == "second"
    assert (runtime.workspace / "sample.py").read_text(encoding="utf-8") == "A = 1\nB = 2\n"


def test_apply_patch_plan_item_falls_back_to_index_when_id_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("A = 1\n", encoding="utf-8")
    payload = {
        **_request(root, planning_dir),
        "execution_protocol": "exact_str_replace_v1",
        "task_card": {
            "patch_plan": [
                {"id": "first", "operation": "str_replace", "path": "sample.py", "old_text": "A = 1\n", "new_text": "A = 2\n"},
            ]
        },
    }
    runtime = _runtime(root, planning_dir, monkeypatch, request_payload=payload)

    result = runtime.execute_tool("apply_patch_plan_item", {"id": "missing", "index": 0})

    assert result["id"] == "first"


def test_apply_patch_plan_item_reports_invalid_and_missing_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("A = 1\n", encoding="utf-8")
    payload = {
        **_request(root, planning_dir),
        "execution_protocol": "exact_str_replace_v1",
        "task_card": {
            "patch_plan": [
                {"id": "first", "operation": "str_replace", "path": "sample.py", "old_text": "A = 1\n", "new_text": "A = 2\n"},
            ]
        },
    }
    runtime = _runtime(root, planning_dir, monkeypatch, request_payload=payload)

    with pytest.raises(runner.OpenAIToolUseExecutionError) as invalid:
        runtime.execute_tool("apply_patch_plan_item", {"index": "nope"})
    assert invalid.value.code == "PATCH_PLAN_ITEM_INVALID"

    with pytest.raises(runner.OpenAIToolUseExecutionError) as missing:
        runtime.execute_tool("apply_patch_plan_item", {"id": "missing", "index": 9})
    assert missing.value.code == "PATCH_PLAN_ITEM_MISSING"


def test_apply_patch_plan_item_reports_empty_duplicate_and_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("A = 1\n", encoding="utf-8")
    empty_runtime = _runtime(root, planning_dir, monkeypatch)
    with pytest.raises(runner.OpenAIToolUseExecutionError) as empty:
        empty_runtime.execute_tool("apply_patch_plan_item", {"id": "missing"})
    assert empty.value.code == "PATCH_PLAN_MISSING"

    payload = {
        **_request(root, planning_dir),
        "execution_protocol": "exact_str_replace_v1",
        "task_card": {
            "patch_plan": [
                {"id": "first", "operation": "str_replace", "path": "sample.py", "old_text": "A = 1\n", "new_text": "A = 2\n"},
                {"id": "bad", "operation": "move_file", "path": "sample.py"},
            ]
        },
    }
    runtime = _runtime(root, planning_dir, monkeypatch, request_payload=payload)
    assert runtime.execute_tool("apply_patch_plan_item", {"id": "first"})["ok"] is True
    assert runtime.execute_tool("apply_patch_plan_item", {"id": "first"})["already_applied"] is True
    with pytest.raises(runner.OpenAIToolUseExecutionError) as unsupported:
        runtime.execute_tool("apply_patch_plan_item", {"id": "bad"})
    assert unsupported.value.code == "PATCH_PLAN_OPERATION_UNSUPPORTED"


def test_openai_tool_use_user_prompt_summarizes_patch_plan_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("old\n", encoding="utf-8")
    runtime = runner.ToolUseRuntime(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload={
            **_request(root, planning_dir),
            "execution_protocol": "exact_str_replace_v1",
            "task_card": {
                "patch_plan": [
                    {
                        "id": "large_patch",
                        "operation": "str_replace",
                        "path": "sample.py",
                        "old_text": "old\n",
                        "new_text": "x" * 5000,
                    }
                ]
            },
        },
        project_root=root.resolve(),
        planning_dir=planning_dir.resolve(),
        allowed_files=["sample.py"],
    )

    prompt = runner._user_prompt(runtime)

    assert "x" * 100 not in prompt
    assert "new_text_bytes" in prompt
    assert "apply_patch_plan_item" in prompt


def test_openai_tool_use_expanding_reads_count_as_progress_for_patch_protocol(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    filler = "\n".join(f"# filler {idx}" for idx in range(80))
    (root / "sample.py").write_text(
        f"from __future__ import annotations\n\n{filler}\n\ndef value():\n    return 'original'\n",
        encoding="utf-8",
    )
    sample_hash = runner._file_hash(root / "sample.py")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("get_file_hash", {"path": "sample.py"}),
            ("read_file_partial", {"path": "sample.py", "offset": 0, "limit": 80}),
            ("read_file_partial", {"path": "sample.py", "offset": 0, "limit": 200}),
            ("read_file_partial", {"path": "sample.py", "offset": 0, "limit": 500}),
            ("read_file_partial", {"path": "sample.py", "offset": 500, "limit": 500}),
            ("read_file_partial", {"path": "sample.py", "offset": 1000, "limit": 500}),
            ("read_file_partial", {"path": "sample.py", "offset": 1500, "limit": 500}),
            (
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "return 'original'",
                    "new_text": "return 'updated'",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
            ),
            ("finish_execution", {"summary": "patched after progressive reads"}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "PASS"
    assert result["changed_files"] == ["sample.py"]
    assert "return 'updated'" in (root / "sample.py").read_text(encoding="utf-8")


def test_openai_tool_use_slice_reads_after_full_read_do_not_extend_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    filler = "\n".join(f"# context {idx}" for idx in range(120))
    (root / "sample.py").write_text(
        f"{filler}\n\ndef value():\n    return 'original'\n",
        encoding="utf-8",
    )
    sample_hash = runner._file_hash(root / "sample.py")
    _install_fake_post_chat(
        monkeypatch,
        [
            ("read_file_partial", {"path": "sample.py"}),
            ("read_file_partial", {"path": "sample.py", "offset": 0, "limit": 80}),
            ("read_file_partial", {"path": "sample.py", "offset": 200, "limit": 80}),
            ("read_file_partial", {"path": "sample.py", "offset": 400, "limit": 80}),
            ("read_file_partial", {"path": "sample.py", "offset": 600, "limit": 80}),
            (
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "return 'original'",
                    "new_text": "return 'updated'",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
            ),
            ("finish_execution", {"summary": "patched after slice reads"}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"
    config = _config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1")
    config.runtime_caps["max_no_progress_iterations"] = 3

    result = runner.materialize_openai_tool_use_result(
        config=config,
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "NO_PROGRESS_ABORTED"
    assert result["changed_files"] == []
    assert "return 'original'" in (root / "sample.py").read_text(encoding="utf-8")


def test_openai_tool_use_exact_str_replace_reports_ambiguous_match_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    sample_hash = runner._file_hash(root / "sample.py")
    _install_fake_post_chat(
        monkeypatch,
        [
            (
                "str_replace",
                {
                    "path": "sample.py",
                    "old_text": "x = 1",
                    "new_text": "x = 2",
                    "expected_occurrences": 1,
                    "precondition_sha256": sample_hash,
                },
            ),
            ("finish_execution", {"summary": "finish"}),
        ],
    )
    payload = _request(root, planning_dir)
    payload["files_to_change"] = ["sample.py"]
    payload["execution_protocol"] = "exact_str_replace_v1"

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch, execution_protocol="exact_str_replace_v1"),
        request_path=planning_dir / ".execution_request.json",
        request_payload=payload,
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] in {"OPENAI_TOOL_USE_ERROR", "MAX_TOOL_ITERATIONS", "VERIFY_FAILED"}
    assert (root / "sample.py").read_text(encoding="utf-8") == "x = 1\nx = 1\n"
    patch = json.loads((planning_dir / runner.PATCH_ATTEMPTS_FILENAME).read_text(encoding="utf-8").splitlines()[0])
    assert patch["error_code"] == "PATCH_OCCURRENCE_MISMATCH"


def test_openai_tool_use_redacts_http_error_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    (root / "sample.txt").write_text("before\n", encoding="utf-8")

    def _fake(**_kwargs: Any) -> dict[str, Any]:
        raise runner.OpenAIToolUseExecutionError("HTTP_ERROR", "Authorization: Bearer tp-super-secret-test-key")

    monkeypatch.setattr(runner, "_post_chat", _fake)

    result = runner.materialize_openai_tool_use_result(
        config=_config(root, planning_dir, monkeypatch),
        request_path=planning_dir / ".execution_request.json",
        request_payload=_request(root, planning_dir),
    )

    assert result["status"] == "BLOCKED"
    assert "tp-super-secret-test-key" not in json.dumps(result)
    assert "<redacted>" in json.dumps(result)


def test_openai_tool_use_http_timeout_uses_wall_clock_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "project"
    planning_dir = root / ".claude" / "workflow" / "run"
    planning_dir.mkdir(parents=True)
    config = _config(root, planning_dir, monkeypatch)

    assert runner._http_timeout_seconds(config) == 120

    config.runtime_caps["max_wall_clock_seconds"] = 3600

    assert runner._http_timeout_seconds(config) == 300

    config.runtime_caps["http_timeout_seconds"] = 45

    assert runner._http_timeout_seconds(config) == 45

    config.runtime_caps["http_timeout_seconds"] = 2

    assert runner._http_timeout_seconds(config) == 5

    config.runtime_caps["http_timeout_seconds"] = 600

    assert runner._http_timeout_seconds(config) == 300


def test_openai_tool_use_redirect_guard_blocks_same_host_downgrade() -> None:
    handler = runner.SafeRedirectHandler()
    request = urlrequest.Request("https://api.example.test/v1/chat/completions")

    with pytest.raises(runner.RedirectBlocked):
        handler.redirect_request(
            request,
            fp=None,
            code=302,
            msg="Found",
            headers={},
            newurl="http://api.example.test/v1/chat/completions",
        )


def test_openai_tool_use_redirect_guard_preserves_same_origin_post_307() -> None:
    handler = runner.SafeRedirectHandler()
    request = urlrequest.Request(
        "https://api.example.test/v1/chat/completions",
        data=b'{"ping": true}',
        headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        method="POST",
    )

    redirected = handler.redirect_request(
        request,
        fp=None,
        code=307,
        msg="Temporary Redirect",
        headers={"Location": "https://api.example.test/v1/chat/completions/"},
        newurl="https://api.example.test/v1/chat/completions/",
    )

    assert redirected is not None
    assert redirected.get_method() == "POST"
    assert redirected.data == b'{"ping": true}'
    assert redirected.full_url == "https://api.example.test/v1/chat/completions/"
    assert redirected.headers["Authorization"] == "Bearer test-token"

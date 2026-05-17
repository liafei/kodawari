from __future__ import annotations

import json
from pathlib import Path
import subprocess

import kodawari.autopilot.recovery.executor_recovery as recovery
from kodawari.autopilot.recovery.executor_recovery import (
    RecoverySynthesizerConfig,
    build_recovery_prompt,
    build_recovery_card,
    build_scope_expansion_recovery_card,
    normalize_recovery_decision,
    request_recovery_decision,
    write_recovery_artifacts,
)
from kodawari.infra.io_atomic import canonical_json_text


def test_recovery_decision_filters_patch_plan_to_allowed_files() -> None:
    decision = normalize_recovery_decision(
        {
            "action": "narrow_patch_plan",
            "patch_plan": [
                {
                    "id": "ok",
                    "operation": "str_replace",
                    "path": "src/app.py",
                    "old_text": "old",
                    "new_text": "new",
                },
                {
                    "id": "bad",
                    "operation": "str_replace",
                    "path": "../secret.txt",
                    "old_text": "old",
                    "new_text": "new",
                },
            ],
        },
        allowed_files=["src/app.py"],
    )

    assert decision["action"] == "narrow_patch_plan"
    assert [item["id"] for item in decision["patch_plan"]] == ["ok"]


def test_recovery_decision_supports_expand_scope_request() -> None:
    decision = normalize_recovery_decision(
        {
            "action": "expand_scope_request",
            "requested_files": ["src/router.py"],
            "reason": "router contract owns the blocker",
        },
        allowed_files=["src/app.py"],
    )

    assert decision["action"] == "expand_scope_request"
    assert decision["requested_files"] == ["src/router.py"]


def test_recovery_prompt_includes_previous_attempt_context() -> None:
    prompt = build_recovery_prompt(
        task="T08: recover verifier",
        task_card={"files_to_change": ["src/app.py"], "verify_cmd": "pytest -q"},
        must_fix=["sqlite no such column: method"],
        stall_report={"error_code": "VERIFY_FAILED_RETRYABLE"},
        allowed_files=["src/app.py"],
        recovery_context={
            "previous_recovery_decisions": [
                {"action": "narrow_patch_plan", "reason": "added retry helper"},
            ],
            "previous_execution_result": {"error_code": "VERIFY_FAILED_RETRYABLE"},
        },
    )

    assert "previous_recovery_decisions" in prompt
    assert "do not repeat the same patch" in prompt
    assert "schema/table/column mismatch" in prompt


def test_recovery_card_inherits_scope_and_verify_command() -> None:
    card = build_recovery_card(
        original_card={
            "files_to_change": ["src/app.py"],
            "new_files": [],
            "invariants": ["route unchanged"],
            "verify_cmd": "pytest tests/test_app.py -q",
            "coverage_hints": ["session-admin bearer must be rejected"],
        },
        decision={
            "action": "narrow_patch_plan",
            "reason": "fix review blocker",
            "patch_plan": [{"id": "p1", "operation": "str_replace", "path": "src/app.py", "old_text": "a", "new_text": "b"}],
        },
        task_id="T001",
        must_fix=["fix wrapper"],
    )

    assert card["task_id"] == "T001_RECOVERY"
    assert card["files_to_change"] == ["src/app.py"]
    assert card["verify_cmd"] == "pytest tests/test_app.py -q"
    assert card["coverage_hints"] == ["session-admin bearer must be rejected"]
    assert card["patch_plan"][0]["id"] == "p1"
    assert card["recovery"]["must_fix"] == ["fix wrapper"]


def test_copy_string_list_keeps_legacy_filtering_semantics() -> None:
    assert recovery._copy_string_list({"items": ["a", "", "  ", 7]}, "items") == ["a", "7"]
    assert recovery._copy_string_list({"items": None}, "items") == []
    assert recovery._copy_string_list({}, "items") == []


def test_recovery_card_canonical_json_is_byte_stable() -> None:
    card = build_recovery_card(
        original_card={
            "files_to_change": ["src/app.py"],
            "new_files": ["src/new.py"],
            "invariants": ["keep API"],
            "forbidden_changes": ["db schema"],
            "verify_cmd": "pytest -q",
        },
        decision={
            "action": "narrow_patch_plan",
            "reason": "fix blocker",
            "patch_plan": [{"id": "p1", "operation": "str_replace", "path": "src/app.py"}],
        },
        task_id="T001",
        must_fix=["review failed"],
    )

    assert canonical_json_text(card) == (
        "{\n"
        '  "files_to_change": [\n'
        '    "src/app.py"\n'
        "  ],\n"
        '  "forbidden_changes": [\n'
        '    "db schema"\n'
        "  ],\n"
        '  "invariants": [\n'
        '    "keep API"\n'
        "  ],\n"
        '  "new_files": [\n'
        '    "src/new.py"\n'
        "  ],\n"
        '  "patch_plan": [\n'
        "    {\n"
        '      "id": "p1",\n'
        '      "operation": "str_replace",\n'
        '      "path": "src/app.py"\n'
        "    }\n"
        "  ],\n"
        '  "recovery": {\n'
        '    "must_fix": [\n'
        '      "review failed"\n'
        "    ],\n"
        '    "reason": "fix blocker",\n'
        '    "schema_version": "execution.recovery_card.v1",\n'
        '    "source_action": "narrow_patch_plan"\n'
        "  },\n"
        '  "schema_version": "contract_first.task_card.v1",\n'
        '  "task_id": "T001_RECOVERY",\n'
        '  "task_name": "Recovery for T001",\n'
        '  "verify_cmd": "pytest -q",\n'
        '  "why_this_layer": "Executor recovery card generated from deterministic stall/review evidence."\n'
        "}\n"
    )


def test_write_recovery_artifacts_removes_stale_card_when_no_card(tmp_path: Path) -> None:
    write_recovery_artifacts(
        tmp_path,
        decision={"schema_version": "execution.recovery_decision.v1", "action": "narrow_patch_plan"},
        card={"schema_version": "contract_first.task_card.v1", "task_id": "T1_RECOVERY"},
    )
    assert (tmp_path / recovery.RECOVERY_CARD_FILENAME).exists()

    write_recovery_artifacts(
        tmp_path,
        decision={"schema_version": "execution.recovery_decision.v1", "action": "escalate_to_human"},
        card=None,
    )

    assert not (tmp_path / recovery.RECOVERY_CARD_FILENAME).exists()


def test_write_recovery_artifacts_uses_canonical_json_and_trailing_newline(tmp_path: Path) -> None:
    decision = {"schema_version": "execution.recovery_decision.v1", "action": "escalate_to_human", "z": 1}
    card = {"schema_version": "contract_first.task_card.v1", "task_id": "T1_RECOVERY", "a": 1}

    write_recovery_artifacts(tmp_path, decision=decision, card=card)

    assert (tmp_path / recovery.RECOVERY_DECISION_FILENAME).read_text(encoding="utf-8") == canonical_json_text(decision)
    assert (tmp_path / recovery.RECOVERY_CARD_FILENAME).read_text(encoding="utf-8") == canonical_json_text(card)


def test_scope_expansion_recovery_card_adds_existing_guarded_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (project / "src" / "router.py").write_text("from src.app import app\n", encoding="utf-8")

    card = build_scope_expansion_recovery_card(
        original_card={
            "files_to_change": ["src/app.py"],
            "verify_cmd": "pytest tests/test_app.py -q",
            "coverage_hints": ["router contract owns the failing assertion"],
        },
        decision={
            "action": "expand_scope_request",
            "requested_files": ["src/router.py", "../secret.txt", "src/app.py"],
            "reason": "router contract owns the failing assertion",
        },
        task_id="T001",
        must_fix=["verify failed"],
        project_root=project,
    )

    assert card is not None
    assert card["files_to_change"] == ["src/app.py", "src/router.py"]
    assert card["verify_cmd"] == "pytest tests/test_app.py -q"
    assert card["coverage_hints"] == ["router contract owns the failing assertion"]
    assert card["recovery"]["source_action"] == "expand_scope_request"
    assert card["recovery"]["approved_scope_files"] == ["src/router.py"]


def test_scope_expansion_recovery_card_canonical_json_is_byte_stable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("old\n", encoding="utf-8")
    (project / "src" / "router.py").write_text("route\n", encoding="utf-8")

    card = build_scope_expansion_recovery_card(
        original_card={"files_to_change": ["src/app.py"], "verify_cmd": "pytest -q"},
        decision={"action": "expand_scope_request", "requested_files": ["src/router.py"], "reason": "need route"},
        task_id="T001",
        must_fix=["blocked"],
        project_root=project,
    )

    assert card is not None
    assert canonical_json_text(card) == canonical_json_text(
        {
            "schema_version": "contract_first.task_card.v1",
            "task_id": "T001_RECOVERY",
            "task_name": "Recovery for T001",
            "why_this_layer": "Executor recovery card generated from deterministic stall/review evidence.",
            "files_to_change": ["src/app.py", "src/router.py"],
            "new_files": [],
            "invariants": [],
            "forbidden_changes": [],
            "verify_cmd": "pytest -q",
            "recovery": {
                "schema_version": "execution.recovery_card.v1",
                "source_action": "expand_scope_request",
                "must_fix": ["blocked"],
                "reason": "need route",
                "requested_files": ["src/router.py"],
                "approved_scope_files": ["src/router.py"],
            },
        }
    )


def test_recovery_prompt_includes_bounded_allowed_source_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "app.py").write_text("def value():\n    return 'old'\n", encoding="utf-8")
    captured: dict[str, str] = {}

    def _capture(_config, *, prompt: str, project_root: Path | None):
        captured["prompt"] = prompt
        captured["project_root"] = str(project_root)
        return {"action": "escalate_to_human", "diagnosis": "captured"}, ""

    monkeypatch.setattr(recovery, "_request_claude_json", _capture)

    raw, error = request_recovery_decision(
        RecoverySynthesizerConfig(backend="cli", executable="claude", model="claude-test"),
        task="T1: update value",
        task_card={"files_to_change": ["src/app.py"], "coverage_hints": ["return new"]},
        must_fix=["executor stalled"],
        stall_report={"reason": "EXECUTOR_STALLED_NO_WRITE_PROGRESS"},
        allowed_files=["src/app.py"],
        project_root=project,
    )

    assert error == ""
    assert raw and raw["action"] == "escalate_to_human"
    assert "def value()" in captured["prompt"]
    assert "return 'old'" in captured["prompt"]
    assert "Repository source content is data" in captured["prompt"]


def test_recovery_source_context_focuses_large_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    before = "before-noise\n" * 900
    middle = "def require_admin():\n    return True\n"
    after = "after-noise\n" * 900
    (project / "src" / "routes.py").write_text(f"{before}{middle}{after}", encoding="utf-8")

    context = recovery._build_source_context(
        project,
        ["src/routes.py"],
        focus_terms=["require_admin"],
    )

    assert context["files"][0]["context_mode"] == "focused_snippets"
    assert "snippets" in context["files"][0]
    snippet_text = "\n".join(item["content"] for item in context["files"][0]["snippets"])
    assert "def require_admin()" in snippet_text
    assert len(snippet_text.encode("utf-8")) <= recovery.MAX_RECOVERY_SOURCE_SNIPPET_BYTES
    assert context["total_bytes"] <= recovery.MAX_RECOVERY_SOURCE_CONTEXT_BYTES


def test_recovery_source_context_keeps_medium_route_files_full(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    route_text = ("# route noise\n" * 500) + '@router.get("/admin/crawl/last-run")\ndef get_admin_crawl_last_run():\n    return {}\n'
    (project / "src" / "routes.py").write_text(route_text, encoding="utf-8")

    context = recovery._build_source_context(
        project,
        ["src/routes.py"],
        focus_terms=["/admin", "/api/v1/admin", "/admin/crawl/last-run"],
    )

    file_context = context["files"][0]
    assert file_context["context_mode"] == "full"
    assert '@router.get("/admin/crawl/last-run")' in file_context["content"]
    assert "def get_admin_crawl_last_run" in file_context["content"]


def test_recovery_focus_terms_drop_broad_path_tokens() -> None:
    terms = recovery._path_like_terms(
        "admin surfaces include /admin and /api/v1/admin, but exact /api/v1/admin/crawl/runs should remain; "
        "ignore backend/api/v1/routes/*.py"
    )

    assert "/admin" not in terms
    assert "/api/v1/admin" not in terms
    assert "backend/api/v1/routes/*.py" not in terms
    assert "/api/v1/admin/crawl/runs" in terms


def test_recovery_focus_terms_ignore_non_dict_stall_arguments() -> None:
    terms = recovery._stall_report_focus_terms(
        {
            "recent_tool_calls": [
                {"arguments": None},
                {"arguments": "query"},
                {"arguments": ["query"]},
            ]
        }
    )

    assert terms == []


def test_recovery_source_context_snippets_file_even_when_raw_size_exceeds_remaining_budget(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    medium = ("# medium route\n" * 900) + "def existing_route():\n    return True\n"
    large = ("before\n" * 2500) + "def target_route():\n    return True\n" + ("after\n" * 2500)
    (project / "src" / "medium.py").write_text(medium, encoding="utf-8")
    (project / "src" / "large.py").write_text(large, encoding="utf-8")

    context = recovery._build_source_context(
        project,
        ["src/medium.py", "src/large.py"],
        focus_terms=["target_route"],
    )

    assert [item["path"] for item in context["files"]] == ["src/medium.py", "src/large.py"]
    assert context["files"][0]["context_mode"] == "full"
    assert context["files"][1]["context_mode"] == "focused_snippets"
    snippet_text = "\n".join(item["content"] for item in context["files"][1]["snippets"])
    assert "def target_route()" in snippet_text


def test_recovery_source_context_can_expand_existing_files_to_full_source(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    large = ("before\n" * 2500) + "def target_route():\n    return True\n" + ("after\n" * 2500)
    (project / "src" / "large.py").write_text(large, encoding="utf-8")

    context = recovery._build_source_context(
        project,
        ["src/large.py"],
        focus_terms=["target_route"],
        full_source_files=["src/large.py"],
    )

    file_context = context["files"][0]
    assert file_context["context_mode"] == "full"
    assert "def target_route()" in file_context["content"]
    assert file_context["content"].count("after") >= 2500


def test_recovery_subprocess_timeout_terminates_process_tree(monkeypatch) -> None:
    terminated: list[int] = []

    class _TimedOutProcess:
        pid = 12345
        returncode = None

        def communicate(self, _prompt: str, *, timeout: int) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(cmd=["claude"], timeout=timeout)

        def poll(self) -> None:
            return None

    proc = _TimedOutProcess()

    def _fake_popen(*_args, **_kwargs):
        return proc

    def _fake_terminate(timed_out_proc) -> None:
        terminated.append(timed_out_proc.pid)

    monkeypatch.setattr(recovery.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(recovery, "_terminate_process_tree", _fake_terminate)

    payload, error = recovery._run_json_subprocess(["claude"], prompt="{}", timeout_seconds=1, project_root=None)

    assert payload is None
    assert error == "recovery synthesizer timed out after 30s"
    assert terminated == [12345]


def test_recovery_subprocess_prefers_output_last_message_file(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "last.json"

    class _Process:
        pid = 12345
        returncode = 0

        def communicate(self, _prompt: str, *, timeout: int) -> tuple[str, str]:
            output_path.write_text('{"action":"escalate_to_human","diagnosis":"from file"}', encoding="utf-8")
            return ("not json", "")

        def poll(self) -> int:
            return 0

    def _fake_popen(*_args, **_kwargs):
        return _Process()

    monkeypatch.setattr(recovery.subprocess, "Popen", _fake_popen)

    payload, error = recovery._run_json_subprocess(
        ["codex"],
        prompt="{}",
        timeout_seconds=30,
        project_root=None,
        output_path=output_path,
    )

    assert error == ""
    assert payload == {"action": "escalate_to_human", "diagnosis": "from file"}


def test_recovery_subprocess_accepts_output_last_message_on_nonzero_exit(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "last.json"

    class _Process:
        pid = 12345
        returncode = 1

        def communicate(self, _prompt: str, *, timeout: int) -> tuple[str, str]:
            output_path.write_text('{"action":"escalate_to_human","diagnosis":"usable"}', encoding="utf-8")
            return ("stdout banner", "stderr warning")

        def poll(self) -> int:
            return 1

    def _fake_popen(*_args, **_kwargs):
        return _Process()

    monkeypatch.setattr(recovery.subprocess, "Popen", _fake_popen)

    payload, error = recovery._run_json_subprocess(
        ["codex"],
        prompt="{}",
        timeout_seconds=30,
        project_root=None,
        output_path=output_path,
    )

    assert error == ""
    assert payload == {"action": "escalate_to_human", "diagnosis": "usable"}


def test_recovery_subprocess_failure_reports_stdout_stderr_and_output_file(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "last.json"

    class _Process:
        pid = 12345
        returncode = 1

        def communicate(self, _prompt: str, *, timeout: int) -> tuple[str, str]:
            output_path.write_text("not json", encoding="utf-8")
            return ("stdout tail reason", "stderr head warning")

        def poll(self) -> int:
            return 1

    def _fake_popen(*_args, **_kwargs):
        return _Process()

    monkeypatch.setattr(recovery.subprocess, "Popen", _fake_popen)

    payload, error = recovery._run_json_subprocess(
        ["codex"],
        prompt="{}",
        timeout_seconds=30,
        project_root=None,
        output_path=output_path,
    )

    assert payload is None
    assert "output-last-message parse failed" in error
    assert "stderr=stderr head warning" in error
    assert "stdout=stdout tail reason" in error
    assert "output_last_message=not json" in error


def test_codex_recovery_schema_is_strict_for_structured_output() -> None:
    schema_path = recovery._write_temp_recovery_schema()
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    finally:
        recovery._unlink_quietly(schema_path)

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"action", "reason", "diagnosis", "requested_files", "patch_plan"}
    item_schema = schema["properties"]["patch_plan"]["items"]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) == {
        "id",
        "operation",
        "path",
        "old_text",
        "new_text",
        "content",
        "expected_occurrences",
        "precondition_sha256",
    }


def test_codex_recovery_exec_uses_isolated_low_reasoning_runtime(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    captured: dict[str, list[str]] = {}

    class _Process:
        pid = 12345
        returncode = 0

        def communicate(self, _prompt: str, *, timeout: int) -> tuple[str, str]:
            cmd = captured["cmd"]
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text('{"action":"escalate_to_human","diagnosis":"ok"}', encoding="utf-8")
            return ("", "")

        def poll(self) -> int:
            return 0

    def _fake_popen(cmd, *_args, **_kwargs):
        captured["cmd"] = list(cmd)
        return _Process()

    monkeypatch.setattr(recovery.subprocess, "Popen", _fake_popen)

    payload, error = recovery._request_codex_json(
        RecoverySynthesizerConfig(
            backend="codex",
            executable=str(executable),
            model="gpt-5.5",
            reasoning_effort="medium",
        ),
        prompt="{}",
        project_root=tmp_path,
    )

    assert error == ""
    assert payload == {"action": "escalate_to_human", "diagnosis": "ok"}
    cmd = captured["cmd"]
    assert "--ignore-user-config" not in cmd
    assert "--ephemeral" in cmd
    assert ["--model", "gpt-5.5"] == cmd[cmd.index("--model"): cmd.index("--model") + 2]
    assert "-c" in cmd
    assert 'model_reasoning_effort="medium"' in cmd


def test_recovery_prompt_compacts_stall_report_tool_results() -> None:
    prompt = recovery.build_recovery_prompt(
        task="T1: compact stall",
        task_card={"files_to_change": ["src/app.py"]},
        must_fix=["executor stalled"],
        stall_report={
            "reason": "EXECUTOR_STALLED_NO_WRITE_PROGRESS",
            "recent_tool_calls": [
                {
                    "iteration": 1,
                    "tool": "read_file_partial",
                    "arguments": {"path": "src/app.py", "offset": 0, "limit": 500},
                    "result": {
                        "ok": True,
                        "path": "src/app.py",
                        "content": "x" * 5000,
                        "matches": [{"line": index, "offset": index, "excerpt": "secret" * 100} for index in range(8)],
                    },
                }
            ],
        },
        allowed_files=["src/app.py"],
    )

    assert "EXECUTOR_STALLED_NO_WRITE_PROGRESS" in prompt
    assert '"content"' not in prompt
    assert "secretsecret" not in prompt
    assert '"matches_omitted": 3' in prompt

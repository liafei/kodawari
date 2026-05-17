import json
from pathlib import Path

from kodawari.autopilot.collaboration import (
    CollaborationRole,
    enforce_reviewer_boundary,
    normalize_reviewer_feedback,
)
from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine
from kodawari.autopilot.review_bridge import normalize_self_review_payload
from kodawari.instincts import learn_from_globs


def _expected_hook_events() -> list[str]:
    return [
        "pre_compact",
        "pre_plan",
        "post_plan",
        "pre_implement",
        "post_implement",
        "pre_review",
        "post_review",
        "pre_gate",
        "post_gate",
        "auto_gate",
    ]


def _assert_hook_events(result: dict[str, object]) -> None:
    hook_events = [event["event"] for event in result["hook_events"]]
    assert hook_events[0] == "session_start"
    assert hook_events[-1] == "session_stop"
    for expected in _expected_hook_events():
        assert expected in hook_events
    _assert_hook_metadata(result)


def _assert_hook_fields(events: list[dict[str, object]]) -> None:
    for item in events:
        assert item["lifecycle_version"] == "ws114.v2"
        assert item["phase"]
        assert isinstance(item["phase_order"], int)
        assert item["actor_boundary"]
        assert item["event_sequence_key"]


def _has_boundary_event(
    events: list[dict[str, object]],
    *,
    event_name: str,
    actor_boundary: str,
) -> bool:
    return any(
        item["event"] == event_name and item["actor_boundary"] == actor_boundary
        for item in events
    )


def _assert_hook_metadata(result: dict[str, object]) -> None:
    events = result["hook_events"]
    hook_indexes = [int(item["hook_index"]) for item in events]
    assert hook_indexes == list(range(1, len(events) + 1))
    _assert_hook_fields(events)
    assert _has_boundary_event(events, event_name="pre_review", actor_boundary="opus")
    assert _has_boundary_event(events, event_name="pre_implement", actor_boundary="codex")


def _changes_requested_review_rounds(rounds: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        record
        for record in rounds
        if record["stage"] == "PEER_REVIEW" and record["stage_status"] == "changes_requested"
    ]


def _review_iteration_numbers(rounds: list[dict[str, object]]) -> list[int]:
    return [
        int(record.get("review_round", 0))
        for record in rounds
        if record["stage"] == "PEER_REVIEW"
    ]


def _assert_round_metadata(rounds: list[dict[str, object]]) -> None:
    assert all(record["round_id"].startswith("R") for record in rounds)
    assert all(record["action"] for record in rounds)
    assert all("assigned_role_before" in record for record in rounds)
    assert all("assigned_role_after" in record for record in rounds)


def _assert_review_fix_rounds(result: dict[str, object]) -> None:
    rounds = result["rounds"]
    stages = [record["stage"] for record in rounds]
    assert "DESIGN" in stages
    assert "VERIFY" in stages
    assert "RULES_GATE" in stages
    assert "FIX_ROUND" in stages
    assert "SELF_REVIEW" in stages
    assert all(record.get("round_outcome") for record in rounds)
    assert _changes_requested_review_rounds(rounds)
    assert all(record["round_outcome"] == "needs_fix" for record in _changes_requested_review_rounds(rounds))
    review_rounds = _review_iteration_numbers(rounds)
    assert review_rounds == sorted(review_rounds)
    _assert_round_metadata(rounds)


def _assert_absorption_payloads(result: dict[str, object]) -> None:
    expected_status = {
        "planning_summary": "已吸收",
        "context_compact": "部分吸收",
        "instincts": "部分吸收",
    }
    _assert_compact_runtime_alignment(result, expected_status=expected_status)
    _assert_review_runtime_semantics(result, expected_status=expected_status)
    assert result["architecture_decisions"]
    assert result["must_fix_open_items"] == []
    assert result["gate_recommendation"] == "PROCEED_TO_GATE"
    assert result["post_execution_qa"]["status"] == "PASS"


def _assert_compact_runtime_alignment(
    result: dict[str, object],
    *,
    expected_status: dict[str, str],
) -> None:
    assert result["pre_compact"]["feature"] == "ws114"
    assert "compact_markdown" in result["pre_compact"]
    assert result["pre_compact"]["merged_absorption_status"] == expected_status
    assert result["merged_absorption_status"] == expected_status
    assert result["pre_compact"]["instincts_loaded"] is False
    assert result["pre_compact"]["instincts_status"] == "store_not_found"
    runtime_compact = result["context_compact_runtime"]
    assert runtime_compact["status"] == "partial"
    assert runtime_compact["mode"] == "compat"
    assert runtime_compact["triggered"] is True
    assert runtime_compact["trigger_event"] == "pre_compact"
    assert runtime_compact["instincts_loaded"] is False
    assert runtime_compact["instincts_status"] == "store_not_found"
    assert runtime_compact["artifact_state"] == "written"
    assert runtime_compact["merged_absorption_status"] == expected_status
    assert runtime_compact["post_loop"]["reason"] == result["reason"]
    assert runtime_compact["post_loop"]["stop_reason"] == result["unified_status"]["stop_reason"]
    assert runtime_compact["post_loop"]["blocked"] == result["unified_status"]["is_blocked"]
    assert runtime_compact["loop_stop_reason"] == result["unified_status"]["stop_reason"]
    assert runtime_compact["loop_reason"] == result["reason"]
    assert runtime_compact["loop_blocked"] == result["unified_status"]["is_blocked"]
    assert result["pre_compact"]["runtime"]["trigger_event"] == "pre_compact"
    compact_md = Path(runtime_compact["artifacts"]["COMPACT_CONTEXT.md"])
    compact_json = Path(runtime_compact["artifacts"]["compact_context.json"])
    assert compact_md.exists()
    assert compact_json.exists()
    compact_payload = json.loads(compact_json.read_text(encoding="utf-8"))
    assert compact_payload["runtime_status"] == "partial"
    assert compact_payload["runtime_mode"] == "compat"
    assert compact_payload["instincts_loaded"] is False
    assert compact_payload["instincts_status"] == "store_not_found"
    assert compact_payload["merged_absorption_status"] == expected_status
    assert compact_payload["post_loop"]["reason"] == result["reason"]
    assert compact_payload["post_loop"]["stop_reason"] == result["unified_status"]["stop_reason"]
    assert compact_payload["loop_stop_reason"] == result["unified_status"]["stop_reason"]


def _assert_review_runtime_semantics(
    result: dict[str, object],
    *,
    expected_status: dict[str, str],
) -> None:
    assert result["codex_self_reviews"]
    assert result["self_review_summary"]["review_count"] == len(result["codex_self_reviews"])
    assert result["self_review_summary"]["reviewers"] == ["codex"]
    assert all(item.get("reviewer") == "codex" for item in result["codex_self_reviews"])
    assert result["peer_review_summary"]["review_count"] >= 1
    assert result["peer_review_summary"]["reviewers"]
    assert result["peer_review_summary"]["max_review_iteration"] >= 1
    assert result["peer_review_summary"]["review_round"] >= 1
    assert result["peer_review_summary"]["must_fix_remaining"] == 0
    assert result["peer_review_summary"]["approved"] is True
    runtime_semantics = result["runtime_semantics"]
    assert runtime_semantics["peer_review"]["approved"] is True
    assert runtime_semantics["peer_review"]["must_fix_remaining"] == 0
    assert runtime_semantics["peer_review"]["mode"] == "simulate_local"
    assert runtime_semantics["peer_review"]["source"] == "kodawari"
    assert runtime_semantics["peer_review"]["real_requested"] is False
    assert runtime_semantics["peer_review"]["fallback_used"] is False
    assert runtime_semantics["self_review"]["count"] == len(result["codex_self_reviews"])
    assert runtime_semantics["self_review"]["count"] == result["self_review_summary"]["review_count"]
    assert runtime_semantics["self_review"]["reviewers"] == result["self_review_summary"]["reviewers"]
    assert runtime_semantics["self_review"]["latest"]["reviewer"] == "codex"
    assert runtime_semantics["compact_runtime"]["available"] is True
    assert runtime_semantics["compact_runtime"]["status"] == "partial"
    assert runtime_semantics["compact_runtime"]["mode"] == "compat"
    assert runtime_semantics["compact_runtime"]["instincts_loaded"] is False
    assert runtime_semantics["compact_runtime"]["merged_absorption_status"] == expected_status


def test_reviewer_feedback_normalization_absorbs_blocking_semantics() -> None:
    feedback = normalize_reviewer_feedback(
        {
            "approved": True,
            "summary": "- critical: API contract changed without tests",
            "must_fix": ["Must fix: add compatibility tests"],
            "reviewer": "opus",
        },
        review_iteration=2,
    )

    assert feedback.approved is False
    assert feedback.review_iteration == 2
    assert feedback.severity == "critical"
    assert feedback.gate_recommendation == "REVIEW_FIX_REQUIRED"
    assert any("critical" in item.lower() for item in feedback.blocking_items)


def test_reviewer_boundary_preserves_upstream_original_reviewer() -> None:
    payload = enforce_reviewer_boundary(
        {"reviewer": "codex", "original_reviewer": "anthropic-opus"},
        expected_reviewer=CollaborationRole.OPUS,
    )

    assert payload["reviewer"] == "opus"
    assert payload["actor_boundary_enforced"] is True
    assert payload["original_reviewer"] == "anthropic-opus"


def test_self_review_boundary_preserves_upstream_original_reviewer() -> None:
    payload = normalize_self_review_payload(
        payload={"reviewer": "opus", "original_reviewer": "anthropic-opus"},
        feature="ws114-self-review",
        changed_files=["src/module.py"],
        reviewer="codex",
    )

    assert payload["reviewer"] == "codex"
    assert payload["actor_boundary_enforced"] is True
    assert payload["original_reviewer"] == "anthropic-opus"


def test_engine_absorbs_orchestrator_hooks_and_review_fix_rounds(tmp_path: Path) -> None:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="ws114",
        max_cycles=12,
        collaboration_max_rounds=8,
    )
    engine = AutopilotEngine(config=config, requirements_text="")

    result = engine.run_collaboration_loop(
        task_label="T114: Architecture review for merged workflow loop",
        task_scope="absorb hook lifecycle and reviewer semantics",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    _assert_review_fix_rounds(result)
    _assert_hook_events(result)
    _assert_absorption_payloads(result)

    policy = result["peer_review_policy"]
    assert policy["target_score"] == 95
    assert policy["min_dimension_score"] == 80
    assert policy["max_rounds"] == 8

    final_feedback = result["collaboration_context"]["review_feedback"]
    assert final_feedback["approved"] is True
    assert final_feedback["gate_recommendation"] == "PROCEED_TO_GATE"


def test_round_records_keep_actor_boundary(tmp_path: Path) -> None:
    config = AutopilotConfig(project_root=tmp_path, feature="ws114-role", max_cycles=10)
    engine = AutopilotEngine(config=config, requirements_text="")

    result = engine.run_collaboration_loop(
        task_label="T900: Architecture design for SDK orchestration",
        task_scope="single-source orchestration absorption",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    actors_by_stage = {(record["stage"], record["actor"]) for record in result["rounds"]}
    assert ("DESIGN", "opus") in actors_by_stage
    assert ("PEER_REVIEW", "opus") in actors_by_stage
    assert any(stage in {"IMPLEMENT", "FIX_ROUND", "SELF_REVIEW"} and actor == "codex" for stage, actor in actors_by_stage)
    assert result["collaboration_context"]["fix_history"]
    assert result["collaboration_context"]["fix_history"][0]["actor"] == "codex"

    encoded = json.dumps(result["hook_events"], ensure_ascii=False)
    assert "session_start" in encoded
    assert "auto_gate" in encoded


def test_engine_runtime_compact_loads_instinct_hints_when_store_exists(tmp_path: Path) -> None:
    learn_from_globs(tmp_path, ["planning/*", "src/**/*.py"])
    config = AutopilotConfig(project_root=tmp_path, feature="ws114-instincts", max_cycles=10)
    engine = AutopilotEngine(config=config, requirements_text="")

    result = engine.run_collaboration_loop(
        task_label="T901: absorb instincts hints into compact runtime",
        task_scope="verify minimal instincts runtime handoff",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["pre_compact"]["instincts_loaded"] is True
    assert result["pre_compact"]["instincts_status"] == "loaded"
    assert result["pre_compact"]["instinct_hints_count"] >= 1

    runtime_compact = result["context_compact_runtime"]
    assert runtime_compact["merged_absorption_status"]["planning_summary"] == "已吸收"
    assert runtime_compact["merged_absorption_status"]["context_compact"] == "部分吸收"
    assert runtime_compact["merged_absorption_status"]["instincts"] == "部分吸收"
    assert runtime_compact["instincts_loaded"] is True
    assert runtime_compact["instincts_status"] == "loaded"
    assert runtime_compact["instinct_hints_count"] >= 1
    assert runtime_compact["instinct_hints"]
    assert any(item["pattern"] == "planning/*" for item in runtime_compact["instinct_hints"])

    compact_json = Path(runtime_compact["artifacts"]["compact_context.json"])
    compact_payload = json.loads(compact_json.read_text(encoding="utf-8"))
    assert compact_payload["instincts_loaded"] is True
    assert compact_payload["instincts_status"] == "loaded"
    assert compact_payload["instinct_hints_count"] >= 1


class _InstinctCaptureAdapter:
    def __init__(self) -> None:
        self.implement_contexts: list[dict[str, object]] = []

    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task
        self.implement_contexts.append(dict(context))
        return {
            "status": "done",
            "changes": ["src/module.py", "tests/test_module.py"],
        }


def test_engine_implementation_context_consumes_instinct_hints(tmp_path: Path) -> None:
    learn_from_globs(tmp_path, ["planning/*", "src/**/*.py"])
    adapter = _InstinctCaptureAdapter()
    config = AutopilotConfig(project_root=tmp_path, feature="ws114-instinct-consume", max_cycles=10)
    engine = AutopilotEngine(config=config, requirements_text="", adapter=adapter)

    result = engine.run_collaboration_loop(
        task_label="T901B: consume instincts hints in implementation runtime",
        task_scope="lock runtime hints consumption semantics",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.implement_contexts
    first_context = adapter.implement_contexts[0]
    assert first_context["instincts_loaded"] is True
    assert first_context["instincts_status"] == "loaded"
    assert int(first_context["instinct_hints_count"]) >= 1
    hints = list(first_context["instinct_hints"])
    assert any(item["pattern"] == "planning/*" for item in hints)

    codex_implement = next(record for record in result["rounds"] if record["stage"] == "IMPLEMENT")
    assert codex_implement["details"]["instincts_status"] == "loaded"
    assert codex_implement["details"]["instinct_hints_count"] >= 1
    assert "planning/*" in codex_implement["details"]["instinct_hint_patterns"]


class _RoundLimitBoundaryAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["src/module.py", "tests/test_module.py"],
        }

    def review(
        self,
        *,
        task: str,
        context: dict[str, object],
        changed_files: list[str],
        review_iteration: int = 0,
    ) -> dict[str, object]:
        del task, context, changed_files, review_iteration
        return {
            "approved": True,
            "reviewer": "codex",
            "summary": "Ship it",
            "score": 90,
            "target_score": 95,
            "min_dimension_score": 80,
            "dimension_scores": {"architecture": 79, "tests": 88},
        }

    def self_review(
        self,
        *,
        task: str,
        context: dict[str, object],
        changed_files: list[str],
        review_iteration: int = 0,
    ) -> dict[str, object]:
        del task, context, changed_files, review_iteration
        return {"reviewer": "opus", "approved": True, "summary": "local self review"}


def _round_records_for_stage(result: dict[str, object], stage: str) -> list[dict[str, object]]:
    rounds = result["rounds"]
    return [row for row in rounds if row["stage"] == stage]


def _assert_round_limit_review_rounds(result: dict[str, object]) -> None:
    review_rounds = _round_records_for_stage(result, "PEER_REVIEW")
    assert len(review_rounds) == 2
    assert all(item["details"]["reviewer"] == "opus" for item in review_rounds)
    assert all(item["details"]["gate_recommendation"] == "REVIEW_FIX_REQUIRED" for item in review_rounds)
    assert all(item["stage_status"] == "changes_requested" for item in review_rounds)
    assert all(item["round_outcome"] == "needs_fix" for item in review_rounds)


def _assert_round_limit_hook_details(result: dict[str, object]) -> None:
    post_review_events = [event for event in result["hook_events"] if event["event"] == "post_review"]
    assert post_review_events
    assert all("review_round" in event["details"] for event in post_review_events)
    assert all("must_fix_remaining" in event["details"] for event in post_review_events)
    assert all("gate_recommendation" in event["details"] for event in post_review_events)

    session_stop = next(event for event in reversed(result["hook_events"]) if event["event"] == "session_stop")
    assert session_stop["details"]["review_rounds_used"] == 2
    assert session_stop["details"]["must_fix_remaining"] == len(result["must_fix_open_items"])
    assert session_stop["details"]["gate_recommendation"] == result["gate_recommendation"]
    assert session_stop["details"]["reason"] == result["loop_outcome"]["reason"]


def test_engine_round_limit_uses_review_rounds_and_enforces_actor_boundary(tmp_path: Path) -> None:
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="ws114-round-limit",
        max_cycles=20,
        collaboration_max_rounds=2,
    )
    engine = AutopilotEngine(
        config=config,
        requirements_text="",
        adapter=_RoundLimitBoundaryAdapter(),
    )

    result = engine.run_collaboration_loop(
        task_label="T114B: absorb deeper review round semantics",
        task_scope="orchestration absorption regression",
    )

    assert result["reason"] == "COLLABORATION_ROUND_LIMIT"
    assert result["review_rounds_used"] == 2
    assert result["last_error"] == "Reached review round limit (2)"
    assert result["unified_status"]["stop_reason"] == "STUCK"
    assert result["unified_status"]["blocking_reason"] == "Reached review round limit (2)"
    assert result["loop_outcome"]["reason"] == "COLLABORATION_ROUND_LIMIT"
    assert result["loop_outcome"]["stop_reason"] == "STUCK"
    assert result["loop_outcome"]["blocked"] is True
    assert result["loop_outcome"]["round_outcome"] == "needs_fix"
    assert result["loop_outcome"]["exit_category"] == "blocked"
    assert "Reached review round limit" in (result["loop_outcome"]["blocking_reason"] or "")
    assert result["loop_outcome"]["must_fix_remaining"] == len(result["must_fix_open_items"])
    assert result["must_fix_open_items"]
    assert result["gate_recommendation"] == "REVIEW_FIX_REQUIRED"
    _assert_round_limit_review_rounds(result)

    codex_reviews = result["codex_self_reviews"]
    assert codex_reviews == []
    _assert_round_limit_hook_details(result)

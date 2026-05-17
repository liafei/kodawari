from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kodawari.autopilot.core.collaboration import CollaborationAction
from kodawari.autopilot.engine.engine import AutopilotConfig, AutopilotEngine
from kodawari.autopilot.recovery.executor_recovery import RECOVERY_CARD_FILENAME, RECOVERY_DECISION_FILENAME
from kodawari.autopilot.recovery.gate_recovery import (
    GATE_COMPLEXITY_RECOVERY_ACTION,
    build_gate_complexity_recovery,
)


def test_complexity_gate_recovery_uses_structured_violation_and_read_only_tests(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "provider.py", "def fetch():\n    return 1\n")
    _write_project_file(tmp_path / "tests" / "test_provider.py", "def test_fetch():\n    assert True\n")

    result = build_gate_complexity_recovery(
        project_root=tmp_path,
        gate_check=_gate_check(),
        original_card={
            "files_to_change": ["src/provider.py", "tests/test_provider.py"],
            "read_only_files": ["src/base_provider.py"],
            "related_existing_tests": ["tests/test_provider.py"],
            "invariants": ["keep return shape"],
            "forbidden_changes": ["do not touch auth"],
            "coverage_hints": ["RSS parser behavior"],
            "verify_cmd": "python -m pytest tests/test_provider.py -q",
        },
        task_id="T123",
        must_fix=["src/provider.py: Function fetch complexity 16 exceeds 10."],
    )

    assert result is not None
    decision, card = result
    assert decision["action"] == GATE_COMPLEXITY_RECOVERY_ACTION
    assert decision["gate_violations"][0]["symbol"] == "fetch"
    assert "patch_plan" not in card
    assert card["files_to_change"] == ["src/provider.py"]
    assert card["read_only_files"] == ["src/base_provider.py", "tests/test_provider.py"]
    assert card["related_existing_tests"] == ["tests/test_provider.py"]
    assert card["target_symbols"] == [
        {
            "kind": "function",
            "name": "fetch",
            "file": "src/provider.py",
            "line": 5,
            "metric": "complexity",
            "actual": 16,
            "limit": 10,
        }
    ]
    assert card["coverage_hints"] == ["RSS parser behavior"]
    assert card["recovery"]["source_action"] == GATE_COMPLEXITY_RECOVERY_ACTION


def test_function_metrics_gate_recovery_accepts_complexity_and_nesting(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "provider.py", "def fetch():\n    return 1\n")
    gate_check = _gate_check(
        extra_violation={
            "path": "src/provider.py",
            "line": 5,
            "symbol": "fetch",
            "metric": "nesting",
            "actual": 5,
            "limit": 3,
            "message": "Function fetch nesting depth 5 exceeds 3.",
        }
    )

    result = build_gate_complexity_recovery(
        project_root=tmp_path,
        gate_check=gate_check,
        original_card={"files_to_change": ["src/provider.py"]},
        task_id="T123",
        must_fix=["gate blocked"],
    )

    assert result is not None
    decision, card = result
    assert [item["metric"] for item in decision["gate_violations"]] == ["complexity", "nesting"]
    assert card["target_symbols"][1]["metric"] == "nesting"


def test_complexity_gate_recovery_rejects_out_of_scope_violation(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "provider.py", "def fetch():\n    return 1\n")

    result = build_gate_complexity_recovery(
        project_root=tmp_path,
        gate_check=_gate_check(),
        original_card={"files_to_change": ["src/other.py"]},
        task_id="T123",
        must_fix=["gate blocked"],
    )

    assert result is None


def test_complexity_gate_recovery_rejects_violation_count_mismatch(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "provider.py", "def fetch():\n    return 1\n")
    gate_check = _gate_check()
    gate_check["total_violations"] = 2

    result = build_gate_complexity_recovery(
        project_root=tmp_path,
        gate_check=gate_check,
        original_card={"files_to_change": ["src/provider.py"]},
        task_id="T123",
        must_fix=["gate blocked"],
    )

    assert result is None


def test_engine_routes_complexity_only_gate_block_without_synthesizer(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "__init__.py", "")
    _write_project_file(
        tmp_path / "tests" / "test_provider.py",
        "from src.provider import fetch\n\n\ndef test_fetch_counts_thresholds():\n    assert fetch(12) == 11\n",
    )
    planning_dir = tmp_path / "planning" / "newsapp"
    _write_project_file(
        planning_dir / "TASK_CARD_ACTIVE.json",
        json.dumps(
            {
                "schema_version": "contract_first.task_card.v1",
                "task_id": "T123",
                "task_name": "Implement provider",
                "files_to_change": ["src/provider.py", "tests/test_provider.py"],
                "related_existing_tests": ["tests/test_provider.py"],
                "verify_cmd": "python -m pytest tests/test_provider.py -q",
            }
        ),
    )
    adapter = _ComplexityGateRecoveryAdapter()
    config = AutopilotConfig(
        project_root=tmp_path,
        feature="newsapp",
        max_cycles=10,
        verify_cmd="python -m pytest tests/test_provider.py -q",
    )
    engine = AutopilotEngine(config, adapter=adapter)

    result = engine.run_collaboration_loop(
        task_label="T123: Implement provider",
        task_scope="exercise deterministic complexity recovery",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert adapter.calls == 2
    assert adapter.recovery_calls == 0
    recovery_context = adapter.contexts[1]
    recovery_card = recovery_context["recovery_card"]
    assert recovery_card["recovery"]["source_action"] == GATE_COMPLEXITY_RECOVERY_ACTION
    assert recovery_card["files_to_change"] == ["src/provider.py"]
    assert recovery_card["read_only_files"] == ["tests/test_provider.py"]
    assert any(
        event["event"] == "executor_recovery_deterministic"
        for event in result["hook_events"]
    )
    assert json.loads((planning_dir / RECOVERY_CARD_FILENAME).read_text(encoding="utf-8"))["recovery"][
        "source_action"
    ] == GATE_COMPLEXITY_RECOVERY_ACTION
    assert json.loads((planning_dir / RECOVERY_DECISION_FILENAME).read_text(encoding="utf-8"))[
        "action"
    ] == GATE_COMPLEXITY_RECOVERY_ACTION


def test_engine_routes_function_metrics_gate_recovery_without_synthesizer(tmp_path: Path) -> None:
    _write_project_file(tmp_path / "src" / "provider.py", "def fetch():\n    return 1\n")
    adapter = _SynthesizerCountingAdapter()
    config = AutopilotConfig(project_root=tmp_path, feature="newsapp")
    engine = AutopilotEngine(config, adapter=adapter)
    engine._task_card_payload = {"files_to_change": ["src/provider.py"]}
    runtime = engine._create_loop_runtime(
        task_label="T124: Provider",
        task_scope="manual recovery check",
        max_rounds=2,
        enable_peer_review=True,
    )
    runtime.verify_check = {"status": "PASS", "passed": True}
    runtime.gate_check = _gate_check(
        extra_violation={
            "path": "src/provider.py",
            "line": 5,
            "symbol": "fetch",
            "metric": "nesting",
            "actual": 5,
            "limit": 3,
            "message": "Function fetch nesting depth 5 exceeds 3.",
        }
    )
    runtime.context.review_feedback.must_fix = ["gate blocked"]

    engine._maybe_prepare_executor_recovery(
        runtime=runtime,
        action=CollaborationAction.FIX_ROUND,
        round_record={},
    )

    assert adapter.recovery_calls == 0
    assert runtime.pending_recovery_card is not None
    assert runtime.pending_recovery_card["recovery"]["source_action"] == GATE_COMPLEXITY_RECOVERY_ACTION


class _ComplexityGateRecoveryAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.recovery_calls = 0
        self.contexts: list[dict[str, Any]] = []

    def implement(self, task: str, context: dict[str, Any]) -> dict[str, Any]:
        del task
        self.calls += 1
        self.contexts.append(dict(context))
        root = Path(str(context["project_root"]))
        if self.calls == 1:
            _write_project_file(root / "src" / "provider.py", _complex_provider_source())
            return {"status": "done", "changes": ["src/provider.py", "tests/test_provider.py"]}
        recovery = dict(context.get("recovery_card") or {}).get("recovery") or {}
        assert recovery["source_action"] == GATE_COMPLEXITY_RECOVERY_ACTION
        _write_project_file(
            root / "src" / "provider.py",
            "def fetch(value):\n    return sum(1 for threshold in range(11) if value > threshold)\n",
        )
        return {"status": "done", "changes": ["src/provider.py"]}

    def synthesize_executor_recovery(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.recovery_calls += 1
        raise AssertionError("complexity-only gate blocks must not call the recovery synthesizer")


class _SynthesizerCountingAdapter:
    def __init__(self) -> None:
        self.recovery_calls = 0

    def synthesize_executor_recovery(self, **kwargs: Any) -> dict[str, Any]:
        self.recovery_calls += 1
        must_fix = list(kwargs.get("must_fix") or [])
        return {
            "status": "ok",
            "decision": {
                "schema_version": "execution.recovery_decision.v1",
                "action": "narrow_patch_plan",
                "reason": "fallback path",
                "patch_plan": [
                    {
                        "id": "p1",
                        "operation": "str_replace",
                        "path": "src/provider.py",
                        "old_text": "return 1",
                        "new_text": "return 2",
                    }
                ],
                "must_fix": must_fix,
            },
        }


def _gate_check(*, extra_violation: dict[str, Any] | None = None) -> dict[str, Any]:
    violations = [
        {
            "checker": "function_metrics",
            "path": "src/provider.py",
            "line": 5,
            "symbol": "fetch",
            "metric": "complexity",
            "actual": 16,
            "limit": 10,
            "severity": "block",
            "message": "Function fetch complexity 16 exceeds 10.",
        }
    ]
    if extra_violation is not None:
        violations.append(extra_violation)
    return {
        "total_status": "BLOCKED",
        "items": [
            {
                "checker": "function_metrics",
                "status": "BLOCKED",
                "checked_files": 1,
                "violations": violations,
            }
        ],
        "blocking_reason": "src/provider.py: Function fetch complexity 16 exceeds 10.",
    }


def _complex_provider_source() -> str:
    lines = ["def fetch(value):", "    score = 0"]
    for number in range(11):
        lines.extend([f"    if value > {number}:", "        score += 1"])
    lines.append("    return score")
    return "\n".join(lines) + "\n"


def _write_project_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

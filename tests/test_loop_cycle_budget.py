from pathlib import Path

from kodawari.autopilot.engine import AutopilotConfig, AutopilotEngine


class _ApprovedInOnePassAdapter:
    def implement(self, task: str, context: dict[str, object]) -> dict[str, object]:
        del task, context
        return {
            "status": "done",
            "changes": ["app/main.py", "tests/test_main.py"],
        }


def test_peer_review_bookkeeping_does_not_consume_cycle_budget_after_verify_and_gate(
    tmp_path: Path,
) -> None:
    app_file = tmp_path / "app" / "main.py"
    test_file = tmp_path / "tests" / "test_main.py"
    app_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    app_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    test_file.write_text("def test_handler():\n    assert True\n", encoding="utf-8")

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="cycle-budget", max_cycles=1),
        adapter=_ApprovedInOnePassAdapter(),
    )

    result = engine.run_collaboration_loop(
        task_label="T1: Prepare schema contract",
        task_scope="single implementation pass with scoped tests",
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["verify_check"]["status"] == "PASS"
    assert result["gate_check"]["total_status"] == "PASS"
    assert result["peer_review_summary"]["approved"] is True
    assert result["self_review_summary"]["review_count"] == 1
    assert all(row["stage_status"] != "max_cycles" for row in result["rounds"])
    review_round = next(row for row in result["rounds"] if row["stage"] == "PEER_REVIEW")
    self_review_round = next(row for row in result["rounds"] if row["stage"] == "SELF_REVIEW")
    proceed_round = next(row for row in result["rounds"] if row["stage"] == "PROCEED_TO_GATE")
    assert review_round["cycle"] == 1
    assert self_review_round["cycle"] == 1
    assert proceed_round["cycle"] == 1


def test_single_pass_design_bookkeeping_does_not_consume_cycle_budget(
    tmp_path: Path,
) -> None:
    app_file = tmp_path / "app" / "main.py"
    test_file = tmp_path / "tests" / "test_main.py"
    app_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.parent.mkdir(parents=True, exist_ok=True)
    app_file.write_text("def handler():\n    return 1\n", encoding="utf-8")
    test_file.write_text("def test_handler():\n    assert True\n", encoding="utf-8")

    engine = AutopilotEngine(
        AutopilotConfig(project_root=tmp_path, feature="cycle-budget", max_cycles=1),
        adapter=_ApprovedInOnePassAdapter(),
    )

    result = engine.run_collaboration_loop(
        task_label="TASK001: Run scoped verify",
        task_scope="single-pass task cycle should fit inside one implementation cycle",
        enable_peer_review=False,
    )

    assert result["reason"] == "PROCEED_TO_GATE"
    assert result["verify_check"]["status"] == "PASS"
    assert result["gate_check"]["total_status"] == "PASS"
    assert all(row["stage_status"] != "max_cycles" for row in result["rounds"])
    design_round = next(row for row in result["rounds"] if row["stage"] == "DESIGN")
    implement_round = next(row for row in result["rounds"] if row["stage"] == "IMPLEMENT")
    proceed_round = next(row for row in result["rounds"] if row["stage"] == "PROCEED_TO_GATE")
    assert design_round["cycle"] == 0
    assert implement_round["cycle"] == 1
    assert proceed_round["cycle"] == 1

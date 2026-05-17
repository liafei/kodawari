from __future__ import annotations

import argparse
import json
from pathlib import Path

from kodawari.cli.autopilot_cmd import run_autopilot_command
from kodawari.cli.autopilot_decision_runtime import DECISION_REQUEST_FILENAME


def test_autopilot_prd_flow_requests_architecture_freeze(tmp_path: Path, capsys) -> None:
    prd_path = tmp_path / "PRD.md"
    prd_path.write_text(
        "\n".join(
            [
                "# Demo PRD",
                "",
                "Build a small FastAPI endpoint for hydration summaries.",
            ]
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        project_root=str(tmp_path),
        feature="autodrive-demo",
        tier="heavy",
        prd=str(prd_path),
        requirements_file=None,
        profile="profiles/generic.yaml",
        verify_cmd="pytest -q",
        max_cycles=4,
        token_budget=20000,
        task_cycle=True,
        enable_peer_review=False,
        task_label=None,
        task_scope=None,
    )

    rc = run_autopilot_command(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    planning_dir = Path(payload["planning_dir"])
    decision_path = planning_dir / DECISION_REQUEST_FILENAME

    assert payload["status"] == "awaiting_decision"
    assert payload["planning_artifact_mode"] == "contract_first"
    assert payload["interaction_state"] == "AWAITING_DECISION"
    assert payload["decision_kind"] in {"intent_clarification", "architecture_freeze"}
    assert payload["next_action_type"] == "await_decision"
    assert decision_path.exists()

    request_payload = json.loads(decision_path.read_text(encoding="utf-8"))
    assert request_payload["decision_kind"] == payload["decision_kind"]
    assert request_payload["decision_id"] == f"autodrive-demo:{payload['decision_kind']}"

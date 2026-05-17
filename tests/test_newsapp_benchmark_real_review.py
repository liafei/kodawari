from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.generic_canary_support import create_test_shims, run_cli, workflow_env
from tests.test_newsapp_benchmark_proof import (
    _respond_decision,
    _run_autopilot,
    _write_newsapp_benchmark_fixture,
    _write_passing_review_evidence,
    _write_prd,
    _write_self_review_script,
    _write_text,
    _write_verify_script,
)


REAL_REVIEW_READY = bool(
    (os.getenv("WORKFLOW_REVIEWER_API_KEY") or os.getenv("WORKFLOW_OPUS_API_KEY"))
    and (os.getenv("WORKFLOW_REVIEWER_BASE_URL") or os.getenv("WORKFLOW_OPUS_GATEWAY"))
)


def _integration_env(tmp_path: Path) -> dict[str, str]:
    return workflow_env(
        shim_dir=create_test_shims(tmp_path),
        extra_env={
            "WORKFLOW_REVIEW_ENABLED": "1",
            "WORKFLOW_REVIEW_REQUIRED": "1",
        },
    )


def _run_until_terminal(
    *,
    project_root: Path,
    feature: str,
    prd_path: Path,
    env: dict[str, str],
    verify_cmd: str,
    self_review_cmd: str,
) -> tuple[int, dict[str, object], Path]:
    planning_dir = project_root / "planning" / feature
    final_rc = 1
    final_payload: dict[str, object] = {}
    for _ in range(6):
        rc, payload = _run_autopilot(
            project_root,
            feature=feature,
            prd_path=prd_path,
            env=env,
            verify_cmd=verify_cmd,
            self_review_cmd=self_review_cmd,
        )
        final_rc = rc
        final_payload = payload
        planning_dir = Path(str(payload.get("planning_dir") or planning_dir))
        if payload.get("status") != "awaiting_decision":
            return final_rc, final_payload, planning_dir
        decision_kind = str(payload.get("decision_kind") or "")
        if decision_kind == "release_approval":
            _write_text(project_root / "AUTOMATION_EVAL_REPORT.json", json.dumps({"status": "PASS"}))
            _write_passing_review_evidence(planning_dir=planning_dir, feature=feature)
            _respond_decision(planning_dir, selected_option="ship", rationale="integration ship")
            continue
        _respond_decision(planning_dir, selected_option="approve", rationale="integration continue")
    return final_rc, final_payload, planning_dir


def _load_peer_runtime(planning_dir: Path) -> dict[str, object]:
    chain_path = planning_dir / ".workflow_chain.json"
    payload = json.loads(chain_path.read_text(encoding="utf-8"))
    upstream = dict(payload.get("upstream") or {})
    return dict(upstream.get("peer_review_runtime") or {})


@pytest.mark.skipif(
    not REAL_REVIEW_READY,
    reason="newsapp benchmark real-review proof requires WORKFLOW_REVIEWER_API_KEY and WORKFLOW_REVIEWER_BASE_URL",
)
def test_newsapp_benchmark_real_review_runtime_semantics(tmp_path: Path) -> None:
    feature = "newsapp-benchmark-real-review"
    _write_newsapp_benchmark_fixture(tmp_path)
    prd_path = tmp_path / "PRD.md"
    _write_prd(prd_path)
    env = _integration_env(tmp_path)
    verify_cmd = _write_verify_script(tmp_path)
    self_review_cmd = _write_self_review_script(tmp_path)

    final_rc, final_payload, planning_dir = _run_until_terminal(
        project_root=tmp_path,
        feature=feature,
        prd_path=prd_path,
        env=env,
        verify_cmd=verify_cmd,
        self_review_cmd=self_review_cmd,
    )
    assert final_rc in {0, 1}
    assert final_payload

    peer_runtime = _load_peer_runtime(planning_dir)
    assert peer_runtime.get("real_requested") is True
    assert peer_runtime.get("real_required") is True
    assert peer_runtime.get("fallback_used") is False
    mode = str(peer_runtime.get("mode") or "")
    assert mode in {"real_opus_gateway", "real_required_failed"}
    if mode == "real_required_failed":
        assert str(peer_runtime.get("error") or "").strip()

    status_rc, status_payload, _ = run_cli(
        "status",
        "--project-root",
        str(tmp_path),
        "--feature",
        feature,
        env=env,
    )
    assert status_rc == 0
    assert status_payload["real_review_requested"] is True
    assert status_payload["real_review_required"] is True
    assert status_payload["fallback_used"] is False
    assert status_payload["review_mode"] in {"real_peer_review", "simulated"}

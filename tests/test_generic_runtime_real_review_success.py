from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_generic_runtime_real_review import (
    REAL_REVIEW_CASES,
    _prepare_case_project,
    _prepare_task_card,
    _task_run_verify_cmd,
)
from tests.generic_canary_support import run_cli


pytestmark = pytest.mark.skipif(
    not (
        (os.getenv("WORKFLOW_REVIEWER_API_KEY") or os.getenv("WORKFLOW_OPUS_API_KEY"))
        and (os.getenv("WORKFLOW_REVIEWER_BASE_URL") or os.getenv("WORKFLOW_OPUS_GATEWAY"))
    ),
    reason="real-review-success lane requires WORKFLOW_REVIEWER_API_KEY and WORKFLOW_REVIEWER_BASE_URL",
)


def test_generic_runtime_real_review_success_lane(tmp_path: Path) -> None:
    case = REAL_REVIEW_CASES[0]
    project_root, env = _prepare_case_project(tmp_path, case)
    task_card_path = _prepare_task_card(project_root, case, env)

    run_rc, run_payload, _ = run_cli(
        "task-run",
        "--project-root",
        str(project_root),
        "--feature",
        case.feature,
        "--card",
        task_card_path,
        "--strict-scope",
        "--contract-mode",
        "strict",
        "--executor-backend",
        "codex_cli",
        "--self-review-backend",
        "noop_test_only",
        "--real-opus-review",
        "--require-real-opus-review",
        "--verify-cmd",
        _task_run_verify_cmd(project_root),
        env=env,
    )

    assert run_rc == 0, run_payload
    runtime = dict((run_payload.get("run_result") or {}).get("runtime_semantics") or {})
    peer_review = dict(runtime.get("peer_review") or {})
    assert peer_review.get("mode") in {"real_opus_gateway", "real_peer_review_gateway"}
    assert peer_review.get("real_requested") is True
    assert peer_review.get("real_required") is True
    assert peer_review.get("fallback_used") is False
    assert peer_review.get("semantic_review_performed") is True
    assert peer_review.get("review_quality") == "real"

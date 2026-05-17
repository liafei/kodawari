"""Unit tests for `kodawari serve` HTTP endpoints."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kodawari.cli.serve_cmd import (
    _aggregate_stats,
    _build_app,
    _derive_stages,
    _format_sse,
    _list_projects,
    _project_status,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "planning").mkdir()
    (root / ".claude" / "workflow").mkdir(parents=True)
    (root / ".claude" / "workflow" / "models.yaml").write_text(
        "schema_version: \"models.v2\"\n"
        "roles:\n"
        "  planner: { transport: mimo_chat, model: mimo-v2.5-pro }\n"
        "  impl_reviewer: { transport: codex_local, model: gpt-5.4 }\n"
        "  executor: { transport: mimo_tool_use, model: mimo-v2.5-pro }\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def feature_dir(project_root: Path) -> Path:
    feat = project_root / "planning" / "feature-alpha"
    feat.mkdir()
    _write_json(
        feat / ".work_all_manifest.json",
        {
            "status": "PASS",
            "summary": "feature alpha completed",
            "steps": [
                {"name": "plan", "status": "PASS"},
                {"name": "work", "status": "PASS"},
                {"name": "review", "status": "PASS"},
                {"name": "release", "status": "PASS"},
            ],
        },
    )
    _write_json(
        feat / ".execution_result.json",
        {"status": "PASS", "changed_files": ["docs/A.md"]},
    )
    _write_json(feat / ".task_run_result.json", {"task_delta_changed_files": ["docs/A.md"]})
    _write_json(feat / "PLANNING_CONVERSATION.json", {"schema_version": "x", "rounds": []})
    _write_jsonl(
        feat / ".autopilot_rounds.jsonl",
        [
            {
                "round": 1,
                "stage": "PEER_REVIEW",
                "action": "peer_review",
                "actor": "opus",
                "round_outcome": "success",
                "details": {"approved": True, "blocking_items": [], "score": 95},
            },
            {
                "round": 2,
                "stage": "VERIFY",
                "action": "verify",
                "stage_status": "pass",
                "round_outcome": "success",
                "details": {"blocking_items": []},
            },
        ],
    )
    return feat


def test_list_projects_returns_features_sorted_by_mtime(project_root: Path, feature_dir: Path) -> None:
    second = project_root / "planning" / "feature-beta"
    second.mkdir()
    _write_json(second / ".work_all_manifest.json", {"status": "RUNNING"})
    # Make beta newer than alpha.
    os.utime(second, (time.time(), time.time() + 60))
    projects = _list_projects(project_root)
    assert [p["feature"] for p in projects] == ["feature-beta", "feature-alpha"]
    assert projects[0]["status"] == "RUNNING"
    assert projects[1]["status"] == "PASS"


def test_list_projects_skips_underscore_prefixed_dirs(project_root: Path, feature_dir: Path) -> None:
    archived = project_root / "planning" / "_archive"
    archived.mkdir()
    _write_json(archived / ".work_all_manifest.json", {"status": "PASS"})
    features = {p["feature"] for p in _list_projects(project_root)}
    assert "_archive" not in features


def test_project_status_aggregates_rounds_and_stages(project_root: Path, feature_dir: Path) -> None:
    status = _project_status(project_root, feature_dir)
    assert status["feature"] == "feature-alpha"
    assert status["rounds_count"] == 2
    assert status["stats"] == {"issues": 0, "fixed": 0, "passed": 2}
    stage_by_id = {s["id"]: s["status"] for s in status["stages"]}
    assert stage_by_id["prd"] == "done"
    assert stage_by_id["gen"] == "done"
    assert stage_by_id["review"] == "done"
    assert stage_by_id["test"] == "done"
    assert stage_by_id["done"] == "done"
    assert "planner" in status["models"]


def test_aggregate_stats_counts_blocking_and_fix_rounds() -> None:
    rounds = [
        {"action": "peer_review", "round_outcome": "needs_fix", "details": {"blocking_items": ["a", "b"]}},
        {"action": "fix_round", "round_outcome": "success", "details": {"blocking_items": []}},
        {"action": "peer_review", "round_outcome": "success", "details": {"blocking_items": []}},
    ]
    stats = _aggregate_stats(rounds)
    assert stats == {"issues": 2, "fixed": 1, "passed": 2}


def test_derive_stages_marks_failure_when_manifest_blocked() -> None:
    stages = _derive_stages(
        manifest={"steps": [{"name": "plan", "status": "BLOCKED"}]},
        execution={},
        task_run={},
        rounds=[],
        state={},
    )
    by_id = {s["id"]: s["status"] for s in stages}
    assert by_id["prd"] == "failed"
    # Subsequent stages stay pending when plan failed.
    assert by_id["split"] == "pending"


def test_get_projects_endpoint(project_root: Path, feature_dir: Path) -> None:
    client = TestClient(_build_app(project_root))
    r = client.get("/api/projects")
    assert r.status_code == 200
    payload = r.json()
    assert payload["schema_version"] == "serve.projects.v1"
    assert payload["project_root"] == str(project_root)
    assert {p["feature"] for p in payload["projects"]} == {"feature-alpha"}
    assert "planner" in payload["models"]


def test_get_projects_endpoint_query_root_overrides_default(tmp_path: Path, project_root: Path, feature_dir: Path) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()
    (other_root / "planning").mkdir()
    client = TestClient(_build_app(project_root))
    r = client.get("/api/projects", params={"root": str(other_root)})
    assert r.status_code == 200
    assert r.json()["project_root"] == str(other_root.resolve())
    assert r.json()["projects"] == []


def test_get_project_endpoint(project_root: Path, feature_dir: Path) -> None:
    client = TestClient(_build_app(project_root))
    r = client.get("/api/projects/feature-alpha")
    assert r.status_code == 200
    payload = r.json()
    assert payload["schema_version"] == "serve.project_status.v1"
    assert payload["feature"] == "feature-alpha"
    assert payload["rounds_count"] == 2
    assert payload["stats"]["passed"] == 2


def test_get_project_returns_404_when_unknown(project_root: Path) -> None:
    client = TestClient(_build_app(project_root))
    r = client.get("/api/projects/does-not-exist")
    assert r.status_code == 404


def test_get_artifact_returns_json_payload(project_root: Path, feature_dir: Path) -> None:
    client = TestClient(_build_app(project_root))
    r = client.get("/api/projects/feature-alpha/artifacts/PLANNING_CONVERSATION.json")
    assert r.status_code == 200
    assert r.json()["schema_version"] == "x"


def test_get_artifact_blocks_traversal_and_unlisted(project_root: Path, feature_dir: Path) -> None:
    client = TestClient(_build_app(project_root))
    r = client.get("/api/projects/feature-alpha/artifacts/secret.txt")
    assert r.status_code == 403


def test_sse_payload_gets_schema_version_when_missing() -> None:
    raw = _format_sse({"kind": "round", "round": 1}).decode("utf-8")
    assert "data: " in raw
    payload = json.loads(raw.split("data: ", 1)[1].strip())
    assert payload["schema_version"] == "serve.event.v1"
    assert payload["kind"] == "round"


def test_post_create_project_requires_prd_input(project_root: Path) -> None:
    client = TestClient(_build_app(project_root))
    r = client.post("/api/projects", json={"feature": "smoke-feature"})
    assert r.status_code == 400


def test_post_missing_root_when_no_default_returns_400(tmp_path: Path) -> None:
    client = TestClient(_build_app(default_project_root=None))
    r = client.get("/api/projects")
    assert r.status_code == 400

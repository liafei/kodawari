"""Tests for D2 — `kodawari doctor preflight` static configuration checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.cli.runtime import doctor_cmd


def _project(tmp_path: Path) -> Path:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".gitignore").write_text(
        "\n".join([".workflow/", ".workflow_runtime/", "planning/"]) + "\n",
        encoding="utf-8",
    )
    (project_root / ".workflow_runtime" / "local-env" / ".venv").mkdir(parents=True)
    return project_root


def _args(project_root: Path, **overrides: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "project_root": str(project_root),
        "feature": "test-feat",
        "prd": None,
        "require_real_peer_review": False,
        "output": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_preflight_returns_pass_when_all_static_checks_succeed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = _project(tmp_path)
    args = _args(project_root)

    rc = doctor_cmd.run_doctor_preflight_command(args)

    out = capsys.readouterr().out
    report = json.loads(out)
    assert rc == 0
    assert report["status"] == "PASS"
    assert report["blockers"] == 0
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses["project_root_exists"] == "PASS"
    assert statuses["planning_dir_writable"] == "PASS"
    assert statuses["workflow_ignore_present"] == "PASS"


def test_preflight_fails_when_project_root_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _args(tmp_path / "does-not-exist")

    rc = doctor_cmd.run_doctor_preflight_command(args)

    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report["status"] == "FAIL"
    assert any(
        c["name"] == "project_root_exists" and c["status"] == "FAIL"
        for c in report["checks"]
    )


def test_preflight_warns_when_gitignore_missing_workflow_entries(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (project_root / ".workflow_runtime" / "local-env" / ".venv").mkdir(parents=True)

    rc = doctor_cmd.run_doctor_preflight_command(_args(project_root))

    report = json.loads(capsys.readouterr().out)
    # WARN does not block — rc=0, but the warning surfaces.
    assert rc == 0
    assert report["status"] == "WARN"
    workflow_ignore = next(c for c in report["checks"] if c["name"] == "workflow_ignore_present")
    assert workflow_ignore["status"] == "WARN"
    assert "remediation" in workflow_ignore


def test_preflight_fails_when_prd_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = _project(tmp_path)
    args = _args(project_root, prd=str(tmp_path / "no-such-prd.md"))

    rc = doctor_cmd.run_doctor_preflight_command(args)

    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert report["status"] == "FAIL"
    prd_check = next(c for c in report["checks"] if c["name"] == "prd_file")
    assert prd_check["status"] == "FAIL"


def test_preflight_fails_when_prd_too_short(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project_root = _project(tmp_path)
    prd = tmp_path / "stub.md"
    prd.write_text("hi", encoding="utf-8")  # 2 bytes, well below 100

    rc = doctor_cmd.run_doctor_preflight_command(_args(project_root, prd=str(prd)))

    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    prd_check = next(c for c in report["checks"] if c["name"] == "prd_file")
    assert prd_check["status"] == "FAIL"
    assert "100" in prd_check["detail"]


def test_preflight_fails_when_real_review_requested_without_env_vars(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2 explicit out-of-scope guard: we check env VARS exist (cheap, static)
    but do NOT call the gateway. Auth-liveness probing belongs to upstream-
    instability territory which the user excluded from scoring."""
    monkeypatch.delenv("WORKFLOW_REVIEWER_API_KEY", raising=False)
    monkeypatch.delenv("WORKFLOW_REVIEWER_BASE_URL", raising=False)
    project_root = _project(tmp_path)

    rc = doctor_cmd.run_doctor_preflight_command(
        _args(project_root, require_real_peer_review=True),
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 2
    reviewer_check = next(c for c in report["checks"] if c["name"] == "reviewer_env_vars")
    assert reviewer_check["status"] == "FAIL"
    assert "WORKFLOW_REVIEWER_API_KEY" in reviewer_check["detail"]


def test_preflight_passes_when_review_env_vars_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKFLOW_REVIEWER_API_KEY", "stub-key")
    monkeypatch.setenv("WORKFLOW_REVIEWER_BASE_URL", "https://example.invalid")
    project_root = _project(tmp_path)

    rc = doctor_cmd.run_doctor_preflight_command(
        _args(project_root, require_real_peer_review=True),
    )

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    reviewer_check = next(c for c in report["checks"] if c["name"] == "reviewer_env_vars")
    assert reviewer_check["status"] == "PASS"


def test_preflight_skips_reviewer_env_check_when_not_requested(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2: env-var check fires only when --require-real-peer-review is set —
    users running with simulated reviewer don't need the gateway creds."""
    monkeypatch.delenv("WORKFLOW_REVIEWER_API_KEY", raising=False)
    project_root = _project(tmp_path)

    rc = doctor_cmd.run_doctor_preflight_command(_args(project_root))

    report = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert not any(c["name"] == "reviewer_env_vars" for c in report["checks"])


def test_preflight_writes_output_file(tmp_path: Path) -> None:
    project_root = _project(tmp_path)
    output_path = tmp_path / "preflight.json"
    args = _args(project_root, output=str(output_path))

    rc = doctor_cmd.run_doctor_preflight_command(args)

    assert rc == 0
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "doctor_preflight.v1"

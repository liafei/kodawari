"""Tests for A3 — SCAFFOLD_MANIFEST.json write + greenfield-preferred read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kodawari.autopilot.planning.init_scaffold import (
    SCAFFOLD_MANIFEST_FILENAME,
    SCAFFOLD_MANIFEST_SCHEMA_VERSION,
    read_scaffold_manifest,
    scaffold_project,
    write_scaffold_manifest,
)
from kodawari.cli.contract.generic_bootstrap import (
    _scaffold_archetype_hint,
    ensure_repo_inventory,
)


def _scaffold_and_manifest(tmp_path: Path, archetype: str = "fastapi_api") -> dict[str, Any]:
    project_root = tmp_path / "proj"
    project_root.mkdir()
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir()
    scaffold = scaffold_project(
        project_root=project_root,
        archetype=archetype,
        capabilities=["docs_runbook"],
    )
    payload = write_scaffold_manifest(planning_dir, scaffold=scaffold, project_root=project_root)
    return {
        "project_root": project_root,
        "planning_dir": planning_dir,
        "scaffold": scaffold,
        "payload": payload,
    }


def test_write_scaffold_manifest_records_archetype_and_capabilities(tmp_path: Path) -> None:
    state = _scaffold_and_manifest(tmp_path)
    manifest_path = state["planning_dir"] / SCAFFOLD_MANIFEST_FILENAME
    assert manifest_path.exists(), "manifest must be written by write_scaffold_manifest"

    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == SCAFFOLD_MANIFEST_SCHEMA_VERSION
    assert parsed["archetype"] == "fastapi_api"
    assert "docs_runbook" in parsed["capabilities"]
    assert len(parsed["created_files"]) > 0


def test_read_scaffold_manifest_returns_none_when_absent(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir()
    assert read_scaffold_manifest(planning_dir) is None


def test_read_scaffold_manifest_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir()
    (planning_dir / SCAFFOLD_MANIFEST_FILENAME).write_text("{ not valid json", encoding="utf-8")
    # Soft-degrade not raise — keeps planning runnable on hand-edited manifests.
    assert read_scaffold_manifest(planning_dir) is None


def test_scaffold_archetype_hint_existing_mode_ignores_manifest(tmp_path: Path) -> None:
    """A3: existing-mode must NOT honor SCAFFOLD_MANIFEST — that's a greenfield
    artifact and existing projects derive archetype from real filesystem
    detection."""
    state = _scaffold_and_manifest(tmp_path, archetype="fastapi_api")
    archetype, capabilities = _scaffold_archetype_hint(
        planning_dir=state["planning_dir"],
        planning_mode="existing",
    )
    assert archetype == "auto"
    assert capabilities == []


def test_scaffold_archetype_hint_greenfield_prefers_manifest(tmp_path: Path) -> None:
    """A3 key behavior: greenfield mode reads manifest and returns the explicit
    archetype, NOT 'auto'. This prevents detect_archetype's empty-dir fallback
    (project_model.py:199) from coercing a CLI/lib project into fastapi_api."""
    state = _scaffold_and_manifest(tmp_path, archetype="fastapi_api")
    archetype, capabilities = _scaffold_archetype_hint(
        planning_dir=state["planning_dir"],
        planning_mode="greenfield",
    )
    assert archetype == "fastapi_api"
    assert "docs_runbook" in capabilities


def test_scaffold_archetype_hint_greenfield_no_manifest_falls_back_to_auto(tmp_path: Path) -> None:
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir()
    archetype, capabilities = _scaffold_archetype_hint(
        planning_dir=planning_dir,
        planning_mode="greenfield",
    )
    assert archetype == "auto"
    assert capabilities == []


def test_scaffold_archetype_hint_greenfield_schema_mismatch_falls_back(tmp_path: Path) -> None:
    """A3 forward-compat: an unrecognized schema_version is treated as a soft
    degrade rather than a hard failure. The consumer falls back to auto-detect
    so a stale or hand-edited manifest cannot break planning."""
    planning_dir = tmp_path / "planning"
    planning_dir.mkdir()
    (planning_dir / SCAFFOLD_MANIFEST_FILENAME).write_text(
        json.dumps({
            "schema_version": "scaffold.v999",
            "archetype": "cli_tool",
            "capabilities": [],
        }),
        encoding="utf-8",
    )
    archetype, _ = _scaffold_archetype_hint(
        planning_dir=planning_dir,
        planning_mode="greenfield",
    )
    assert archetype == "auto", "unrecognized schema_version must degrade to auto, not raise"


def test_ensure_repo_inventory_greenfield_with_manifest_uses_manifest_archetype(tmp_path: Path) -> None:
    """End-to-end A3 wiring: ensure_repo_inventory writes a REPO_INVENTORY.json
    whose archetype reflects the manifest, not the empty-dir auto-detect."""
    state = _scaffold_and_manifest(tmp_path, archetype="fastapi_api")
    project_root: Path = state["project_root"]
    planning_dir: Path = state["planning_dir"]
    steps_run: list[str] = []
    artifacts: dict[str, str] = {}

    payload = ensure_repo_inventory(
        planning_dir,
        project_root=project_root,
        planning_mode="greenfield",
        steps_run=steps_run,
        artifacts=artifacts,
    )

    assert payload["archetype"] == "fastapi_api", (
        "ensure_repo_inventory in greenfield mode must honor manifest archetype; "
        f"got {payload['archetype']!r}"
    )
    assert "docs_runbook" in list(payload.get("capabilities") or [])
    assert "repo-inventory" in steps_run

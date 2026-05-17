"""Cross-surface QA consistency helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kodawari.autopilot.core.verify_surfaces import surface_coverage_for_files
from kodawari.cli.contract.contract_first_schema import load_contract_first_artifact


def build_surface_consistency_checks(
    *,
    planning_dir: Path,
    execution_files: list[str],
    review_files: list[str],
    verify_files: list[str],
    verify_report: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    repo_inventory = _load_repo_inventory(planning_dir)
    if not repo_inventory:
        return {"surface_coverage_consistency": _pass_check("surface coverage skipped; repo inventory unavailable")}
    if str((verify_report or {}).get("verify_scope_mode") or "") == "custom":
        return {"surface_coverage_consistency": _pass_check("surface coverage skipped; verify used explicit custom command")}
    expected = _expected_surfaces(
        repo_inventory=repo_inventory,
        execution_files=execution_files,
        review_files=review_files,
        verify_files=verify_files,
    )
    actual = _verify_surfaces(verify_report)
    if not expected:
        return {"surface_coverage_consistency": _pass_check("surface coverage skipped; changed files did not map to a known surface")}
    if not actual:
        return {"surface_coverage_consistency": _fail_check(expected=expected, actual=actual)}
    if {item.lower() for item in expected} == {item.lower() for item in actual}:
        details = f"verify surface coverage consistent; expected={expected}; actual={actual}"
        return {"surface_coverage_consistency": _pass_check(details)}
    return {"surface_coverage_consistency": _fail_check(expected=expected, actual=actual)}


def _load_repo_inventory(planning_dir: Path) -> dict[str, Any]:
    path = (planning_dir / "REPO_INVENTORY.json").resolve()
    if not path.exists():
        return {}
    try:
        payload = load_contract_first_artifact(path, schema_name="repo_inventory")
    except ValueError:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _expected_surfaces(
    *,
    repo_inventory: dict[str, Any],
    execution_files: list[str],
    review_files: list[str],
    verify_files: list[str],
) -> list[str]:
    coverage = _coverage(repo_inventory, execution_files + review_files)
    return coverage or _coverage(repo_inventory, verify_files)


def _coverage(repo_inventory: dict[str, Any], files: list[str]) -> list[str]:
    return surface_coverage_for_files(files, repo_inventory=repo_inventory)


def _verify_surfaces(verify_report: dict[str, Any] | None) -> list[str]:
    values: list[str] = []
    for item in list((verify_report or {}).get("surface_results") or []):
        if not isinstance(item, dict):
            continue
        surface = str(item.get("surface") or "").strip()
        if surface and surface != "custom" and surface not in values:
            values.append(surface)
    return values


def _pass_check(details: str) -> dict[str, Any]:
    return {"status": "PASS", "details": details}


def _fail_check(*, expected: list[str], actual: list[str]) -> dict[str, Any]:
    details = f"verify surface coverage mismatch; expected={expected}; actual={actual}"
    return {
        "status": "FAIL",
        "reason": details,
        "details": details,
        "expected_surfaces": list(expected),
        "verify_surfaces": list(actual),
    }


__all__ = ["build_surface_consistency_checks"]



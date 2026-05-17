"""Gate command implementation and artifact writers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from kodawari.cli.contract.command_contract import normalize_mutating_payload
from kodawari.cli.main_support import (
    MERGED_CONTRACT_VERSION,
    _build_cli_provenance,
    _command_preflight,
    _normalized_error_payload,
    _preflight_blocked_payload,
    _write_optional_json_output,
)
from kodawari.cli.delivery.workflow_chain import resolve_gate_blocking_reason
from kodawari.gate.code_health import collect_code_health_snapshot
from kodawari.gate import GateEngine
from kodawari.gate.gate_ratchet import compare_against_baseline
from kodawari.infra.gate_artifacts import (
    _render_gate_markdown,
    write_gate_artifacts,
)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected object json: {path}")
    return payload


def _read_changed_files_from_execution_result(planning_dir: Path) -> list[str] | None:
    """Return changed_files from the planning dir's .execution_result.json.

    Returns None when the file is missing, malformed, or has no changed_files
    list. Callers can fall back to full-project scan in that case.
    """
    result_path = planning_dir / ".execution_result.json"
    if not result_path.is_file():
        return None
    try:
        payload = _read_json(result_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    raw = payload.get("changed_files")
    if not isinstance(raw, list):
        return None
    items = [str(item).strip() for item in raw if str(item).strip()]
    return items or None


def _resolve_gate_targets(args: argparse.Namespace, project_root: Path) -> list[Path]:
    # Explicit --path always wins (pre-release audits and ad-hoc checks).
    raw_paths = list(getattr(args, "path", []) or [])
    if raw_paths:
        targets: list[Path] = []
        for raw in raw_paths:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = project_root / candidate
            targets.append(candidate.resolve())
        return targets

    # --scope: auto | changed | full. Default 'auto' prefers the last task's
    # changed_files (so post-task gates don't drown in pre-existing tech debt),
    # and falls back to project_root when no .execution_result.json exists.
    scope = str(getattr(args, "scope", "auto") or "auto").strip().lower()
    if scope == "full":
        return [project_root]

    planning_dir = _resolve_gate_planning_dir(args, project_root)
    changed: list[str] | None = None
    if planning_dir is not None:
        changed = _read_changed_files_from_execution_result(planning_dir)

    if changed:
        resolved: list[Path] = []
        for rel in changed:
            candidate = Path(rel)
            if not candidate.is_absolute():
                candidate = project_root / rel
            resolved.append(candidate.resolve())
        return resolved

    if scope == "changed":
        # Hard requirement: user asked for changed-only, but we have no
        # evidence. Surface as empty so the caller reports a clean FAIL.
        raise ValueError(
            "--scope=changed requires a .execution_result.json with "
            "changed_files in the planning dir; none found"
        )

    # scope == "auto" + no evidence → full project (legacy default).
    return [project_root]


def _resolve_gate_planning_dir(args: argparse.Namespace, project_root: Path) -> Path | None:
    explicit = getattr(args, "planning_dir", None)
    if explicit:
        return Path(str(explicit)).resolve()
    feature = str(getattr(args, "feature", "") or "").strip()
    if feature:
        return (project_root / "planning" / feature).resolve()
    return None


def _gate_payload_blocking_reason(payload: dict[str, Any]) -> str:
    if str(payload.get("total_status") or "").upper() != "BLOCKED":
        return ""
    return resolve_gate_blocking_reason(payload)


def _enrich_gate_payload(
    payload: dict[str, Any],
    target_count: int,
    *,
    scope_used: str = "",
    scope_source: str = "",
) -> dict[str, Any]:
    payload["contract_version"] = MERGED_CONTRACT_VERSION
    payload["entrypoint"] = "kodawari gate"
    payload["target_count"] = target_count
    if scope_used:
        payload["scope_used"] = scope_used
    if scope_source:
        payload["scope_source"] = scope_source
    payload["blocking_reason"] = _gate_payload_blocking_reason(payload)
    return payload


def _apply_ratchet(
    payload: dict[str, Any],
    *,
    project_root: Path,
    targets: list[Path],
    baseline_path: Path,
) -> dict[str, Any]:
    baseline = _read_json(baseline_path)
    current_snapshot = collect_code_health_snapshot(project_root=project_root, targets=targets)
    ratchet = compare_against_baseline(current_snapshot, baseline).to_dict()
    payload["code_health_snapshot"] = current_snapshot
    payload["ratchet"] = ratchet
    payload["ratchet_enabled"] = True
    payload["baseline_path"] = str(baseline_path)
    if str(ratchet.get("status") or "").upper() == "FAIL":
        payload["total_status"] = "BLOCKED"
        payload["ratchet_blocking_regressions"] = int(ratchet.get("regression_count") or 0)
        regressions = list(ratchet.get("regressions") or [])
        first_metric = str(dict(regressions[0]).get("metric") or "").strip() if regressions else ""
        payload["blocking_reason"] = (
            f"code health ratchet regressed ({first_metric})" if first_metric else "code health ratchet regressed"
        )
    return payload


def _classify_scope_used(
    args: argparse.Namespace, *, project_root: Path, targets: list[Path]
) -> tuple[str, str]:
    """Return (scope_used, scope_source) strings for payload observability."""
    if list(getattr(args, "path", []) or []):
        return ("explicit", "--path")
    requested = str(getattr(args, "scope", "auto") or "auto").strip().lower()
    if requested == "full":
        return ("full", "--scope=full")
    # auto / changed — differentiate by whether we read changed_files
    if len(targets) == 1 and targets[0].resolve() == project_root.resolve():
        return ("full", "auto_fallback_no_execution_result")
    return ("changed", f"--scope={requested}:.execution_result.json")


def _cmd_gate(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    planning_dir = _resolve_gate_planning_dir(args, project_root)
    try:
        targets = _resolve_gate_targets(args, project_root)
    except ValueError as exc:
        payload = _normalized_error_payload(
            command="gate",
            project_root=project_root,
            planning_dir=planning_dir,
            error=str(exc),
            error_code="gate_scope_missing_evidence",
            remediation=[
                "Run `kodawari task-run` first so .execution_result.json exists,",
                "or pass `--scope=full` for a project-wide scan,",
                "or pass explicit `--path` targets.",
            ],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    preflight = _command_preflight(
        command="gate",
        project_root=project_root,
        planning_dir=planning_dir,
    )
    if str(preflight.get("status")) == "BLOCKED":
        print(
            json.dumps(
                _preflight_blocked_payload(
                    command="gate",
                    project_root=project_root,
                    planning_dir=planning_dir,
                    preflight=preflight,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    try:
        report = GateEngine(project_root=project_root).evaluate(targets=targets, profile_name=args.profile)
    except ValueError as exc:
        payload = _normalized_error_payload(
            command="gate",
            project_root=project_root,
            planning_dir=planning_dir,
            error=str(exc),
            error_code="gate_failed",
            remediation=["Fix the gate input path/profile and rerun `kodawari gate`."],
            preflight=preflight,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    scope_used, scope_source = _classify_scope_used(
        args, project_root=project_root, targets=targets
    )
    payload = _enrich_gate_payload(
        report.to_dict(),
        len(targets),
        scope_used=scope_used,
        scope_source=scope_source,
    )
    if getattr(args, "ratchet", False):
        if not getattr(args, "baseline", None):
            payload = _normalized_error_payload(
                command="gate",
                project_root=project_root,
                planning_dir=planning_dir,
                error="--ratchet requires --baseline",
                error_code="gate_ratchet_requires_baseline",
                remediation=["Provide a baseline snapshot path via `kodawari gate --ratchet --baseline <path>`."] ,
                preflight=preflight,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
        try:
            payload = _apply_ratchet(
                payload,
                project_root=project_root,
                targets=targets,
                baseline_path=Path(str(args.baseline)).resolve(),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            payload = _normalized_error_payload(
                command="gate",
                project_root=project_root,
                planning_dir=planning_dir,
                error=str(exc),
                error_code="gate_ratchet_failed",
                remediation=["Fix the baseline snapshot path or JSON payload and rerun `kodawari gate --ratchet`."],
                preflight=preflight,
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
    if planning_dir is not None:
        payload = write_gate_artifacts(payload, planning_dir)
    payload["preflight"] = preflight
    remediation: list[str] = []
    if payload.get("total_status") == "BLOCKED":
        remediation.append("Address the blocking gate violations and rerun `kodawari gate`.")
    if str(dict(payload.get("ratchet") or {}).get("status") or "").upper() == "FAIL":
        remediation.append("Reduce the regressed code health metrics or refresh the baseline only after real improvements.")
    payload["remediation"] = remediation
    payload["next_action"] = (
        ""
        if payload.get("total_status") != "BLOCKED"
        else "Fix the reported violations before continuing delivery."
    )
    payload["provenance"] = _build_cli_provenance(
        command="gate",
        project_root=project_root,
        planning_dir=planning_dir,
    )
    payload = normalize_mutating_payload(payload)
    _write_optional_json_output(payload, getattr(args, "output", None))

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.fail_on_block and payload.get("total_status") == "BLOCKED":
        return 2
    if str(dict(payload.get("ratchet") or {}).get("status") or "").upper() == "FAIL":
        return 2
    return 0


__all__ = ["_cmd_gate"]


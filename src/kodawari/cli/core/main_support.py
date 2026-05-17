"""Shared support helpers for the kodawari CLI entrypoint family."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any

from kodawari.cli.command_contract import (
    build_error_payload,
    build_mutating_preflight,
    normalize_mutating_payload,
)
from kodawari.cli.io_atomic import atomic_write_json, load_json_dict
from kodawari.cli.provenance import (
    build_cli_provenance as build_shared_cli_provenance,
    find_kodawari_repo_root as _find_kodawari_repo_root_impl,
    resolved_wrapper_repo_root as _resolved_wrapper_repo_root_impl,
)
from kodawari.infra.contract_version import MERGED_CONTRACT_VERSION  # noqa: F401

CLI_MAIN_MODULE = Path(__file__).with_name("main.py")
REQUIRED_PLANNING_ARTIFACTS = ["PLAN.md", "TASKS.md", "ACCEPTANCE.md", "GATE.md"]
ARTIFACT_SEMANTICS: dict[str, str] = {
    "PRD_INTAKE.json": "contract_first_prd_intake_payload",
    "REPO_INVENTORY.json": "contract_first_repo_inventory_payload",
    "ARCHITECTURE_PLAN.json": "contract_first_architecture_plan_payload",
    "PLANNING_CONVERSATION.json": "model_driven_planning_conversation_payload",
    "TASK_GRAPH.json": "contract_first_task_graph_payload",
    "TASK_CARD_ACTIVE.json": "contract_first_active_task_card_payload",
    "COMPLIANCE_REPORT.json": "contract_first_compliance_report_payload",
    "PLAN.md": "planning_scope_and_strategy",
    "TASKS.md": "execution_backlog_and_task_order",
    "ACCEPTANCE.md": "acceptance_criteria_checklist",
    "GATE.md": "human_readable_gate_decision_summary",
    "DESIGN.md": "design_decisions_snapshot",
    "REVIEW.md": "diff_aware_review_summary",
    "QA_REPORT.md": "qa_validation_summary",
    "RELEASE.md": "ship_readiness_release_summary",
    "STATUS.md": "human_readable_status_snapshot",
    ".autopilot_state.json": "machine_state_snapshot_for_status",
    ".autopilot_rounds.jsonl": "round_level_execution_trace",
    ".gate_result.json": "machine_gate_result_payload",
    ".workflow_chain.json": "develop_family_runtime_chain_snapshot",
    ".review_result.json": "machine_diff_aware_review_payload",
    ".review_evidence.json": "machine_dual_review_evidence_payload",
    ".execution_request.json": "machine_execution_request_payload",
    ".execution_result.json": "machine_execution_result_payload",
    ".review_bundle.json": "machine_peer_review_bundle_payload",
    ".verify_report.json": "machine_verify_report_payload",
    ".qa_report.json": "machine_qa_report_payload",
    ".ship_readiness.json": "machine_ship_readiness_payload",
    ".status_snapshot.json": "machine_status_snapshot_payload",
}
DEFAULT_GATE_REDLINE = {
    "file_max_lines": 1500,
    "function_max_lines": 10000,
    "nesting_max": 4,
    "complexity_max": 7,
    "complexity_warn": 7,
    "complexity_block": 10,
    "file_complexity_warn_lines": 1000,
    "file_complexity_warn_sum": 20,
    "file_complexity_block_lines": 1500,
    "file_complexity_block_sum": 30,
    "max_violations": 50,
    "severity": "WARNING",
}
LEGACY_UNSUPPORTED_REASON = (
    "Historical workflow-claude shell is not restored as a standalone runtime in kodawari. "
    "Use canonical kodawari entrypoints below."
)
LEGACY_RUNTIME_REASON = "Historical shell is routed to kodawari canonical runtime entrypoints."


def _mutating_commands() -> set[str]:
    return {
        "approve",
        "autopilot",
        "compact",
        "research",
        "develop",
        "quick-develop",
        "optimize-existing-develop",
        "gate",
        "review",
        "review-evidence",
        "execution-evidence",
        "verify",
        "qa",
        "ship-readiness",
        "prd-intake",
        "task-plan",
        "task-prepare",
        "task-run",
        "compliance-check",
        "telemetry",
        "field-report",
        "field-report-update",
        "eval-report",
        "migrate-artifacts",
        "replay-gate",
        "canary-gate",
        "incident-ingest",
    }


def _mismatched_module_repo(cwd_repo: Path) -> Path | None:
    module_repo = _resolve_repo_root(CLI_MAIN_MODULE)
    if module_repo is None or module_repo == cwd_repo:
        return None
    return module_repo


def _main_override(name: str) -> Any | None:
    main_module = sys.modules.get("kodawari.cli.main")
    if main_module is None:
        return None
    return getattr(main_module, name, None)


def _resolve_repo_root(path: Path) -> Path | None:
    override = _main_override("find_kodawari_repo_root")
    if callable(override) and override is not _find_kodawari_repo_root_impl:
        return override(path)
    return _find_kodawari_repo_root_impl(path)


def _resolve_wrapper_root() -> Path | None:
    override = _main_override("resolved_wrapper_repo_root")
    if callable(override) and override is not _resolved_wrapper_repo_root_impl:
        return override()
    return _resolved_wrapper_repo_root_impl()


def _warn_if_repo_resolution_mismatch() -> None:
    if os.environ.get("WORKFLOWCTL_SUPPRESS_REPO_WARNING") == "1":
        return

    cwd_repo = _resolve_repo_root(Path.cwd())
    if cwd_repo is None:
        return

    mismatch_override = _main_override("_mismatched_module_repo")
    if callable(mismatch_override) and mismatch_override is not _mismatched_module_repo:
        module_repo = mismatch_override(cwd_repo)
    else:
        module_repo = _mismatched_module_repo(cwd_repo)
    if module_repo is None:
        return

    wrapper_repo = _resolve_wrapper_root()
    if wrapper_repo == cwd_repo:
        return
    canonical_wrapper = (cwd_repo / "scripts" / "kodawari.ps1").resolve()
    canonical_hint = str(canonical_wrapper) if canonical_wrapper.exists() else ".\\scripts\\kodawari.ps1"

    print(
        (
            "[kodawari] warning: current directory looks like kodawari repo "
            f"'{cwd_repo}', but loaded CLI code is from '{module_repo}'. "
            f"Use '{canonical_hint} ...' or '.\\.workflow_runtime\\local-env\\.venv\\Scripts\\kodawari.exe ...' "
            "in the target repo."
        ),
        file=sys.stderr,
    )


def _repo_mismatch_guard_payload(command: str) -> dict[str, Any] | None:
    if str(command) not in _mutating_commands():
        return None
    if os.environ.get("WORKFLOWCTL_ALLOW_REPO_MISMATCH") == "1":
        return None
    cwd_repo = _resolve_repo_root(Path.cwd())
    if cwd_repo is None:
        return None
    mismatch_override = _main_override("_mismatched_module_repo")
    if callable(mismatch_override) and mismatch_override is not _mismatched_module_repo:
        module_repo = mismatch_override(cwd_repo)
    else:
        module_repo = _mismatched_module_repo(cwd_repo)
    if module_repo is None:
        return None
    wrapper_repo = _resolve_wrapper_root()
    if wrapper_repo == cwd_repo:
        return None
    canonical_hint = (cwd_repo / "scripts" / "kodawari.ps1").resolve()
    return {
        "error": "repo_resolution_mismatch",
        "command": str(command),
        "cwd_repo_root": str(cwd_repo),
        "module_repo_root": str(module_repo),
        "recommended_entrypoint": str(canonical_hint),
        "hint": "Use repo-local wrapper from target repo or set WORKFLOWCTL_ALLOW_REPO_MISMATCH=1 to override.",
    }


def _build_cli_provenance(
    *,
    command: str,
    project_root: Path | None = None,
    planning_dir: Path | None = None,
    resolved_planning_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    return build_shared_cli_provenance(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        resolved_planning_dirs=resolved_planning_dirs,
        module_file=CLI_MAIN_MODULE,
    )


def _command_preflight(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    require_existing_planning_dir: bool = False,
) -> dict[str, Any]:
    required_modules = (
        ["jsonschema"] if command in {"telemetry", "field-report", "field-report-update", "eval-report"} else []
    )
    return build_mutating_preflight(
        command=command,
        project_root=project_root,
        planning_dir=planning_dir,
        module_file=CLI_MAIN_MODULE,
        required_modules=required_modules,
        require_existing_planning_dir=require_existing_planning_dir,
    )


def _preflight_blocked_payload(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    return normalize_mutating_payload(
        build_error_payload(
            command=command,
            project_root=project_root,
            planning_dir=planning_dir,
            module_file=CLI_MAIN_MODULE,
            error=str(preflight.get("blocking_reason") or "preflight blocked"),
            error_code="preflight_failed",
            blocking_reason=str(preflight.get("blocking_reason") or "preflight blocked"),
            remediation=list(preflight.get("remediation") or []),
            next_action=str(preflight.get("next_action") or ""),
            preflight=preflight,
        )
    )


def _normalized_error_payload(
    *,
    command: str,
    project_root: Path,
    planning_dir: Path | None,
    error: str,
    error_code: str,
    remediation: list[str] | None = None,
    next_action: str | None = None,
    preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return normalize_mutating_payload(
        build_error_payload(
            command=command,
            project_root=project_root,
            planning_dir=planning_dir,
            module_file=CLI_MAIN_MODULE,
            error=error,
            error_code=error_code,
            remediation=list(remediation or []),
            next_action="" if next_action is None else next_action,
            preflight=preflight,
        )
    )


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        return load_json_dict(path, required=False)
    except ValueError:
        return None


def _load_optional_json_dict(path: Path) -> dict[str, Any] | None:
    return _load_json_dict(path)


def _write_json_output(output: str | None, payload: dict[str, Any]) -> None:
    if not output:
        return
    output_path = Path(output).resolve()
    atomic_write_json(output_path, payload)


def _write_optional_json_output(payload: dict[str, Any], output: str | None) -> None:
    _write_json_output(output, payload)


def _resolve_feature_planning_dir(*, project_root: Path, feature: str, planning_dir: str | None) -> Path:
    if planning_dir:
        return Path(planning_dir).resolve()
    return (project_root / "planning" / feature).resolve()


def _add_project_root_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project-root", default=".")


__all__ = [
    "ARTIFACT_SEMANTICS",
    "CLI_MAIN_MODULE",
    "DEFAULT_GATE_REDLINE",
    "LEGACY_RUNTIME_REASON",
    "LEGACY_UNSUPPORTED_REASON",
    "MERGED_CONTRACT_VERSION",
    "REQUIRED_PLANNING_ARTIFACTS",
    "_add_project_root_argument",
    "_build_cli_provenance",
    "_command_preflight",
    "_load_json_dict",
    "_load_optional_json_dict",
    "_mismatched_module_repo",
    "_normalized_error_payload",
    "_preflight_blocked_payload",
    "_repo_mismatch_guard_payload",
    "_resolve_feature_planning_dir",
    "_warn_if_repo_resolution_mismatch",
    "_write_json_output",
    "_write_optional_json_output",
]

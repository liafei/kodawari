"""Compatibility facade for gate checker modules."""

from __future__ import annotations

from pathlib import Path
import subprocess

from kodawari.gate.ast_checker import check_source_of_truth_conflict_ast
from kodawari.gate.checker_compliance import build_contract_compliance_report
from kodawari.gate.checker_metrics import (
    FunctionMetric,
    run_file_length_checker,
    run_file_redline_checker,
    run_function_metrics_checker,
)
from kodawari.gate.checker_scope_contract import (
    _decode_process_output as _decode_process_output_impl,
    discover_project_schema_files,
    check_layer_boundary_simple,
    check_scope_drift,
    run_source_of_truth_conflict_check,
)
from kodawari.gate.checker_semantics import (
    check_cache_consistency,
    check_invariant_proof,
    check_prd_coverage,
    check_runtime_contract_scatter,
)


def _decode_process_output(value: object) -> str:
    return _decode_process_output_impl(value)


def _git_added_lines(project_root: Path, rel_path: str) -> list[str] | None:
    command = [
        "git",
        "-C",
        str(project_root),
        "diff",
        "--unified=0",
        "--no-color",
        "--",
        rel_path,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    stdout = _decode_process_output(getattr(result, "stdout", b""))
    added: list[str] = []
    for line in stdout.splitlines():
        if not line.startswith("+"):
            continue
        if line.startswith("+++"):
            continue
        added.append(line[1:])
    return added


def check_source_of_truth_conflict(
    changed_files: list[str],
    project_root: Path,
    declared_sot: list[str],
    declared_sot_canonical: list[str] | None = None,
) -> list[str]:
    return run_source_of_truth_conflict_check(
        changed_files,
        project_root,
        declared_sot,
        declared_sot_canonical=declared_sot_canonical,
        git_added_lines_fn=_git_added_lines,
        ast_checker_fn=check_source_of_truth_conflict_ast,
    )


__all__ = [
    "FunctionMetric",
    "_git_added_lines",
    "build_contract_compliance_report",
    "check_cache_consistency",
    "check_invariant_proof",
    "check_layer_boundary_simple",
    "check_prd_coverage",
    "check_runtime_contract_scatter",
    "check_scope_drift",
    "check_source_of_truth_conflict",
    "discover_project_schema_files",
    "run_file_length_checker",
    "run_file_redline_checker",
    "run_function_metrics_checker",
]

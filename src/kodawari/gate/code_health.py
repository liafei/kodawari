"""Repository-level code health snapshot helpers."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from typing import Any

from code_redline import REDLINE

from kodawari.gate.checker_compliance import build_contract_compliance_report
from kodawari.gate.checker_metrics import run_file_length_checker, run_function_metrics_checker
from kodawari.gate.checkers import discover_project_schema_files
from kodawari.gate.engine import discover_python_files
from kodawari.gate.models import GateThresholds


# code_health still keeps this legacy-shaped default to drive the
# historical file-length & function-length checkers (these report the
# per-file-500-lines / per-function-50-lines snapshot metrics that
# ratcheting dashboards track). The modern file-shape bands below come
# straight from code_redline.
_DEFAULT_THRESHOLDS = GateThresholds(
    file_max_lines=1000,
    function_max_lines=50,
    nesting_max=REDLINE.nesting_max,
    complexity_max=6,
    max_violations=100000,
    severity="ERROR",
)
_PLACEHOLDER_FILES = {"", "<unknown>", "<runtime>", "<tool>"}
_COMPLEXITY_WARN_MIN = REDLINE.complexity_warn
_COMPLEXITY_WARN_MAX = REDLINE.complexity_block
_COMPLEXITY_BLOCK_MIN = REDLINE.complexity_block + 1
_FILE_COMPLEXITY_WARN_LINES = REDLINE.file_complexity_warn_lines
_FILE_COMPLEXITY_WARN_SUM = REDLINE.file_complexity_warn_sum
_FILE_COMPLEXITY_BLOCK_LINES = REDLINE.file_complexity_block_lines
_FILE_COMPLEXITY_BLOCK_SUM = REDLINE.file_complexity_block_sum
_METRIC_DEPRECATIONS = {
    "files_over_1000_lines": (
        "Deprecated: use files_large_and_complex_warn/files_large_and_complex_block "
        "or files_large_declarative_over_1500."
    ),
    "functions_over_50_lines": (
        "Deprecated: use functions_complexity_7_to_10/functions_complexity_over_10."
    ),
    "functions_complexity_over_6": (
        "Deprecated: use functions_complexity_7_to_10/functions_complexity_over_10."
    ),
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_head(project_root: Path) -> str:
    try:
        run = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            check=False,
        )
    except OSError:
        return "UNKNOWN"
    if run.returncode != 0:
        return "UNKNOWN"
    text = str(run.stdout or "").strip()
    return text or "UNKNOWN"


def _relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _thresholds(*, file_max_lines: int | None = None) -> GateThresholds:
    return GateThresholds(
        file_max_lines=int(file_max_lines or _DEFAULT_THRESHOLDS.file_max_lines),
        function_max_lines=_DEFAULT_THRESHOLDS.function_max_lines,
        nesting_max=_DEFAULT_THRESHOLDS.nesting_max,
        complexity_max=_DEFAULT_THRESHOLDS.complexity_max,
        max_violations=_DEFAULT_THRESHOLDS.max_violations,
        severity=_DEFAULT_THRESHOLDS.severity,
    )


def _count_metric_violations(report: dict[str, Any], *, metric: str) -> int:
    items = list(report.get("violations") or [])
    return sum(1 for item in items if str(item.get("metric") or "").strip() == metric)


def _metric_actuals(report: dict[str, Any], *, metric: str) -> list[int]:
    actuals: list[int] = []
    for item in list(report.get("violations") or []):
        if str(item.get("metric") or "").strip() != metric:
            continue
        raw_actual = item.get("actual")
        if isinstance(raw_actual, bool):
            continue
        if isinstance(raw_actual, int):
            actuals.append(raw_actual)
            continue
        if isinstance(raw_actual, float) and raw_actual.is_integer():
            actuals.append(int(raw_actual))
    return actuals


def _count_metric_band(
    report: dict[str, Any],
    *,
    metric: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    values = _metric_actuals(report, metric=metric)
    return sum(
        1
        for value in values
        if (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
    )


def _node_complexity(node: ast.AST) -> int:
    complexity = 1
    for child in ast.walk(node):
        if isinstance(
            child,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Try,
                ast.ExceptHandler,
                ast.With,
                ast.AsyncWith,
                ast.IfExp,
                ast.Match,
                ast.comprehension,
            ),
        ):
            complexity += 1
        if isinstance(child, ast.BoolOp):
            complexity += max(0, len(child.values) - 1)
    return complexity


def _file_shape_counts(files: list[Path]) -> dict[str, int]:
    warn_count = 0
    block_count = 0
    declarative_count = 0
    for path in files:
        try:
            source = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        line_count = len(source.splitlines())
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        complexity_sum = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexity_sum += _node_complexity(node)
        if line_count > _FILE_COMPLEXITY_WARN_LINES and complexity_sum > _FILE_COMPLEXITY_WARN_SUM:
            warn_count += 1
        if line_count > _FILE_COMPLEXITY_BLOCK_LINES and complexity_sum > _FILE_COMPLEXITY_BLOCK_SUM:
            block_count += 1
        if line_count > _FILE_COMPLEXITY_BLOCK_LINES and complexity_sum <= _FILE_COMPLEXITY_WARN_SUM:
            declarative_count += 1
    return {
        "files_large_and_complex_warn": warn_count,
        "files_large_and_complex_block": block_count,
        "files_large_declarative_over_1500": declarative_count,
    }


def _check_by_name(report: dict[str, Any], check_name: str) -> dict[str, Any] | None:
    for item in list(report.get("checks") or []):
        if str(item.get("check_name") or "").strip() == check_name:
            return dict(item)
    return None


def _active_check(report: dict[str, Any], check_name: str) -> dict[str, Any] | None:
    check = _check_by_name(report, check_name)
    if not isinstance(check, dict):
        return None
    status = str(check.get("status") or "").upper()
    return check if status in {"WARN", "FAIL"} else None


def _check_evidence_items(report: dict[str, Any], check_name: str) -> list[dict[str, Any]]:
    check = _active_check(report, check_name)
    if not isinstance(check, dict):
        return []
    return [dict(item) for item in list(check.get("evidence") or []) if isinstance(item, dict)]


def _evidence_file(item: dict[str, Any]) -> str:
    file_value = str(item.get("file") or "").strip()
    return "" if file_value in _PLACEHOLDER_FILES else file_value


def _count_check_evidence(report: dict[str, Any], check_name: str) -> int | None:
    return len(_check_evidence_items(report, check_name))


def _unique_check_files(report: dict[str, Any], check_name: str) -> int | None:
    files = {_evidence_file(item) for item in _check_evidence_items(report, check_name)}
    files.discard("")
    return len(files)


def _metadata_field(item: dict[str, Any], key: str) -> str:
    return str(dict(item.get("metadata") or {}).get(key) or "").strip()


def _is_runtime_contract_structural_conflict(item: dict[str, Any]) -> bool:
    rule = str(item.get("rule") or "").strip()
    if rule == "runtime_contract_scatter.structural_conflict":
        return True
    return rule.startswith("runtime_contract_scatter.") and "conflict" in rule and "metadata_drift" not in rule


def _runtime_contract_scatter_fields(report: dict[str, Any]) -> set[str]:
    return {
        _metadata_field(item, "field")
        for item in _check_evidence_items(report, "runtime_contract_scatter")
        if _metadata_field(item, "field") and _is_runtime_contract_structural_conflict(item)
    }


def _count_runtime_contract_scatter_conflicts(report: dict[str, Any]) -> int | None:
    return len(_runtime_contract_scatter_fields(report))


def _duplication_block_count(payload: dict[str, Any]) -> int | None:
    for key in ("total_duplicate_blocks", "duplicate_block_count"):
        raw = payload.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
    return None


def _duplication_payload(project_root: Path, targets: list[Path]) -> dict[str, Any]:
    try:
        from kodawari.gate.checker_duplication import run_duplication_checker
    except Exception as exc:
        return {
            "status": "SKIP",
            "checker": "duplication",
            "error_code": "checker_unavailable",
            "details": f"duplication checker unavailable: {exc.__class__.__name__}",
            "total_duplicate_blocks": None,
            "tool_versions": {"python": sys.version.split()[0]},
            "evidence": [],
        }
    payload = run_duplication_checker(targets, project_root=project_root)
    if hasattr(payload, "to_dict"):
        payload = payload.to_dict()
    if not isinstance(payload, dict):
        return {
            "status": "WARN",
            "checker": "duplication",
            "error_code": "checker_invalid_payload",
            "details": "duplication checker returned a non-dict payload",
            "total_duplicate_blocks": None,
            "tool_versions": {"python": sys.version.split()[0]},
            "evidence": [],
        }
    payload.setdefault("tool_versions", {"python": sys.version.split()[0]})
    return payload


def _synthetic_task_graph(changed_files: list[str]) -> dict[str, Any]:
    return {
        "tasks": [
            {
                "task_id": "BASELINE",
                "task_name": "code_health_snapshot",
                "layer_owner": "repo_health",
                "core_files": list(changed_files),
                "invariants": ["Preserve repository-level architectural boundaries."],
                "test_proof": "Repository health snapshot only.",
            }
        ]
    }


def _compliance_report(project_root: Path, changed_files: list[str]) -> dict[str, Any]:
    schema_files = discover_project_schema_files(project_root)
    return build_contract_compliance_report(
        project_root=project_root,
        changed_files=changed_files,
        task_graph=_synthetic_task_graph(changed_files),
        task_card={"files_to_change": list(changed_files), "invariants": ["Preserve repository-level architectural boundaries."]},
        allowed_files=list(changed_files),
        prd_intake={
            "business_outcome": "Maintain repository code health baseline",
            "source_of_truth": ["db.primary"],
            "source_of_truth_canonical": ["db.primary"],
            "layers": ["repo_health"],
            "path_type": "mixed",
        },
        review_evidence={"status": "PASS", "details": "code health snapshot"},
        schema_files=schema_files,
    )


def _gate_metric_reports(files: list[Path], project_root: Path) -> dict[str, dict[str, Any]]:
    return {
        "files_over_500_lines": run_file_length_checker(
            files,
            project_root=project_root,
            thresholds=_thresholds(file_max_lines=500),
        ).to_dict(),
        "files_over_1000_lines": run_file_length_checker(
            files,
            project_root=project_root,
            thresholds=_thresholds(file_max_lines=1000),
        ).to_dict(),
        "files_over_1500_lines": run_file_length_checker(
            files,
            project_root=project_root,
            thresholds=_thresholds(file_max_lines=1500),
        ).to_dict(),
        "function_metrics": run_function_metrics_checker(
            files,
            project_root=project_root,
            thresholds=_DEFAULT_THRESHOLDS,
        ).to_dict(),
    }


def _snapshot_metrics(
    *,
    gate_reports: dict[str, dict[str, Any]],
    compliance_report: dict[str, Any],
    duplication_payload: dict[str, Any],
    file_shape_counts: dict[str, int],
) -> dict[str, int | None]:
    function_metrics = gate_reports["function_metrics"]
    return {
        "files_over_500_lines": len(list(gate_reports["files_over_500_lines"].get("violations") or [])),
        "files_over_1000_lines": len(list(gate_reports["files_over_1000_lines"].get("violations") or [])),
        "files_over_1500_lines": len(list(gate_reports["files_over_1500_lines"].get("violations") or [])),
        "files_large_and_complex_warn": int(file_shape_counts["files_large_and_complex_warn"]),
        "files_large_and_complex_block": int(file_shape_counts["files_large_and_complex_block"]),
        "files_large_declarative_over_1500": int(file_shape_counts["files_large_declarative_over_1500"]),
        "functions_over_50_lines": _count_metric_violations(function_metrics, metric="function_lines"),
        "functions_complexity_7_to_10": _count_metric_band(
            function_metrics,
            metric="complexity",
            minimum=_COMPLEXITY_WARN_MIN,
            maximum=_COMPLEXITY_WARN_MAX,
        ),
        "functions_complexity_over_10": _count_metric_band(
            function_metrics,
            metric="complexity",
            minimum=_COMPLEXITY_BLOCK_MIN,
        ),
        "functions_complexity_over_6": _count_metric_violations(function_metrics, metric="complexity"),
        "total_duplicate_blocks": _duplication_block_count(duplication_payload),
        "layer_boundary_violations": _count_check_evidence(compliance_report, "layer_boundary"),
        "layer_boundary_debt_files": _unique_check_files(compliance_report, "layer_boundary_debt"),
        "sot_conflict_count": _count_check_evidence(compliance_report, "source_of_truth_conflict"),
        "runtime_contract_scatter_conflicts": _count_runtime_contract_scatter_conflicts(compliance_report),
        "import_rule_violations": _count_check_evidence(compliance_report, "import_rules"),
        "domain_sot_conflict_count": _count_check_evidence(compliance_report, "domain_source_of_truth"),
    }


def collect_code_health_snapshot(
    *,
    project_root: Path,
    targets: list[Path],
) -> dict[str, Any]:
    resolved_root = project_root.resolve()
    resolved_targets = [path.resolve() for path in targets]
    files = discover_python_files(resolved_targets)
    changed_files = [_relative_path(path, resolved_root) for path in files]
    gate_reports = _gate_metric_reports(files, resolved_root)
    compliance_report = _compliance_report(resolved_root, changed_files)
    duplication_payload = _duplication_payload(resolved_root, resolved_targets)
    file_shape_counts = _file_shape_counts(files)

    return {
        "schema_version": "code_health.baseline.v1",
        "generated_at": _utc_now_iso(),
        "source_commit": _git_head(resolved_root),
        "project_root": str(resolved_root),
        "targets": [str(path) for path in resolved_targets],
        "tool_versions": {
            "python": sys.version.split()[0],
            "duplication": dict(duplication_payload.get("tool_versions") or {}),
        },
        "metrics": _snapshot_metrics(
            gate_reports=gate_reports,
            compliance_report=compliance_report,
            duplication_payload=duplication_payload,
            file_shape_counts=file_shape_counts,
        ),
        "metric_deprecations": dict(_METRIC_DEPRECATIONS),
        "gate_metrics": gate_reports,
        "compliance_report": compliance_report,
        "duplication": duplication_payload,
    }


__all__ = ["collect_code_health_snapshot"]

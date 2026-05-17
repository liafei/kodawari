"""File and function metric checkers for gate evaluation."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from code_redline import RedlineStandard, Tier, evaluate_file

from kodawari.gate.models import CheckerResult, GateThresholds, Violation, derive_item_status

_BLOCK_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Match,
    ast.ExceptHandler,
)


@dataclass
class FunctionMetric:
    symbol: str
    start_line: int
    end_line: int
    line_count: int
    nesting: int
    complexity: int


class _FunctionCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self._scope: list[str] = []
        self.metrics: list[FunctionMetric] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._capture(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._capture(node)

    def _capture(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        symbol = ".".join(self._scope + [node.name]) if self._scope else node.name
        start = int(getattr(node, "lineno", 1) or 1)
        end = int(getattr(node, "end_lineno", start) or start)
        line_count = max(1, end - start + 1)
        nesting = _compute_max_nesting(node)
        complexity = _compute_complexity(node)
        self.metrics.append(
            FunctionMetric(
                symbol=symbol,
                start_line=start,
                end_line=end,
                line_count=line_count,
                nesting=nesting,
                complexity=complexity,
            )
        )
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()


def _compute_max_nesting(node: ast.AST, depth: int = 0) -> int:
    max_depth = depth
    for child in ast.iter_child_nodes(node):
        child_depth = depth + 1 if isinstance(child, _BLOCK_NODES) else depth
        max_depth = max(max_depth, _compute_max_nesting(child, child_depth))
    return max_depth


def _compute_complexity(node: ast.AST) -> int:
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


def _relative_path(path: Path, project_root: Path) -> str:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _as_int_or(value: int | None, fallback: int) -> int:
    return fallback if value is None else int(value)


def _file_redline_standard(thresholds: GateThresholds) -> RedlineStandard:
    complexity_block = max(1, _as_int_or(thresholds.complexity_block, thresholds.complexity_max))
    complexity_warn = _as_int_or(thresholds.complexity_warn, max(0, complexity_block - 1))
    if complexity_warn >= complexity_block:
        complexity_warn = max(0, complexity_block - 1)
    if complexity_warn >= complexity_block:
        complexity_block = complexity_warn + 1

    file_warn_lines = _as_int_or(thresholds.file_complexity_warn_lines, thresholds.file_max_lines)
    file_warn_sum = _as_int_or(thresholds.file_complexity_warn_sum, 0)
    file_block_lines = _as_int_or(thresholds.file_complexity_block_lines, file_warn_lines)
    file_block_sum = _as_int_or(thresholds.file_complexity_block_sum, file_warn_sum)
    if file_warn_lines > file_block_lines:
        file_warn_lines = file_block_lines
    if file_warn_sum > file_block_sum:
        file_warn_sum = file_block_sum

    return RedlineStandard(
        nesting_max=thresholds.nesting_max,
        complexity_warn=complexity_warn,
        complexity_block=complexity_block,
        file_complexity_warn_lines=file_warn_lines,
        file_complexity_warn_sum=file_warn_sum,
        file_complexity_block_lines=file_block_lines,
        file_complexity_block_sum=file_block_sum,
        max_violations=thresholds.max_violations,
    )


def run_file_length_checker(
    files: Iterable[Path],
    *,
    project_root: Path,
    thresholds: GateThresholds,
) -> CheckerResult:
    checker = "file_length"
    violations: list[Violation] = []
    file_list = list(files)
    for path in file_list:
        try:
            line_count = len(path.read_text(encoding="utf-8-sig").splitlines())
        except UnicodeDecodeError:
            continue
        if line_count > thresholds.file_max_lines:
            rel = _relative_path(path, project_root)
            violations.append(
                Violation(
                    checker=checker,
                    path=rel,
                    line=1,
                    symbol=None,
                    metric="file_lines",
                    actual=line_count,
                    limit=thresholds.file_max_lines,
                    severity=thresholds.severity,
                    message=f"{rel} has {line_count} lines; limit is {thresholds.file_max_lines}.",
                )
            )

    return CheckerResult(
        checker=checker,
        status=derive_item_status(len(violations), thresholds.max_violations),
        checked_files=len(file_list),
        violations=violations,
    )


def run_file_redline_checker(
    files: Iterable[Path],
    *,
    project_root: Path,
    thresholds: GateThresholds,
    report_tiers: set[Tier],
) -> CheckerResult:
    """Evaluate canonical file-shape redlines while keeping the historical
    ``file_length`` checker surface for compatibility."""

    checker = "file_length"
    violations: list[Violation] = []
    file_list = list(files)
    standard = _file_redline_standard(thresholds)

    for path in file_list:
        source = _read_python_source(path)
        if source is None:
            continue
        line_count = len(source.splitlines())
        tree, syntax_error = _parse_tree(source)
        if syntax_error is not None or tree is None:
            # Let function_metrics own parse errors so gate output stays
            # deduplicated and file-shape checks remain semantic-only.
            continue
        complexity_sum = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexity_sum += _compute_complexity(node)
        tier = evaluate_file(line_count=line_count, complexity_sum=complexity_sum, std=standard)
        if tier not in report_tiers:
            continue

        if tier == Tier.BLOCK:
            line_limit = standard.file_complexity_block_lines
            complexity_limit = standard.file_complexity_block_sum
        else:
            line_limit = standard.file_complexity_warn_lines
            complexity_limit = standard.file_complexity_warn_sum

        rel = _relative_path(path, project_root)
        violations.append(
            Violation(
                checker=checker,
                path=rel,
                line=1,
                symbol=None,
                metric="file_shape",
                actual=line_count,
                limit=line_limit,
                severity=thresholds.severity,
                message=(
                    f"{rel} is {tier.value.lower()} by file-shape redline: "
                    f"{line_count} lines and complexity-sum {complexity_sum}; "
                    f"thresholds are > {line_limit} lines and > {complexity_limit} complexity."
                ),
            )
        )

    return CheckerResult(
        checker=checker,
        status=derive_item_status(len(violations), thresholds.max_violations),
        checked_files=len(file_list),
        violations=violations,
    )


def _read_python_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return None


def _parse_tree(source: str) -> tuple[ast.AST | None, SyntaxError | None]:
    try:
        return ast.parse(source), None
    except SyntaxError as exc:
        return None, exc


def _parse_error_violation(
    *,
    checker: str,
    path: Path,
    project_root: Path,
    thresholds: GateThresholds,
    message: str,
    line: int = 1,
) -> Violation:
    rel = _relative_path(path, project_root)
    return Violation(
        checker=checker,
        path=rel,
        line=line,
        symbol=None,
        metric="parse_error",
        actual=1,
        limit=0,
        severity=thresholds.severity,
        message=f"Unable to parse {rel}: {message}",
    )


def _function_line_violation(
    *,
    checker: str,
    rel_path: str,
    metric: FunctionMetric,
    thresholds: GateThresholds,
) -> Violation | None:
    if metric.line_count <= thresholds.function_max_lines:
        return None
    return Violation(
        checker=checker,
        path=rel_path,
        line=metric.start_line,
        symbol=metric.symbol,
        metric="function_lines",
        actual=metric.line_count,
        limit=thresholds.function_max_lines,
        severity=thresholds.severity,
        message=f"Function {metric.symbol} has {metric.line_count} lines; limit is {thresholds.function_max_lines}.",
    )


def _nesting_violation(
    *,
    checker: str,
    rel_path: str,
    metric: FunctionMetric,
    thresholds: GateThresholds,
) -> Violation | None:
    if metric.nesting <= thresholds.nesting_max:
        return None
    return Violation(
        checker=checker,
        path=rel_path,
        line=metric.start_line,
        symbol=metric.symbol,
        metric="nesting",
        actual=metric.nesting,
        limit=thresholds.nesting_max,
        severity=thresholds.severity,
        message=f"Function {metric.symbol} nesting depth {metric.nesting} exceeds {thresholds.nesting_max}.",
    )


def _complexity_violation(
    *,
    checker: str,
    rel_path: str,
    metric: FunctionMetric,
    thresholds: GateThresholds,
) -> Violation | None:
    if metric.complexity <= thresholds.complexity_max:
        return None
    return Violation(
        checker=checker,
        path=rel_path,
        line=metric.start_line,
        symbol=metric.symbol,
        metric="complexity",
        actual=metric.complexity,
        limit=thresholds.complexity_max,
        severity=thresholds.severity,
        message=f"Function {metric.symbol} complexity {metric.complexity} exceeds {thresholds.complexity_max}.",
    )


def _function_metric_violations(
    *,
    checker: str,
    rel_path: str,
    metric: FunctionMetric,
    thresholds: GateThresholds,
) -> list[Violation]:
    violations: list[Violation] = []
    for candidate in (
        _function_line_violation(checker=checker, rel_path=rel_path, metric=metric, thresholds=thresholds),
        _nesting_violation(checker=checker, rel_path=rel_path, metric=metric, thresholds=thresholds),
        _complexity_violation(checker=checker, rel_path=rel_path, metric=metric, thresholds=thresholds),
    ):
        if candidate is not None:
            violations.append(candidate)
    return violations


def _collect_file_function_metric_violations(
    *,
    checker: str,
    path: Path,
    project_root: Path,
    thresholds: GateThresholds,
) -> list[Violation]:
    source = _read_python_source(path)
    if source is None:
        return []
    tree, syntax_error = _parse_tree(source)
    if syntax_error is not None:
        return [
            _parse_error_violation(
                checker=checker,
                path=path,
                project_root=project_root,
                thresholds=thresholds,
                message=str(syntax_error),
                line=int(getattr(syntax_error, "lineno", 1) or 1),
            )
        ]
    if tree is None:
        return []
    collector = _FunctionCollector()
    collector.visit(tree)
    rel_path = _relative_path(path, project_root)
    violations: list[Violation] = []
    for metric in collector.metrics:
        violations.extend(
            _function_metric_violations(
                checker=checker,
                rel_path=rel_path,
                metric=metric,
                thresholds=thresholds,
            )
        )
    return violations


def _parse_error_violations_for_source(
    *,
    checker: str,
    path: Path,
    project_root: Path,
    thresholds: GateThresholds,
) -> list[Violation]:
    source = _read_python_source(path)
    if source is None:
        return []
    _, syntax_error = _parse_tree(source)
    if syntax_error is None:
        return []
    return [
        _parse_error_violation(
            checker=checker,
            path=path,
            project_root=project_root,
            thresholds=thresholds,
            message=str(syntax_error),
            line=int(getattr(syntax_error, "lineno", 1) or 1),
        )
    ]


def _collect_tree_metric_violations(
    *,
    checker: str,
    path: Path,
    project_root: Path,
    thresholds: GateThresholds,
) -> list[Violation]:
    source = _read_python_source(path)
    if source is None:
        return []
    tree, syntax_error = _parse_tree(source)
    if syntax_error is not None or tree is None:
        return _parse_error_violations_for_source(
            checker=checker,
            path=path,
            project_root=project_root,
            thresholds=thresholds,
        )
    collector = _FunctionCollector()
    collector.visit(tree)
    rel_path = _relative_path(path, project_root)
    violations: list[Violation] = []
    for metric in collector.metrics:
        violations.extend(
            _function_metric_violations(
                checker=checker,
                rel_path=rel_path,
                metric=metric,
                thresholds=thresholds,
            )
        )
    return violations


def run_function_metrics_checker(
    files: Iterable[Path],
    *,
    project_root: Path,
    thresholds: GateThresholds,
) -> CheckerResult:
    checker = "function_metrics"
    file_list = [path for path in files if path.suffix == ".py"]
    violations: list[Violation] = []
    for path in file_list:
        violations.extend(
            _collect_tree_metric_violations(
                checker=checker,
                path=path,
                project_root=project_root,
                thresholds=thresholds,
            )
        )
    return CheckerResult(
        checker=checker,
        status=derive_item_status(len(violations), thresholds.max_violations),
        checked_files=len(file_list),
        violations=violations,
    )


__all__ = [
    "FunctionMetric",
    "run_file_redline_checker",
    "run_file_length_checker",
    "run_function_metrics_checker",
]

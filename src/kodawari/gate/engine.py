"""Gate engine implementation for quality gate checks."""

from __future__ import annotations

from pathlib import Path

from code_redline import Tier

from kodawari.gate.checkers import run_file_redline_checker, run_function_metrics_checker
from kodawari.gate.models import CheckerResult, GateEvaluation, GateMode, GateProfile, GateStatus, GateThresholds
from kodawari.gate.policy_loader import load_gate_policy
from kodawari.gate.profiles import get_profile


_SKIP_DIRS = {
    ".git",
    ".hg",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    # Phase B isolation workspaces — per-task copies of project_root live under
    # planning/<feature>/.parallel_workers/<backend>/<task-hex>/. They contain
    # duplicates of real source files; scanning them would double-count
    # violations from files that were already counted in the real tree.
    ".parallel_workers",
}


def _is_python_file(path: Path) -> bool:
    return path.suffix == ".py"


def _is_skipped_path(path: Path) -> bool:
    return any(part in _SKIP_DIRS for part in path.parts)


def _iter_python_files(target: Path) -> list[Path]:
    if target.is_file():
        return [target.resolve()] if _is_python_file(target) else []
    if not target.exists():
        return []
    found: list[Path] = []
    for path in target.rglob("*.py"):
        if _is_skipped_path(path):
            continue
        found.append(path.resolve())
    return found


def discover_python_files(targets: list[Path]) -> list[Path]:
    found: list[Path] = []
    for target in targets:
        found.extend(_iter_python_files(target))
    unique = sorted({path for path in found})
    return unique


def _evaluate_total_status(total_violations: int, mode: GateMode) -> tuple[GateStatus, int]:
    if mode == GateMode.ADVISORY:
        return GateStatus.PASS, 0
    blocked = total_violations > 0
    total_status = GateStatus.BLOCKED if blocked else GateStatus.PASS
    blocking_violations = total_violations if blocked else 0
    return total_status, blocking_violations


def _file_redline_report_tiers(profile: GateProfile) -> set[Tier]:
    if profile.mode == GateMode.ADVISORY:
        return {Tier.WARN, Tier.BLOCK}
    return {Tier.BLOCK}


class GateEngine:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def _evaluate_uniform(
        self,
        files: list[Path],
        profile: GateProfile,
    ) -> GateEvaluation:
        """Original behavior: one set of thresholds for all files."""
        file_result = run_file_redline_checker(
            files,
            project_root=self.project_root,
            thresholds=profile.thresholds,
            report_tiers=_file_redline_report_tiers(profile),
        )
        function_result = run_function_metrics_checker(
            files,
            project_root=self.project_root,
            thresholds=profile.thresholds,
        )
        checker_results = [file_result, function_result]
        total_violations = sum(len(item.violations) for item in checker_results)
        total_status, blocking_violations = _evaluate_total_status(
            total_violations, profile.mode
        )
        return GateEvaluation(
            profile=profile,
            total_status=total_status,
            checker_results=checker_results,
            scanned_files=len(files),
            total_violations=total_violations,
            blocking_violations=blocking_violations,
        )

    def _evaluate_with_policy(
        self,
        files: list[Path],
        profile: GateProfile,
        policy: object,
    ) -> GateEvaluation:
        """Policy-aware evaluation: per-file thresholds and skip_checkers."""
        from kodawari.gate.models import derive_item_status

        all_file_violations: list = []
        all_func_violations: list = []
        file_checked_count = 0
        func_checked_count = 0
        applied_thresholds: list[GateThresholds] = []

        for path in files:
            try:
                rel = path.relative_to(self.project_root).as_posix()
            except ValueError:
                rel = path.as_posix()
            thresholds = policy.effective_thresholds(rel)  # type: ignore[union-attr]
            skip = policy.skip_checkers_for_file(rel)  # type: ignore[union-attr]
            applied_thresholds.append(thresholds)

            if "file_length" not in skip:
                result = run_file_redline_checker(
                    [path],
                    project_root=self.project_root,
                    thresholds=thresholds,
                    report_tiers=_file_redline_report_tiers(profile),
                )
                file_checked_count += 1
                all_file_violations.extend(result.violations)

            if "function_metrics" not in skip:
                result = run_function_metrics_checker(
                    [path],
                    project_root=self.project_root,
                    thresholds=thresholds,
                )
                func_checked_count += 1
                all_func_violations.extend(result.violations)

        # Reconstruct CheckerResult objects for the combined results
        base_thresholds = profile.thresholds  # type: ignore[union-attr]
        aggregate_max_violations = (
            min(th.max_violations for th in applied_thresholds)
            if applied_thresholds else base_thresholds.max_violations
        )
        aggregate_severity = (
            applied_thresholds[0].severity
            if applied_thresholds else base_thresholds.severity
        )
        file_checker = CheckerResult(
            checker="file_length",
            status=derive_item_status(len(all_file_violations), aggregate_max_violations),
            checked_files=file_checked_count,
            violations=all_file_violations,
        )
        func_checker = CheckerResult(
            checker="function_metrics",
            status=derive_item_status(len(all_func_violations), aggregate_max_violations),
            checked_files=func_checked_count,
            violations=all_func_violations,
        )
        checker_results = [file_checker, func_checker]
        total_violations = len(all_file_violations) + len(all_func_violations)
        total_status, blocking_violations = _evaluate_total_status(
            total_violations, profile.mode
        )
        effective_profile = GateProfile(
            name=profile.name,
            mode=profile.mode,
            thresholds=GateThresholds(
                file_max_lines=base_thresholds.file_max_lines,
                function_max_lines=base_thresholds.function_max_lines,
                nesting_max=base_thresholds.nesting_max,
                complexity_max=base_thresholds.complexity_max,
                complexity_warn=base_thresholds.complexity_warn,
                complexity_block=base_thresholds.complexity_block,
                file_complexity_warn_lines=base_thresholds.file_complexity_warn_lines,
                file_complexity_warn_sum=base_thresholds.file_complexity_warn_sum,
                file_complexity_block_lines=base_thresholds.file_complexity_block_lines,
                file_complexity_block_sum=base_thresholds.file_complexity_block_sum,
                max_violations=aggregate_max_violations,
                severity=aggregate_severity,
            ),
            description=profile.description,
        )
        return GateEvaluation(
            profile=effective_profile,
            total_status=total_status,
            checker_results=checker_results,
            scanned_files=len(files),
            total_violations=total_violations,
            blocking_violations=blocking_violations,
        )

    def evaluate(
        self,
        *,
        targets: list[Path] | None = None,
        profile_name: str = "advisory",
    ) -> GateEvaluation:
        profile = get_profile(profile_name)
        scoped_targets = [path.resolve() for path in (targets or [self.project_root])]
        files = discover_python_files(scoped_targets)

        policy = load_gate_policy(self.project_root)
        if policy is None:
            return self._evaluate_uniform(files, profile)

        return self._evaluate_with_policy(files, profile, policy)

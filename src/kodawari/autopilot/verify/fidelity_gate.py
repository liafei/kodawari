"""Post-implement fidelity gate.

Runs AFTER the executor finishes and verify+rules_gate pass, BEFORE the
workflow declares the task done. Catches the specific failure modes
observed in the external_trends_v1 run where a task was reported as
PASS but the implementation under-delivered against the planner's spec:

* ``check_task_name_token_coverage`` — when the task name lists named
  scope items (e.g. "Google, X, and Yahoo") and one of those tokens
  never appears in any non-test changed file, the executor likely
  narrowed the implementation. Flag as blocking drift.

* ``check_migration_files_exist`` — when the implementation registers
  a migration with ``filename="20260516_024_xxx.sql"`` but the SQL
  file is not actually on disk, the migration is fictitious. Flag.

* ``check_test_negative_assertion_drift`` — when changed test files
  contain ``assert "<subject>" not in ...`` and ``<subject>`` matches a
  named scope item from the task card (task_name / invariants), the
  test is enforcing the absence of something the planner said must be
  present. Flag (this caught T3 of external_trends_v1).

This module is intentionally heuristic. It produces "block"-severity
findings only on high-confidence signals; everything else is "warn".
Operators should be able to silence individual checks via task_card
fields (``fidelity_gate_skip``), but the default posture is on.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class FidelityFinding:
    kind: str  # "missing_task_name_token" / "missing_migration_file" / "test_drift_negative_assertion"
    severity: str  # "block" or "warn"
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass
class FidelityResult:
    findings: list[FidelityFinding] = field(default_factory=list)

    @property
    def blocking_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "block")

    @property
    def warn_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warn")

    @property
    def passed(self) -> bool:
        return self.blocking_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "blocking_count": self.blocking_count,
            "warn_count": self.warn_count,
            "findings": [f.to_dict() for f in self.findings],
        }

    def must_fix_messages(self) -> list[str]:
        return [f.message for f in self.findings if f.severity == "block"]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_fidelity_gate(
    *,
    project_root: Path,
    task_card: dict[str, Any],
    changed_files: list[str],
) -> FidelityResult:
    """Run all enabled fidelity checks and return aggregated findings.

    ``task_card`` may carry an optional ``fidelity_gate_skip`` list of
    check-name strings to silence specific checks (useful when a task
    legitimately violates one heuristic — e.g. an i18n task whose
    task_name tokens deliberately don't appear in code).
    """
    skip = {str(s).strip() for s in (task_card.get("fidelity_gate_skip") or []) if str(s).strip()}
    findings: list[FidelityFinding] = []
    # For checks that judge "did the executor deliver", use changed_files.
    # For checks that judge "is the final state consistent with the planner's
    # intent", use task_card.files_to_change so pre-existing drift in files
    # the executor didn't touch this round still surfaces.
    declared_files = [str(p) for p in (task_card.get("files_to_change") or []) if str(p).strip()]
    union_files = list(dict.fromkeys([*declared_files, *changed_files]))
    if "task_name_token_coverage" not in skip:
        findings.extend(check_task_name_token_coverage(task_card, union_files, project_root))
    if "migration_files_exist" not in skip:
        findings.extend(check_migration_files_exist(union_files, project_root))
    if "test_negative_assertion_drift" not in skip:
        findings.extend(check_test_negative_assertion_drift(task_card, union_files, project_root))
    return FidelityResult(findings=findings)


# ---------------------------------------------------------------------------
# Check 1: task_name token coverage
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "as", "from", "into", "is", "are", "be", "been", "being",
    "task", "implement", "add", "build", "extract", "define", "make", "create",
    "support", "fix", "update", "configure", "ensure", "use", "run", "set",
    # Common task-name leading verbs and connectives — these are titles
    # like "Bring X in line with Y" / "Collapse duplicate Z to a thin shim"
    # where the leading capitalized verb is not a scope token.
    "bring", "collapse", "persist", "extract", "finalize", "split", "merge",
    "rename", "expose", "wire", "thread", "lock", "unlock", "reset",
    "task_id", "page", "hot",
}

# Enumeration like "A, B, and C" or "A, B, C"
_ENUM_RE = re.compile(r"([A-Z][A-Za-z0-9]+(?:[ ,]+(?:and\s+)?[A-Z][A-Za-z0-9]*)+)")
# Single capitalized proper-noun-like token (>=2 chars to skip "I", "A")
_PROPER_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{1,30})\b")


def _extract_scope_tokens(task_name: str) -> list[str]:
    """Pull "named scope" tokens out of a task title.

    Heuristic: capitalized words and enumeration members. Stopwords and
    common verbs are dropped. Returns the unique ordered list.
    """
    name = str(task_name or "")
    tokens: list[str] = []
    # 1) Enumeration members: "Google, X, and Yahoo" → [Google, X, Yahoo]
    for match in _ENUM_RE.finditer(name):
        seg = match.group(1)
        for piece in re.split(r"[ ,]+(?:and\s+)?", seg):
            tok = piece.strip()
            if tok and tok.lower() not in _STOPWORDS and tok not in tokens:
                tokens.append(tok)
    # 2) Standalone capitalized words
    for m in _PROPER_RE.finditer(name):
        tok = m.group(1)
        if tok.lower() in _STOPWORDS or tok in tokens:
            continue
        tokens.append(tok)
    return tokens


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def check_task_name_token_coverage(
    task_card: dict[str, Any],
    changed_files: list[str],
    project_root: Path,
) -> list[FidelityFinding]:
    task_name = str(task_card.get("task_name") or task_card.get("title") or "")
    tokens = _extract_scope_tokens(task_name)
    if not tokens:
        return []
    # Restrict the search to NON-TEST files — finding a token only in a
    # test file is not proof the executor delivered it.
    non_test = [f for f in changed_files if "/tests/" not in f.replace("\\", "/") and not f.replace("\\", "/").startswith("tests/")]
    if not non_test:
        return []
    haystack = "\n".join(_read_text_safe(project_root / f) for f in non_test).lower()
    findings: list[FidelityFinding] = []
    for tok in tokens:
        # Substring search (case-insensitive). Word boundaries are too strict
        # for snake_case / kebab-case identifiers where ``google`` appears
        # inside ``fetch_google_trending``. Single-character tokens still need
        # to be neighbours of separators to avoid matching letters inside
        # words like ``Box`` matching ``X`` mid-word.
        tok_lower = tok.lower()
        if len(tok_lower) == 1:
            # Require punctuation/separator around 1-char tokens so "X"
            # doesn't match inside "extract".
            if re.search(rf"(?:^|[^A-Za-z0-9_]){re.escape(tok_lower)}(?:$|[^A-Za-z0-9_])", haystack):
                continue
        else:
            if tok_lower in haystack:
                continue
        findings.append(
            FidelityFinding(
                kind="missing_task_name_token",
                severity="block",
                message=(
                    f"Task name mentions {tok!r} but no non-test changed file contains that token. "
                    f"task_name={task_name!r}"
                ),
                evidence={"missing_token": tok, "task_name": task_name, "checked_files": non_test},
            )
        )
    return findings


# ---------------------------------------------------------------------------
# Check 2: migration files exist
# ---------------------------------------------------------------------------

_FILENAME_KEYWORD_RE = re.compile(
    r"""filename\s*=\s*['"]([^'"]+\.sql)['"]""",
    re.IGNORECASE,
)


def check_migration_files_exist(
    files_to_scan: list[str],
    project_root: Path,
) -> list[FidelityFinding]:
    """Scan migration-registry files for filename="<x>.sql" entries and
    assert each .sql actually exists on disk.

    Looks at every file in ``files_to_scan`` whose name contains "migration"
    (case-insensitive). Pass ``task_card.files_to_change`` here, not just
    the executor's last-round ``changed_files`` — pre-existing drift on a
    migrations file the executor didn't touch this round must still surface.
    """
    findings: list[FidelityFinding] = []
    for rel in files_to_scan:
        rel_norm = rel.replace("\\", "/")
        if "migration" not in rel_norm.lower():
            continue
        if rel_norm.endswith(".sql"):
            continue
        path = project_root / rel
        text = _read_text_safe(path)
        if not text:
            continue
        for match in _FILENAME_KEYWORD_RE.finditer(text):
            sql_name = match.group(1)
            # Candidate locations: same dir as the registry, sibling
            # ``migration_sql`` dir, and the project root.
            registry_dir = path.parent
            candidates = [
                registry_dir / sql_name,
                registry_dir / "migration_sql" / sql_name,
                registry_dir.parent / "migration_sql" / sql_name,
                project_root / "backend" / "db" / "migration_sql" / sql_name,
            ]
            if any(c.exists() for c in candidates):
                continue
            findings.append(
                FidelityFinding(
                    kind="missing_migration_file",
                    severity="block",
                    message=(
                        f"Registry {rel_norm!r} references SQL file {sql_name!r} "
                        f"but no such file exists in any expected migration directory."
                    ),
                    evidence={
                        "registry_file": rel_norm,
                        "missing_sql_file": sql_name,
                        "candidates_checked": [str(c) for c in candidates],
                    },
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Check 3: test negative-assertion drift
# ---------------------------------------------------------------------------

# Captures the literal string after ``not in`` / ``not contains`` / ``not present``
# Conservative: only the simplest forms.
_NEG_ASSERT_STRING_RE = re.compile(
    r"""assert\s+['"]([^'"]+)['"]\s+not\s+in\s+""",
    re.IGNORECASE,
)


def _scope_tokens_from_card(task_card: dict[str, Any]) -> list[str]:
    """Aggregate tokens the planner said MUST be present.

    Sources:
      - task_card.task_name
      - task_card.invariants (must-preserve statements)
    """
    tokens = list(_extract_scope_tokens(str(task_card.get("task_name") or task_card.get("title") or "")))
    for inv in (task_card.get("invariants") or []):
        text = str(inv)
        for m in _PROPER_RE.finditer(text):
            tok = m.group(1)
            if tok.lower() in _STOPWORDS or tok in tokens:
                continue
            tokens.append(tok)
        for m in _ENUM_RE.finditer(text):
            for piece in re.split(r"[ ,]+(?:and\s+)?", m.group(1)):
                tok = piece.strip()
                if tok and tok.lower() not in _STOPWORDS and tok not in tokens:
                    tokens.append(tok)
    return tokens


def check_test_negative_assertion_drift(
    task_card: dict[str, Any],
    changed_files: list[str],
    project_root: Path,
) -> list[FidelityFinding]:
    """Find ``assert "<subject>" not in ...`` lines whose subject overlaps
    a scope token the planner said must be present.

    Catches the T3-style drift where the executor satisfied a frontend
    task by writing tests that ASSERT the missing features are absent.
    """
    scope_tokens = [t.lower() for t in _scope_tokens_from_card(task_card)]
    if not scope_tokens:
        return []
    test_files = [
        f for f in changed_files
        if "/tests/" in f.replace("\\", "/") or f.replace("\\", "/").startswith("tests/")
    ]
    findings: list[FidelityFinding] = []
    seen: set[tuple[str, str]] = set()  # (test_file, subject) — dedup repeated matches
    for rel in test_files:
        text = _read_text_safe(project_root / rel)
        if not text:
            continue
        for match in _NEG_ASSERT_STRING_RE.finditer(text):
            subject = match.group(1)
            subject_lower = subject.lower()
            matched = [tok for tok in scope_tokens if tok and tok in subject_lower]
            if not matched:
                continue
            key = (rel, subject_lower)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                FidelityFinding(
                    kind="test_drift_negative_assertion",
                    severity="block",
                    message=(
                        f"Test {rel!r} asserts {subject!r} is absent, but the task card "
                        f"requires {matched!r} to be present. The executor likely down-scoped the "
                        f"implementation and rewrote the test to lock it in."
                    ),
                    evidence={
                        "test_file": rel,
                        "negative_assertion_subject": subject,
                        "matching_required_tokens": matched,
                    },
                )
            )
    return findings


__all__ = [
    "FidelityFinding",
    "FidelityResult",
    "run_fidelity_gate",
    "check_task_name_token_coverage",
    "check_migration_files_exist",
    "check_test_negative_assertion_drift",
]

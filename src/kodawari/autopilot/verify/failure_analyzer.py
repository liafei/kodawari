"""Minimum Tier-A verify-failure analyzer.

Classifies pytest failures into two tiers:

- **Tier A** — literal-assert stale-assertion candidates. Can only be
  auto-mutated when the failure exactly matches a structured
  ``allowed_test_mutations`` entry supplied by the task card. Absent such
  authorization, Tier A still returns ``authorized_mutation=False``.
- **Tier B** — everything else (AttributeError, KeyError, fixture setup
  errors, parametrized mismatches, custom assertion messages, mock
  call mismatches, snapshot diffs). Never auto-mutated; always requires
  human review or task-card update.

This is deliberately narrow. Tier B's "automatic mutation" is **not** in
scope for this iteration — the false-positive risk is too high. The
analyzer only surfaces whether a failure looks like a stale assertion
that matches an already-authorized mutation.

See ``docs/planning/4.23新优化迭代计划.md`` Phase 5 for the full roadmap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


# Tier A patterns: simple numeric or string equality on a scalar / len() call.
# We only match the narrow canonical forms below — anything else falls to B.
_LITERAL_ASSERT = re.compile(
    r"""
    ^\s*
    (?:E\s+)?          # pytest's "E  " prefix on failure lines is optional
    assert\s+
    (?P<lhs>.+?)
    \s*==\s*
    (?P<rhs>.+?)
    \s*$
    """,
    re.VERBOSE,
)

# Literal RHS we will accept: integer, float, single-quoted or double-quoted
# string. More exotic literals (tuples, lists, dicts) fall to Tier B.
_LITERAL_RHS = re.compile(
    r"""
    ^(?:
        -?\d+(?:\.\d+)?      # int or float
        | "(?:[^"\\]|\\.)*"  # double-quoted string
        | '(?:[^'\\]|\\.)*'  # single-quoted string
        | True | False | None
    )$
    """,
    re.VERBOSE,
)

# Pytest "FAILED tests/xxx.py::test_name" summary marker. File path + lineno
# we pull from the traceback frame preceding the "E  assert ..." line.
_PYTEST_FAIL_SUMMARY = re.compile(
    r"^FAILED\s+(?P<nodeid>[^\s]+)",
    re.MULTILINE,
)

# Traceback frame line: "tests/test_foo.py:42: in test_bar"
_TRACEBACK_FRAME = re.compile(
    r"^(?P<file>[^:\s]+\.py):(?P<lineno>\d+):",
    re.MULTILINE,
)


@dataclass(frozen=True)
class PytestFailure:
    """One parsed failing pytest test."""
    nodeid: str
    file: str
    lineno: Optional[int]
    assertion_line: str   # The raw "E  assert ..." line (post-rewrite, e.g. "assert 4 == 5")
    exception_type: str   # "AssertionError" | "AttributeError" | "KeyError" | ...
    source_line: str = ""  # The un-rewritten source line from the test (e.g. "assert len(channels) == 5")

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "file": self.file,
            "lineno": self.lineno,
            "assertion_line": self.assertion_line,
            "source_line": self.source_line,
            "exception_type": self.exception_type,
        }


@dataclass(frozen=True)
class FailureClassification:
    tier: str                          # "A" | "B"
    classification: str                # "stale_assertion_candidate" | "implementation_or_environment_failure" | ...
    authorized_mutation: bool
    reason: str = ""
    mutation: Optional[dict[str, Any]] = None
    lhs: str = ""
    rhs: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tier": self.tier,
            "classification": self.classification,
            "authorized_mutation": self.authorized_mutation,
            "reason": self.reason,
            "lhs": self.lhs,
            "rhs": self.rhs,
        }
        if self.mutation is not None:
            payload["mutation"] = dict(self.mutation)
        return payload


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_pytest_failures(pytest_output: str) -> list[PytestFailure]:
    """Extract failed tests from pytest stdout/stderr.

    Limitations (by design, Tier A scope):
    - Does not reconstruct fixtures or parametrize ids beyond what pytest
      emits in the nodeid.
    - Assertion-line capture uses the first ``E  `` line in the per-test
      error block and the earliest traceback frame with a ``.py:<lineno>``.
    - Does not handle xdist / unittest TestResult output shapes.
    """
    failures: list[PytestFailure] = []
    if not pytest_output:
        return failures

    # Step 1: find summary nodeids
    nodeids = [m.group("nodeid") for m in _PYTEST_FAIL_SUMMARY.finditer(pytest_output)]
    if not nodeids:
        return failures

    # Step 2: walk per-failure blocks. Pytest separates failure blocks with
    # "_________________ test_name _________________" rules. We locate each
    # block by nodeid name and scan it for (file:line, E-line, exception).
    for nodeid in nodeids:
        block = _extract_failure_block(pytest_output, nodeid)
        if not block:
            failures.append(PytestFailure(
                nodeid=nodeid,
                file=nodeid.split("::")[0] if "::" in nodeid else "",
                lineno=None,
                assertion_line="",
                exception_type="Unknown",
            ))
            continue
        file, lineno = _find_first_frame(block)
        assertion_line, exc_type, source_line = _find_error_signal(block)
        if not file and "::" in nodeid:
            file = nodeid.split("::")[0]
        failures.append(PytestFailure(
            nodeid=nodeid,
            file=file,
            lineno=lineno,
            assertion_line=assertion_line,
            exception_type=exc_type,
            source_line=source_line,
        ))
    return failures


def _extract_failure_block(output: str, nodeid: str) -> str:
    """Return the per-test failure block for ``nodeid`` or ``""``."""
    # Test id in pytest "=== FAILURES ===" section uses just the test-name
    # (post ::) with surrounding underscores. We scan for that first,
    # then fall back to the nodeid substring.
    test_name = nodeid.split("::")[-1]
    # Find rule line like "_______________ test_name _______________"
    rule_pattern = re.compile(
        rf"_{{3,}} {re.escape(test_name)} _{{3,}}",
    )
    match = rule_pattern.search(output)
    if not match:
        return ""
    start = match.end()
    # Next rule line terminates this block
    next_rule = re.compile(r"^_{3,}\s+\S", re.MULTILINE)
    nxt = next_rule.search(output, start)
    end = nxt.start() if nxt else len(output)
    return output[start:end]


def _find_first_frame(block: str) -> tuple[str, Optional[int]]:
    match = _TRACEBACK_FRAME.search(block)
    if not match:
        return "", None
    try:
        lineno = int(match.group("lineno"))
    except ValueError:
        lineno = None
    return match.group("file").replace("\\", "/"), lineno


_KNOWN_EXCEPTIONS = (
    "AttributeError", "KeyError", "TypeError", "ValueError",
    "ImportError", "NameError", "RuntimeError", "AssertionError",
    "FileNotFoundError", "IndexError", "ZeroDivisionError",
)


def _classify_exception(e_lines: list[str]) -> str:
    """Pick the most salient named exception from the E-prefixed lines."""
    for ln in e_lines:
        stripped = ln.lstrip("E").strip()
        for candidate in _KNOWN_EXCEPTIONS:
            if stripped.startswith(f"{candidate}:") or stripped == candidate:
                return candidate
    return "AssertionError"


def _find_source_line(lines: list[str], first_e_idx: int) -> str:
    """Return the indented source line preceding the first E-line, or ""."""
    for i in range(first_e_idx - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if _TRACEBACK_FRAME.match(stripped):
            continue
        if lines[i].startswith((" ", "\t")):
            return stripped
        return ""
    return ""


def _is_assertion_error_line(line: str) -> bool:
    """Return True when an E-prefixed line carries an ``assert ...`` expression."""
    stripped = line.strip()
    if stripped.startswith("E"):
        stripped = stripped[1:].lstrip()
    return stripped.startswith("assert ")


def _find_error_signal(block: str) -> tuple[str, str, str]:
    """Extract ``(assertion_line, exception_type, source_line)`` from a block."""
    lines = block.splitlines()
    e_indices = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("E")]
    if not e_indices:
        return "", "AssertionError", ""
    # Prefer the first assertion-shaped E-line. Pytest often appends extra
    # diagnostic lines like "E  + where ..."; selecting the last E-line would
    # lose the actual assert expression and misclassify Tier A as Tier B.
    assertion_line = ""
    for idx in e_indices:
        candidate = lines[idx].strip()
        if _is_assertion_error_line(candidate):
            assertion_line = candidate
            break
    if not assertion_line:
        assertion_line = lines[e_indices[0]].strip()
    source_line = _find_source_line(lines, e_indices[0])
    exc_type = _classify_exception([lines[i] for i in e_indices])
    return assertion_line, exc_type, source_line


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _strip_E_prefix(line: str) -> str:
    """Pytest prefixes failure lines with 'E   '; drop that for pattern match."""
    stripped = line.strip()
    if stripped.startswith("E"):
        stripped = stripped[1:].lstrip()
    return stripped


def _extract_literal_assert(assertion_line: str) -> Optional[tuple[str, str]]:
    """Return (lhs, rhs) if line is a literal-equality assert, else None."""
    if not assertion_line:
        return None
    body = _strip_E_prefix(assertion_line)
    match = _LITERAL_ASSERT.match(body)
    if not match:
        return None
    lhs = match.group("lhs").strip()
    rhs = match.group("rhs").strip()
    # RHS must be a pure literal. LHS can be a name, len(...), attribute.
    if not _LITERAL_RHS.match(rhs):
        return None
    # Reject multi-line / parenthesized exotic LHS — keep it narrow.
    if "\n" in lhs or "{" in lhs or "[" in lhs:
        return None
    return lhs, rhs


def _entry_str(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _pattern_in_any_body(pattern: str, bodies: tuple[str, ...]) -> bool:
    for body in bodies:
        if body and pattern in body:
            return True
    return False


def _mutation_matches_failure(
    entry: dict[str, Any],
    failure_file: str,
    source_body: str,
    assertion_body: str,
) -> bool:
    """Return True if one allowed-mutation entry matches the failure."""
    if _entry_str(entry, "match_kind") != "literal_assert":
        return False
    if _entry_str(entry, "file").replace("\\", "/") != failure_file:
        return False
    old_pattern = _entry_str(entry, "old_pattern")
    if not old_pattern:
        return False
    return _pattern_in_any_body(old_pattern, (source_body, assertion_body))


def _find_authorized_mutation(
    failure: PytestFailure,
    allowed_mutations: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Return the matching allowed_test_mutation entry, or None.

    Pytest assertion rewriting means the line the user wrote in the test file
    (``assert len(channels) == 5``) is NOT the same string that appears in the
    failure output (``assert 4 == 5``); the task card's ``old_pattern`` is the
    source form, so we check both source_line and assertion_line.
    """
    if not allowed_mutations or not failure.file:
        return None
    failure_file = failure.file.replace("\\", "/")
    assertion_body = _strip_E_prefix(failure.assertion_line) if failure.assertion_line else ""
    source_body = failure.source_line.strip() if failure.source_line else ""
    for entry in allowed_mutations:
        if not isinstance(entry, dict):
            continue
        if _mutation_matches_failure(entry, failure_file, source_body, assertion_body):
            return dict(entry)
    return None


def classify_failure(
    failure: PytestFailure,
    allowed_mutations: Optional[list[dict[str, Any]]] = None,
) -> FailureClassification:
    """Classify one pytest failure into Tier A / Tier B.

    Tier A requires BOTH:
      - Exception is AssertionError
      - Assertion line matches ``assert <expr> == <literal>``

    ``authorized_mutation`` is True only if a Tier A failure also matches a
    structured entry in ``allowed_mutations``. Absent authorization, the
    classification stays Tier A but with ``authorized_mutation=False`` and
    the fix-round must not auto-mutate — human review / task-card update is
    required.
    """
    allowed_mutations = allowed_mutations or []

    if failure.exception_type != "AssertionError":
        return FailureClassification(
            tier="B",
            classification="implementation_or_environment_failure",
            authorized_mutation=False,
            reason=(
                f"{failure.exception_type} — not a literal assertion failure; "
                "executor must fix implementation or environment, tests must not be auto-mutated"
            ),
        )

    literal = _extract_literal_assert(failure.assertion_line)
    if literal is None:
        return FailureClassification(
            tier="B",
            classification="non_literal_assertion",
            authorized_mutation=False,
            reason=(
                "AssertionError but the failing line is not a recognized literal-equality form "
                "(lhs == <literal>); test must not be auto-mutated"
            ),
        )

    lhs, rhs = literal
    mutation = _find_authorized_mutation(failure, allowed_mutations)
    if mutation is not None:
        return FailureClassification(
            tier="A",
            classification="stale_assertion_candidate",
            authorized_mutation=True,
            reason="literal-assert failure matched an allowed_test_mutations entry",
            mutation=mutation,
            lhs=lhs,
            rhs=rhs,
        )

    return FailureClassification(
        tier="A",
        classification="stale_assertion_candidate",
        authorized_mutation=False,
        reason=(
            "literal-assert failure looks like a stale assertion but the task card "
            "did not authorize this mutation; human review or task-card update required"
        ),
        lhs=lhs,
        rhs=rhs,
    )


__all__ = [
    "FailureClassification",
    "PytestFailure",
    "classify_failure",
    "parse_pytest_failures",
]

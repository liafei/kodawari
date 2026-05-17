"""
Schema enum exhaustiveness tests.

Each test greps src/ Python files for string literals emitted for a specific
enum-constrained field and asserts that every emitted value is covered by the
JSON schema enum (exhaustiveness check: emitted_values ⊆ schema_enum).

To manually verify: add `gate_recommendation="FAKE_VALUE"` to any .py file
under src/ and confirm this test goes red, then revert.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCHEMA_DIR = SRC / "kodawari" / "schemas"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_schema(relative_path: str) -> dict:
    path = SCHEMA_DIR / relative_path
    return json.loads(path.read_text(encoding="utf-8"))


def _grep_src(src: Path, *patterns: str) -> set[str]:
    """
    Scan all .py files under src, applying each compiled pattern.
    Returns the union of all group(1) captures.
    """
    found: set[str] = set()
    compiled = [re.compile(p) for p in patterns]
    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in compiled:
            for m in rx.finditer(text):
                found.add(m.group(1))
    return found


def _grep_files(files: list[Path], *patterns: str) -> set[str]:
    """Same as _grep_src but over an explicit list of files."""
    found: set[str] = set()
    compiled = [re.compile(p) for p in patterns]
    for py_file in files:
        if not py_file.exists():
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for rx in compiled:
            for m in rx.finditer(text):
                found.add(m.group(1))
    return found


def _collect_review_mode_emitted(src: Path) -> set[str]:
    """
    Collect review_mode string values from src/ Python files.

    Handles the ternary assignment pattern used in delivery_review.py and
    status_runtime.py:
        "review_mode": "real_peer_review" if ... else "simulated"
        review_mode = "real_peer_review" if ... else "simulated"
    """
    found: set[str] = set()
    # Pattern 1: explicit dict entry — captures the immediately-assigned value
    p1 = re.compile(r'"review_mode"\s*:\s*"([^"]+)"')
    # Pattern 2: direct variable assignment
    p2 = re.compile(r'\breview_mode\s*=\s*"([^"]+)"')
    # Pattern 3: `else "VALUE"` on lines that already assign review_mode
    p3 = re.compile(r'\belse\s+"([^"]+)"')

    for py_file in src.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            has_review_mode = False
            for rx in (p1, p2):
                for m in rx.finditer(line):
                    found.add(m.group(1))
                    has_review_mode = True
            # Pick up the `else "simulated"` part of ternary assignments
            if has_review_mode:
                for m in p3.finditer(line):
                    found.add(m.group(1))
    return found


# ---------------------------------------------------------------------------
# Test: gate_recommendation enum in peer_review_response.schema.json
# ---------------------------------------------------------------------------


def test_gate_recommendation_enum_exhaustive() -> None:
    """All gate_recommendation literals emitted in src/ must be in the schema enum."""
    schema = _load_schema("runtime/peer_review_response.schema.json")
    schema_enum: set[str] = set(
        schema["properties"]["gate_recommendation"]["enum"]
    )

    emitted = _grep_src(
        SRC,
        # dict-literal form: "gate_recommendation": "VALUE"
        r'"gate_recommendation"\s*:\s*"([A-Z_]+)"',
        # keyword-arg / assignment form: gate_recommendation="VALUE" or gate_recommendation = "VALUE"
        r'\bgate_recommendation\s*=\s*"([A-Z_]+)"',
    )

    missing = emitted - schema_enum
    assert not missing, (
        f"gate_recommendation values emitted in src/ but absent from schema enum:\n"
        f"  missing : {sorted(missing)}\n"
        f"  schema  : {sorted(schema_enum)}\n"
        f"  emitted : {sorted(emitted)}"
    )


# ---------------------------------------------------------------------------
# Test: severity enum in peer_review_response.schema.json
# ---------------------------------------------------------------------------


def test_severity_enum_exhaustive() -> None:
    """
    Severity literals emitted in peer_review_response-producing files must be
    in the schema enum.

    Only the files listed below are scanned to avoid false positives from
    planning_orchestrator.py / plan_reviewer.py which use 'blocking' for a
    different (planning-findings) schema.
    """
    schema = _load_schema("runtime/peer_review_response.schema.json")
    schema_enum: set[str] = set(schema["properties"]["severity"]["enum"])

    PEER_REVIEW_FILES = [
        SRC / "kodawari" / "autopilot" / "collaboration.py",
        SRC / "kodawari" / "autopilot" / "local_adapter.py",
        SRC / "kodawari" / "autopilot" / "fake_adapter.py",
        SRC / "kodawari" / "autopilot" / "engine_review_mixin.py",
        SRC / "kodawari" / "autopilot" / "review_precheck.py",
        SRC / "kodawari" / "autopilot" / "opus_gateway.py",
        SRC / "kodawari" / "autopilot" / "collaboration_flow.py",
    ]

    emitted = _grep_files(
        PEER_REVIEW_FILES,
        r'"severity"\s*:\s*"(\w+)"',
        r'\bseverity\s*=\s*"(\w+)"',
    )

    missing = emitted - schema_enum
    assert not missing, (
        f"severity values emitted in peer_review_response context but absent from schema enum:\n"
        f"  missing : {sorted(missing)}\n"
        f"  schema  : {sorted(schema_enum)}\n"
        f"  emitted : {sorted(emitted)}"
    )


# ---------------------------------------------------------------------------
# Test: review_mode enum in review_evidence.schema.json
# ---------------------------------------------------------------------------


def test_review_mode_enum_exhaustive() -> None:
    """
    All review_mode string literals assigned in src/ must be in the
    review_evidence schema enum.

    Captures both sides of the ternary pattern:
        "review_mode": "real_peer_review" if ... else "simulated"
    """
    schema = _load_schema("observability/review_evidence.schema.json")
    schema_enum: set[str] = set(schema["properties"]["review_mode"]["enum"])

    emitted = _collect_review_mode_emitted(SRC)

    missing = emitted - schema_enum
    assert not missing, (
        f"review_mode values emitted in src/ but absent from schema enum:\n"
        f"  missing : {sorted(missing)}\n"
        f"  schema  : {sorted(schema_enum)}\n"
        f"  emitted : {sorted(emitted)}"
    )


# ---------------------------------------------------------------------------
# Sanity: schema files are valid JSON and have the expected structure
# ---------------------------------------------------------------------------


def test_peer_review_response_schema_structure() -> None:
    """Schema file is valid and gate_recommendation has an enum constraint."""
    schema = _load_schema("runtime/peer_review_response.schema.json")
    props = schema["properties"]
    assert "enum" in props["gate_recommendation"], (
        "gate_recommendation must have an enum constraint"
    )
    assert "enum" in props["severity"], (
        "severity must have an enum constraint"
    )
    assert set(props["gate_recommendation"]["enum"]) >= {
        "PROCEED_TO_GATE", "REVIEW_FIX_REQUIRED", "ESCALATE_TO_HUMAN"
    }, "gate_recommendation enum must include the three canonical values"


def test_review_evidence_schema_structure() -> None:
    """review_evidence schema has a review_mode property with enum constraint."""
    schema = _load_schema("observability/review_evidence.schema.json")
    assert "review_mode" in schema["properties"], (
        "review_evidence schema must define review_mode property"
    )
    assert "enum" in schema["properties"]["review_mode"], (
        "review_mode must have an enum constraint"
    )
    assert set(schema["properties"]["review_mode"]["enum"]) == {
        "real_peer_review", "simulated"
    }, "review_mode enum must be exactly {real_peer_review, simulated}"

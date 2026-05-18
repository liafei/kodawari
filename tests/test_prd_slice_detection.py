"""Tests for PRD multi-slice detection (E1: epic replan)."""

from __future__ import annotations

from kodawari.autopilot.planning.prd_contract import (
    build_prd_intake,
    extract_prd_slices,
)


def test_no_slices_when_zero_markers() -> None:
    """Single-slice PRDs (the historical default) return empty slices list."""
    text = "# PRD: thing\n\n## 目标\n\nDo a thing.\n"
    assert extract_prd_slices(text) == []


def test_no_slices_when_single_marker() -> None:
    """A single Slice marker is not enough to trigger multi-slice mode —
    you need at least two for the multi-slice loop to make sense."""
    text = "# PRD\n\n## Slice 1: only one\n\nbody\n"
    assert extract_prd_slices(text) == []


def test_english_slice_markers_in_order() -> None:
    text = "\n".join([
        "# PRD: multi-feature",
        "",
        "## 目标",
        "Background context.",
        "",
        "## Slice 1: Schema and repository",
        "Schema + repo layer details.",
        "Multiple lines.",
        "",
        "## Slice 2: API endpoints",
        "API endpoint specifications.",
        "",
        "## Slice 3: Tests and docs",
        "Final phase.",
    ])
    slices = extract_prd_slices(text)
    assert len(slices) == 3
    assert [s["declared_index"] for s in slices] == [1, 2, 3]
    assert [s["title"] for s in slices] == [
        "Schema and repository",
        "API endpoints",
        "Tests and docs",
    ]
    assert "Schema + repo layer details" in slices[0]["content"]
    assert "API endpoint specifications" in slices[1]["content"]


def test_chinese_slice_markers() -> None:
    """Mirror of English support — `## 切片 N:` and friends."""
    text = "\n".join([
        "# PRD",
        "",
        "## 切片 1：schema 层",
        "schema 内容。",
        "",
        "## 切片 2：service 层",
        "service 内容。",
    ])
    slices = extract_prd_slices(text)
    assert len(slices) == 2
    assert slices[0]["title"] == "schema 层"
    assert slices[1]["title"] == "service 层"


def test_phase_and_part_synonyms() -> None:
    """## Phase N: / ## Part N: must work like ## Slice N: — these are
    equally common ways to express the same intent in PRDs."""
    text = "\n".join([
        "# PRD",
        "## Phase 1: design",
        "design body",
        "## Part 2: implement",
        "implement body",
    ])
    slices = extract_prd_slices(text)
    assert len(slices) == 2
    assert slices[0]["title"] == "design"
    assert slices[1]["title"] == "implement"


def test_position_preserved_when_indices_out_of_order() -> None:
    """User declares Slice 2 before Slice 1 (mistake) — position field
    reflects appearance order so downstream consumers can decide whether
    to honor declared_index or fall back to position."""
    text = "\n".join([
        "# PRD",
        "## Slice 2: out of order",
        "second body",
        "## Slice 1: first by declared index",
        "first body",
    ])
    slices = extract_prd_slices(text)
    assert len(slices) == 2
    assert slices[0]["position"] == 0
    assert slices[0]["declared_index"] == 2  # as the PRD wrote it
    assert slices[1]["position"] == 1
    assert slices[1]["declared_index"] == 1


def test_does_not_match_unrelated_h2_headings() -> None:
    """## Slice options / ## 切片说明 should NOT be picked up — the regex
    requires a numeric index + colon, which descriptive headings don't have."""
    text = "\n".join([
        "# PRD",
        "## Slice options",
        "general intro",
        "## 切片说明",
        "more intro",
        "## 目标",
        "goal section",
    ])
    assert extract_prd_slices(text) == []


def test_build_prd_intake_carries_slices_field() -> None:
    text = "\n".join([
        "# PRD: thing",
        "## 目标",
        "outcome",
        "## 数据契约",
        "source of truth: db.thing",
        "## 分层",
        "schema service",
        "## Slice 1: phase one",
        "p1 body",
        "## Slice 2: phase two",
        "p2 body",
    ])
    intake = build_prd_intake(text, feature="multi-slice")
    assert "slices" in intake
    assert len(intake["slices"]) == 2
    assert intake["slices"][0]["title"] == "phase one"


def test_build_prd_intake_empty_slices_for_single_slice_prd() -> None:
    text = "\n".join([
        "# PRD: single",
        "## 目标",
        "outcome",
        "## 数据契约",
        "source of truth: db.x",
        "## 分层",
        "schema service",
    ])
    intake = build_prd_intake(text, feature="single")
    assert intake["slices"] == [], "Single-slice PRDs must NOT trigger multi-slice mode"

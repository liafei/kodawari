"""Reviewer prompt must force verification of implementer claims.

Why: in a real Sonnet+Codex+Sonnet wf-test run, the implementer claimed
"All 15 tests pass" while verify_summary in the bundle was empty. The Codex
reviewer (with local_repo_read capability) approved without opening any file
because the old prompt said "Prefer bundle evidence before making filesystem
reads." Verify then ran and produced 15 ERRORs.

These tests pin two contract changes:
- The local_repo_read capability hint must mandate verification when the
  bundle does not contain proof of a load-bearing claim.
- The Rules block must explicitly tell the reviewer not to take the
  implementer's "tests pass" claim at face value when verify_summary is
  empty/absent.
"""

from __future__ import annotations

from kodawari.autopilot.review.opus_gateway import (
    _capability_hint,
    build_review_prompt,
)


def test_local_repo_read_hint_mandates_verification_when_bundle_lacks_proof() -> None:
    hint = _capability_hint("local_repo_read", workspace_root="/repo/myproject")
    lower = hint.lower()
    # Old wording is gone — it was the foothold for "trust the implementer".
    assert "prefer bundle evidence before making filesystem reads" not in lower
    # New wording mandates active verification.
    assert "must" in lower and "open" in lower
    # The "verify_summary is empty" example is in the hint so the model
    # recognises the most common false-positive shape.
    assert "verify_summary" in lower
    # We still keep "trust the code" so on conflict the filesystem wins.
    assert "trust the code" in lower


def test_rules_section_forbids_taking_implementer_claim_at_face_value() -> None:
    prompt = build_review_prompt(
        task="T1: ship migration",
        context={},
        changed_files=["backend/db/migration_sql/001.sql"],
        review_iteration=0,
        review_bundle={"workspace_root": "/repo/x"},
        reviewer_capability="local_repo_read",
    )
    # A single sentence in the Rules block carries the load-bearing wording.
    assert "implementer_note" in prompt
    assert "verify_summary" in prompt
    assert "must NOT take" in prompt or "must not take" in prompt.lower()
    # The implementer note is explicitly marked non-authoritative for this
    # decision — it shouldn't get accidental weight on a tie.
    assert "non-authoritative" in prompt


def test_bundle_only_capability_unchanged_no_filesystem() -> None:
    """The bundle_only path is unchanged: no filesystem access, blocker on
    insufficient evidence. Must still hold so HTTP-gateway / MCP reviewers
    keep behaving the same."""
    hint = _capability_hint("bundle_only", workspace_root="")
    assert "no filesystem access" in hint
    assert "blocker" in hint.lower()


def test_review_prompt_carries_product_copy_protection_rule() -> None:
    """Regression for a real-world wf-test failure: in the external_trends_v1
    autopilot run, the executor flipped ``_BADGE_TEXT = "外部趋势榜"`` to
    ``"External trends"`` and the reviewer approved with 0 findings — the
    PRD honesty-boundary clause requires the CJK badge label. The Rules
    block now mandates blocking_items when a diff changes the *value* of a
    user-facing string literal without explicit task-plan scope authorization."""
    prompt = build_review_prompt(
        task="T1: harden service payload",
        context={},
        changed_files=["backend/api/v1/services/external_trends_service.py"],
        review_iteration=0,
        review_bundle={"workspace_root": "/repo/x"},
        reviewer_capability="local_repo_read",
    )
    assert "Product copy protection" in prompt
    # Load-bearing concepts the reviewer must recognise.
    assert "user-facing copy" in prompt
    assert "badge_text" in prompt
    assert "honesty-boundary" in prompt or "honesty boundary" in prompt
    # The verbatim CJK example anchors the rule against the specific
    # observed violation so future wording changes don't accidentally
    # drop the CJK-string protection.
    assert "外部趋势榜" in prompt
    # Must be a blocking concern, not advisory.
    assert "blocking_items" in prompt


def test_review_prompt_product_copy_rule_excludes_false_positive_shapes() -> None:
    """The product-copy rule must NOT statistically over-flag legitimate
    diffs that don't ship user-visible text changes. Pin the explicit
    out-of-scope language so future wording tightening cannot regress
    into 'any CJK literal in diff → block' behavior.

    Sub-agent review (a494b265) flagged these false-positive shapes:
    pure identifier renames, refactor moves preserving the literal,
    internal logger/debug messages, and in-scope test fixtures."""
    prompt = build_review_prompt(
        task="T1: rename constant",
        context={},
        changed_files=["backend/api/v1/services/external_trends_service.py"],
        review_iteration=0,
        review_bundle={"workspace_root": "/repo/x"},
        reviewer_capability="local_repo_read",
    )
    # The rule explicitly carves out the four most common false-positive shapes.
    assert "identifier renames" in prompt
    assert "refactor moves" in prompt
    assert "logger" in prompt and "debug" in prompt
    assert "test-fixture" in prompt or "test fixture" in prompt
    # The rule fires on VALUE flip, not on identifier touching — pin the
    # word "value" so the reviewer doesn't trigger on `getattr(obj, "badge_text")`
    # or on renaming the constant while keeping its value.
    assert "value" in prompt.lower()

"""Single-source guard for code-quality redline thresholds.

Every threshold value (7, 10, 20, 30, 50, 1000, 1500) must come from
``code_redline.REDLINE`` in authoritative gate logic and docs.

Allowed exceptions are listed in ``_ALLOWED_HARDCODE_PATHS`` below
with a comment explaining why.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from code_redline import REDLINE
from code_redline.verify import find_hardcoded_copies


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCAN_ROOT = _REPO_ROOT / "src" / "kodawari" / "gate"
_AUTHORITATIVE_TEXT_PATHS = [
    _REPO_ROOT / "AGENTS.md",
    _REPO_ROOT / "CLAUDE.md",
    _REPO_ROOT / "docs" / "CAPABILITY_MAP.md",
    _REPO_ROOT / "docs" / "operations" / "二、运行操作、门禁规则与后续路线.md",
]
_LEGACY_REDLINE_PHRASES = [
    ".claude/redline.toml",
    "strict profile keeps legacy 1000-line limit",
    "legacy 1000-line compatibility profile",
    "单文件最多 `1000` 行",
    "单函数最多 `50` 行",
    "圈复杂度最多 `6`",
]


# Files inside gate/ allowed to contain literal redline numbers, with rationale.
_ALLOWED_HARDCODE_PATHS: dict[str, str] = {
    # code_health keeps historical 500/1000/50 snapshot metrics for
    # ratchet dashboards; these are snapshot-granularity counters,
    # not active redline thresholds.
    "code_health.py": "legacy snapshot metrics (files_over_500, functions_over_50)",
    # checker_metrics contains compatibility-facing examples and legacy
    # remediation text that mention non-redline counters.
    "checker_metrics.py": "non-redline compatibility text and snapshot references",
    # duplication checker tunable similarity threshold (lines = 10)
    # is distinct from the code_redline complexity redline.
    "checker_duplication.py": "duplication similarity threshold (distinct metric)",
    # compliance checker's token-length heuristic (>= 4) is distinct
    # from the code_redline nesting_max value.
    "checker_compliance.py": "SoT token-length heuristic (distinct metric)",
}


def test_redline_values_match_canonical() -> None:
    """Sanity: catch accidental mutation of code_redline itself."""
    assert REDLINE.nesting_max == 4
    assert REDLINE.complexity_warn == 7
    assert REDLINE.complexity_block == 10
    assert REDLINE.file_complexity_warn_lines == 1000
    assert REDLINE.file_complexity_warn_sum == 20
    assert REDLINE.file_complexity_block_lines == 1500
    assert REDLINE.file_complexity_block_sum == 30
    assert REDLINE.max_violations == 50


def test_no_hardcoded_redline_numbers_in_gate() -> None:
    """grep gate/ for any hardcoded redline literal not under the allow-list."""
    hits = find_hardcoded_copies(project_root=_SCAN_ROOT)
    unexpected = [
        (p, ln, text)
        for (p, ln, text) in hits
        if str(p).replace("\\", "/") not in _ALLOWED_HARDCODE_PATHS
    ]
    assert not unexpected, (
        "Hardcoded redline thresholds found in gate/ — import from code_redline instead:\n"
        + "\n".join(f"  gate/{p}:{ln}  {text}" for p, ln, text in unexpected)
        + "\n\nIf the value is genuinely unrelated to code_redline "
        "(e.g. a duplication-similarity threshold that happens to be 10), "
        "add the file to _ALLOWED_HARDCODE_PATHS with a rationale."
    )


def test_allow_list_paths_actually_exist() -> None:
    """Keep _ALLOWED_HARDCODE_PATHS pruned — dead entries should be removed."""
    missing = [
        path for path in _ALLOWED_HARDCODE_PATHS
        if not (_SCAN_ROOT / path).exists()
    ]
    assert not missing, f"Allow-list references nonexistent files: {missing}"


@pytest.mark.parametrize(
    "field,expected",
    [
        ("nesting_max", 4),
        ("complexity_warn", 7),
        ("complexity_block", 10),
        ("file_complexity_warn_lines", 1000),
        ("file_complexity_warn_sum", 20),
        ("file_complexity_block_lines", 1500),
        ("file_complexity_block_sum", 30),
        ("max_violations", 50),
    ],
)
def test_gate_profiles_thresholds_match_redline(field: str, expected: int) -> None:
    """Every gate profile must expose the canonical code-redline fields."""
    from kodawari.gate.profiles import ADVISORY_THRESHOLDS, DEFAULT_THRESHOLDS, STRICT_THRESHOLDS

    for profile_name, thr in [
        ("advisory", ADVISORY_THRESHOLDS),
        ("blocking", DEFAULT_THRESHOLDS),
        ("strict", STRICT_THRESHOLDS),
    ]:
        actual = getattr(thr, field, None)
        assert actual == expected, (
            f"{profile_name} profile field {field} = {actual!r}, "
            f"expected {expected!r} from code_redline.REDLINE"
        )


@pytest.mark.parametrize("path", _AUTHORITATIVE_TEXT_PATHS)
def test_authoritative_redline_docs_reference_code_redline(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "code-redline" in text or "code_redline" in text, (
        f"{path.relative_to(_REPO_ROOT)} must point readers to the shared code-redline standard"
    )


@pytest.mark.parametrize("path", _AUTHORITATIVE_TEXT_PATHS)
def test_authoritative_redline_docs_do_not_publish_legacy_rules(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    unexpected = [phrase for phrase in _LEGACY_REDLINE_PHRASES if phrase in text]
    assert not unexpected, (
        f"{path.relative_to(_REPO_ROOT)} still contains legacy redline wording: {unexpected}"
    )

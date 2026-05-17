"""Tests for kodawari.utils.glob_match — globstar (**) semantics."""

from __future__ import annotations

import pytest

from kodawari.utils.glob_match import glob_match


class TestNoGlobstar:
    """Patterns without ** are delegated to fnmatch."""

    def test_simple_extension_match(self) -> None:
        assert glob_match("foo.py", "*.py") is True

    def test_simple_extension_no_match(self) -> None:
        assert glob_match("foo.txt", "*.py") is False

    def test_single_dir_match(self) -> None:
        assert glob_match("a/b.py", "a/*.py") is True

    def test_single_star_does_not_cross_dirs(self) -> None:
        # * must not cross directory boundaries
        assert glob_match("a/b/c.py", "a/*.py") is False

    def test_exact_path_match(self) -> None:
        assert glob_match(".claude/workflow/p.yaml", ".claude/workflow/*.yaml") is True

    def test_different_prefix_no_match(self) -> None:
        assert glob_match("workflow/policies/p.yaml", ".claude/workflow/*.yaml") is False


class TestGlobstarLeading:
    """**/pattern — match at any depth including zero."""

    def test_zero_dirs(self) -> None:
        # **/*.py should match foo.py (zero leading directories)
        assert glob_match("foo.py", "**/*.py") is True

    def test_one_dir(self) -> None:
        assert glob_match("a/foo.py", "**/*.py") is True

    def test_deep_nesting(self) -> None:
        assert glob_match("a/b/c/foo.py", "**/*.py") is True

    def test_wrong_extension_no_match(self) -> None:
        assert glob_match("foo.txt", "**/*.py") is False

    def test_deep_wrong_extension(self) -> None:
        assert glob_match("a/b/c/foo.txt", "**/*.py") is False


class TestGlobstarMiddle:
    """src/**/file — globstar in the middle of a pattern."""

    def test_zero_middle_dirs(self) -> None:
        # src/**/bar.py should match src/bar.py
        assert glob_match("src/bar.py", "src/**/bar.py") is True

    def test_one_middle_dir(self) -> None:
        assert glob_match("src/foo/bar.py", "src/**/bar.py") is True

    def test_deep_middle_dirs(self) -> None:
        assert glob_match("src/a/b/c/bar.py", "src/**/bar.py") is True

    def test_wrong_prefix_no_match(self) -> None:
        assert glob_match("lib/foo/bar.py", "src/**/bar.py") is False

    def test_wrong_filename_no_match(self) -> None:
        assert glob_match("src/foo/baz.py", "src/**/bar.py") is False


class TestWindowsPaths:
    """Backslash paths are normalized to forward slashes."""

    def test_windows_path_globstar(self) -> None:
        assert glob_match("a\\b\\c.py", "**/*.py") is True

    def test_windows_path_simple(self) -> None:
        assert glob_match("a\\b.py", "a/*.py") is True

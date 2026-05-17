"""glob_match — globstar (**) pattern matching with .gitignore/shell semantics."""

from __future__ import annotations

import re


def _classify_pattern_token(pattern: str, i: int) -> tuple[str, int]:
    """Return (regex_fragment, next_index) for the glob token at position i."""
    if pattern[i : i + 2] == "**":
        if i + 2 < len(pattern) and pattern[i + 2] == "/":
            return "(.*/)?", i + 3   # **/ — zero or more dir components
        return ".*", i + 2           # ** at end — match anything
    if pattern[i] == "*":
        return "[^/]*", i + 1        # * within one path component only
    if pattern[i] == "?":
        return "[^/]", i + 1         # ? within one path component only
    if pattern[i] in r"\.^$+{}[]|()":
        return re.escape(pattern[i]), i + 1
    return pattern[i], i + 1


def _pattern_to_regex(pattern: str) -> str:
    """Convert a glob pattern to an anchored regex. ** crosses dir boundaries."""
    result: list[str] = []
    i = 0
    while i < len(pattern):
        fragment, i = _classify_pattern_token(pattern, i)
        result.append(fragment)
    return "^" + "".join(result) + "$"


def glob_match(path: str, pattern: str) -> bool:
    """Return True if *path* matches *pattern* with proper globstar semantics.

    * matches within a single path component; ** matches zero or more components.
    Both forward and backward slashes in *path* are accepted.
    """
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    # Use regex (not fnmatch) so * never crosses / on any platform.
    return bool(re.match(_pattern_to_regex(pattern), path))

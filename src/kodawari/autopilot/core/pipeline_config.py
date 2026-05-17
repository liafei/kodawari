"""Pipeline-as-Code configuration loader for autopilot Phase 3A.

Reads .claude/workflow/workflow_pipeline.yaml and resolves which pipeline
preset applies to a given set of changed files.

YAML format (schema_version: "pipeline.v1"):

  pipelines:
    docs_only:
      match: "all_files_match('docs/**', '*.md', 'README*')"
      preset: skip_review
      max_cycles: 2
    strict:
      match: "any_file_matches('**/auth_*.py', '**/credential_*.py')"
      preset: strict_review
      max_cycles: 10
    default:
      match: "true"
      preset: default

Match expression grammar:
  - "true"                          — always matches
  - "all_files_match('a', 'b')"    — every file matches at least one glob
  - "any_file_matches('a', 'b')"   — at least one file matches at least one glob

First matching pipeline wins (order preserved from YAML).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kodawari.utils.glob_match import glob_match

logger = logging.getLogger(__name__)

_PIPELINE_YAML_REL = ".claude/workflow/workflow_pipeline.yaml"

_ALL_FILES_RE = re.compile(r"all_files_match\((.+)\)$")
_ANY_FILE_RE = re.compile(r"any_file_matches\((.+)\)$")
_QUOTED_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


def _parse_glob_args(raw: str) -> list[str]:
    """Extract quoted glob strings from a match expression argument list."""
    return [m.group(1) or m.group(2) for m in _QUOTED_RE.finditer(raw)]


def _eval_match(expr: str, changed_files: list[str]) -> bool:
    """Evaluate a match expression against a list of changed file paths.

    Returns True if the expression matches the given file list.
    """
    expr = expr.strip()

    if expr == "true":
        return True

    m = _ALL_FILES_RE.match(expr)
    if m:
        globs = _parse_glob_args(m.group(1))
        if not globs:
            return False
        if not changed_files:
            # No files changed — all_files_match requires at least one file
            return False
        return all(
            any(glob_match(f, g) for g in globs)
            for f in changed_files
        )

    m = _ANY_FILE_RE.match(expr)
    if m:
        globs = _parse_glob_args(m.group(1))
        if not globs:
            return False
        return any(
            any(glob_match(f, g) for g in globs)
            for f in changed_files
        )

    return False


@dataclass
class PipelineEntry:
    """A single named pipeline entry from workflow_pipeline.yaml."""

    name: str
    match_expr: str
    preset: str
    max_cycles: int | None = None


@dataclass
class PipelineConfig:
    """Parsed contents of workflow_pipeline.yaml."""

    schema_version: str
    pipelines: list[PipelineEntry] = field(default_factory=list)


def load_pipeline_config(project_root: Path) -> PipelineConfig | None:
    """Load and parse workflow_pipeline.yaml from *project_root*.

    Returns None if:
    - the file does not exist
    - the yaml package is not installed
    - the file is malformed
    - the schema_version is missing or unrecognised
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        return None

    config_path = Path(project_root) / _PIPELINE_YAML_REL
    if not config_path.exists():
        return None

    try:
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("pipeline config parse failed: %s", config_path, exc_info=True)
        return None

    if not isinstance(raw, dict):
        return None

    schema_version = str(raw.get("schema_version") or "")
    if not schema_version:
        return None

    raw_pipelines = raw.get("pipelines")
    if not isinstance(raw_pipelines, dict):
        return None

    entries: list[PipelineEntry] = []
    for name, data in raw_pipelines.items():
        if not isinstance(data, dict):
            continue
        match_expr = str(data.get("match") or "").strip()
        preset = str(data.get("preset") or "").strip()
        max_cycles_raw = data.get("max_cycles")
        max_cycles: int | None = None
        if max_cycles_raw is not None:
            try:
                max_cycles = int(max_cycles_raw)
            except (TypeError, ValueError):
                max_cycles = None
        entries.append(
            PipelineEntry(
                name=str(name),
                match_expr=match_expr,
                preset=preset,
                max_cycles=max_cycles,
            )
        )

    return PipelineConfig(schema_version=schema_version, pipelines=entries)


def resolve_pipeline(project_root: Path, changed_files: list[str]) -> PipelineEntry | None:
    """Return the first matching PipelineEntry for *changed_files*.

    Returns None when no config file exists or no entry matches.
    """
    config = load_pipeline_config(project_root)
    if config is None:
        return None

    for entry in config.pipelines:
        if _eval_match(entry.match_expr, changed_files):
            return entry

    return None

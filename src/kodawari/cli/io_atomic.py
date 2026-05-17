"""Shared atomic I/O helpers for kodawari CLI artifacts.

.. deprecated::
    Import from ``kodawari.infra.io_atomic`` instead.
    This module re-exports everything for backward compatibility.
"""

from kodawari.infra.io_atomic import (  # noqa: F401
    CorruptArtifactError,
    acquire_file_lock,
    append_jsonl_atomic,
    atomic_write_json,
    atomic_write_text,
    load_json_dict,
    load_jsonl_rows,
    path_lock,
    quarantine_corrupt_artifact,
    quarantine_corrupt_jsonl_lines,
    release_file_lock,
)

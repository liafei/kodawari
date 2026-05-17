"""Compatibility facade for delivery workflow helpers."""

from __future__ import annotations

from kodawari.cli.evidence.changed_files_truth import resolve_task_delta_changed_files
from kodawari.cli.delivery.delivery_common import _git_diff_files, resolve_planning_dir
from kodawari.cli import delivery_release as _delivery_release
from kodawari.cli import delivery_review as _delivery_review
from kodawari.cli import delivery_verify as _delivery_verify


def build_review_report(*args, **kwargs):
    _delivery_review.resolve_task_delta_changed_files = resolve_task_delta_changed_files
    _delivery_review._git_diff_files = _git_diff_files
    return _delivery_review.build_review_report(*args, **kwargs)


def build_verify_report(*args, **kwargs):
    _delivery_verify._git_diff_files = _git_diff_files
    return _delivery_verify.build_verify_report(*args, **kwargs)


build_qa_report = _delivery_release.build_qa_report
build_ship_readiness_report = _delivery_release.build_ship_readiness_report

__all__ = [
    "_git_diff_files",
    "build_qa_report",
    "build_review_report",
    "build_ship_readiness_report",
    "build_verify_report",
    "resolve_planning_dir",
    "resolve_task_delta_changed_files",
]


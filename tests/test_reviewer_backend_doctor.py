from __future__ import annotations

from pathlib import Path

import pytest

from kodawari.autopilot.review.backend_doctor import (
    ReviewerHealthCache,
    probe_reviewer_backend,
)
from kodawari.autopilot.review.gateways.codex import CodexReviewerConfig


def test_probe_reviewer_backend_rejects_invalid_codex_binary(tmp_path: Path) -> None:
    report = probe_reviewer_backend(
        backend="codex",
        workspace_root=tmp_path,
        codex_config=CodexReviewerConfig(executable="claude"),
        codex_available_fn=lambda cfg: True,
    )

    assert report.available is False
    assert report.probe_status == "executable_invalid"
    assert "not a codex binary" in report.detail


def test_probe_reviewer_backend_reports_missing_codex_executable(tmp_path: Path) -> None:
    report = probe_reviewer_backend(
        backend="codex",
        workspace_root=tmp_path,
        codex_config=CodexReviewerConfig(executable="codex"),
        codex_available_fn=lambda cfg: False,
    )

    assert report.available is False
    assert report.probe_status == "executable_missing"
    assert "executable not found" in report.detail


def test_probe_reviewer_backend_blocks_when_codex_home_unwritable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kodawari.autopilot.review.backend_doctor._ensure_dir_writable",
        lambda path: (False, "access denied"),
    )
    report = probe_reviewer_backend(
        backend="codex",
        workspace_root=tmp_path,
        codex_config=CodexReviewerConfig(executable="codex"),
        codex_available_fn=lambda cfg: True,
    )

    assert report.available is False
    assert report.probe_status == "home_unwritable"
    assert "home is not writable" in report.detail


def test_probe_reviewer_backend_requires_api_credentials(tmp_path: Path) -> None:
    report = probe_reviewer_backend(
        backend="api",
        workspace_root=tmp_path,
        reviewer_base_url="https://example.test",
        reviewer_api_key="",
    )

    assert report.available is False
    assert report.probe_status == "config_missing"
    assert "WORKFLOW_REVIEWER_API_KEY" in report.detail


def test_probe_reviewer_backend_uses_session_cache(tmp_path: Path) -> None:
    calls: dict[str, int] = {"count": 0}

    def _available(_cfg: CodexReviewerConfig | None) -> bool:
        calls["count"] += 1
        return True

    cache = ReviewerHealthCache(ttl_seconds=600)
    config = CodexReviewerConfig(executable="codex")
    first = probe_reviewer_backend(
        backend="codex",
        workspace_root=tmp_path,
        codex_config=config,
        codex_available_fn=_available,
        cache=cache,
    )
    second = probe_reviewer_backend(
        backend="codex",
        workspace_root=tmp_path,
        codex_config=config,
        codex_available_fn=_available,
        cache=cache,
    )

    assert first.available is True
    assert second.available is True
    assert calls["count"] == 1

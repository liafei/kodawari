"""Tests for the shared subprocess and isolated-home helpers.

These primitives consolidate three previously-duplicated patterns that each
had at least one site missing a fix:
  - Windows ``.cmd`` / ``.bat`` shim wrapping with ``cmd.exe /c``
  - ``CREATE_NO_WINDOW`` flag on subprocess to prevent UAC/firewall popups
  - mtime-aware credential file sync (replaces ``if target.exists(): return``)
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from kodawari.autopilot.core.isolated_home import (
    sync_file_mtime_aware,
    sync_first_present_source,
)
from kodawari.autopilot.core.subprocess_compat import (
    subprocess_text_kwargs,
    windows_creation_flags,
    windows_safe_command,
)


# --- subprocess_compat ---------------------------------------------------


def test_windows_safe_command_passes_through_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kodawari.autopilot.core.subprocess_compat.os.name", "posix")
    cmd = windows_safe_command("/usr/bin/codex.cmd", "exec", "--quiet")
    # POSIX always passes through — no cmd.exe wrapping even for .cmd suffix.
    assert cmd == ["/usr/bin/codex.cmd", "exec", "--quiet"]


def test_windows_safe_command_wraps_cmd_shim_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kodawari.autopilot.core.subprocess_compat.os.name", "nt")
    cmd = windows_safe_command(r"C:\npm\codex.cmd", "exec", "--quiet")
    assert cmd[:2] == ["cmd.exe", "/c"]
    assert cmd[2:] == [r"C:\npm\codex.cmd", "exec", "--quiet"]


def test_windows_safe_command_wraps_bat_too_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kodawari.autopilot.core.subprocess_compat.os.name", "nt")
    cmd = windows_safe_command(r"C:\tools\runner.bat", "x")
    assert cmd[0] == "cmd.exe"


def test_windows_safe_command_no_wrap_for_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kodawari.autopilot.core.subprocess_compat.os.name", "nt")
    cmd = windows_safe_command(r"C:\bin\codex.exe", "x")
    assert cmd == [r"C:\bin\codex.exe", "x"]


def test_subprocess_text_kwargs_defaults_are_complete() -> None:
    kwargs = subprocess_text_kwargs()
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    if os.name == "nt":
        assert kwargs.get("creationflags") == 0x08000000
    else:
        assert "creationflags" not in kwargs


def test_subprocess_text_kwargs_caller_overrides_win() -> None:
    kwargs = subprocess_text_kwargs(timeout=42, cwd="/tmp")
    assert kwargs["timeout"] == 42
    assert kwargs["cwd"] == "/tmp"
    assert kwargs["text"] is True  # default preserved


def test_windows_creation_flags_zero_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    # The constant is captured at import time, so we just check the actual
    # platform behaves: zero on POSIX, CREATE_NO_WINDOW on Windows.
    flags = windows_creation_flags()
    if os.name == "nt":
        assert flags == 0x08000000
    else:
        assert flags == 0


# --- isolated_home -------------------------------------------------------


def test_sync_file_mtime_aware_copies_when_target_missing(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("v1", encoding="utf-8")
    dst = tmp_path / "iso" / "dst.txt"
    assert sync_file_mtime_aware(src, dst, label="t") is True
    assert dst.read_text(encoding="utf-8") == "v1"


def test_sync_file_mtime_aware_refreshes_when_source_newer(tmp_path: Path) -> None:
    """Regression: the prior `if target.exists(): return` would silently hold a stale token."""
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("v1", encoding="utf-8")
    sync_file_mtime_aware(src, dst)
    assert dst.read_text(encoding="utf-8") == "v1"

    time.sleep(0.05)  # ensure mtime moves forward on coarse-resolution filesystems
    src.write_text("v2-rotated", encoding="utf-8")
    # bump mtime explicitly so the test does not depend on filesystem mtime resolution
    new_mtime = dst.stat().st_mtime + 5
    os.utime(src, (new_mtime, new_mtime))

    assert sync_file_mtime_aware(src, dst, label="t") is True
    assert dst.read_text(encoding="utf-8") == "v2-rotated"


def test_sync_file_mtime_aware_skips_when_target_fresh(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.txt"
    src.write_text("v1", encoding="utf-8")
    sync_file_mtime_aware(src, dst)

    # Explicitly age the source so target is strictly newer.
    older = dst.stat().st_mtime - 5
    os.utime(src, (older, older))

    assert sync_file_mtime_aware(src, dst) is False
    # content unchanged
    assert dst.read_text(encoding="utf-8") == "v1"


def test_sync_file_mtime_aware_returns_false_when_source_missing(tmp_path: Path) -> None:
    src = tmp_path / "missing.txt"
    dst = tmp_path / "dst.txt"
    assert sync_file_mtime_aware(src, dst) is False
    assert not dst.exists()


def test_sync_first_present_source_picks_first_existing(tmp_path: Path) -> None:
    src1 = tmp_path / "first" / "auth.json"
    src2 = tmp_path / "second" / "auth.json"
    src2.parent.mkdir(parents=True)
    src2.write_text('{"k": "v"}', encoding="utf-8")
    dst = tmp_path / "iso" / "auth.json"
    # src1 missing -> fall through to src2
    result = sync_first_present_source(
        target=dst,
        source_candidates=[src1, src2],
        label="auth",
    )
    assert result is True
    assert dst.read_text(encoding="utf-8") == '{"k": "v"}'


def test_sync_first_present_source_returns_false_when_all_missing(tmp_path: Path) -> None:
    dst = tmp_path / "iso" / "auth.json"
    result = sync_first_present_source(
        target=dst,
        source_candidates=[tmp_path / "a", tmp_path / "b"],
    )
    assert result is False
    assert not dst.exists()

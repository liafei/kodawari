"""Cross-platform subprocess helpers shared by executors and reviewers.

Two recurring problems on Windows that historically required ad-hoc fixes
duplicated across `execution_codex_cli.py`, `codex_reviewer.py`, and
`cli_reviewer.py`:

1. ``.cmd``/``.bat`` shims (e.g. npm-installed ``codex.cmd``, ``claude.cmd``)
   cannot be invoked directly by ``subprocess.run([executable, ...])`` on
   Windows — the call fails with ``FileNotFoundError [WinError 2]``. The
   workaround is to wrap them with ``cmd.exe /c <executable> ...``.
2. When invoked from a Windows GUI process (VSCode extension, IDE), launching
   any console-spawning subprocess flashes a ``cmd``/firewall popup window
   unless ``creationflags=CREATE_NO_WINDOW`` is set.

Concentrating both fixes here means a fourth call site cannot silently miss
either treatment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Windows CREATE_NO_WINDOW flag value; 0 on POSIX so it composes safely.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def windows_safe_command(executable: str, *args: str) -> list[str]:
    """Return a subprocess ``argv`` that works for ``.cmd``/``.bat`` shims.

    On Windows, ``.cmd``/``.bat`` files must be executed via ``cmd.exe /c``.
    On POSIX, this is a pass-through.
    """
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        return ["cmd.exe", "/c", executable, *args]
    return [executable, *args]


def subprocess_text_kwargs(**overrides: Any) -> dict[str, Any]:
    """Standard ``subprocess.run`` kwargs for text-mode capture without GUI flicker.

    Defaults: ``capture_output=True``, ``text=True``, ``encoding='utf-8'``,
    ``errors='replace'``. On Windows, also adds ``creationflags=CREATE_NO_WINDOW``
    so the subprocess does not flash a console window or trigger a UAC/firewall
    popup when the parent is a GUI process.

    Caller-supplied kwargs override defaults.
    """
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if _CREATE_NO_WINDOW:
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    kwargs.update(overrides)
    return kwargs


def windows_creation_flags() -> int:
    """Return ``CREATE_NO_WINDOW`` on Windows and 0 elsewhere.

    Useful when a caller already builds its own kwargs dict and only needs the
    creationflags value.
    """
    return _CREATE_NO_WINDOW


__all__ = [
    "subprocess_text_kwargs",
    "windows_creation_flags",
    "windows_safe_command",
]

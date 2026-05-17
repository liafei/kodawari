"""Subprocess isolation helpers for the test suite.

Use these utilities when a test would otherwise pollute ``sys.modules``,
process-level singletons, or other global state that cannot be safely
rolled back inside the same process.

Design invariant
----------------
Never modify ``sys.modules`` inside the test process.  Instead, run the
offending code in a fresh Python subprocess via :func:`run_python_isolated`
so that the calling process's import state is completely untouched.

Consumers
---------
- ``tests/conftest.py`` — imports :func:`run_python_isolated` and
  :func:`assert_no_leaked_module` to back the ``run_isolated_python``
  fixture and ``clean_env_state`` documentation.
- ``tests/_helpers/__init__.py`` — re-exports the public API so that test
  modules can do ``from tests._helpers import run_python_isolated``.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass


@dataclass(frozen=True)
class SubprocessResult:
    """Immutable result from an isolated subprocess execution."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        """True when the subprocess exited with returncode 0."""
        return self.returncode == 0


def run_python_isolated(
    code: str,
    *,
    timeout: int = 60,
    extra_env: dict[str, str] | None = None,
) -> SubprocessResult:
    """Execute *code* in a fresh Python subprocess.

    The child process inherits the caller's environment plus any
    *extra_env* overrides.  ``sys.modules`` of the calling process is
    never touched.

    Args:
        code: Python source code to execute (automatically dedented).
        timeout: Seconds before the child process is forcibly killed.
        extra_env: Additional environment variables for the child process.

    Returns:
        :class:`SubprocessResult` with ``returncode``, ``stdout``, and
        ``stderr`` from the child process.
    """
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
    )
    return SubprocessResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def assert_no_leaked_module(
    probe_code: str,
    forbidden_prefix: str,
    *,
    timeout: int = 60,
) -> None:
    """Assert that executing *probe_code* does not import any module whose
    name equals or starts with *forbidden_prefix*.

    Runs entirely inside a subprocess so the calling process's
    ``sys.modules`` remains pristine.

    Args:
        probe_code: Python code whose import side-effects are under test.
        forbidden_prefix: Dotted module prefix to forbid, e.g.
            ``"kodawari.cli"``.
        timeout: Seconds before the subprocess is forcibly killed.

    Raises:
        AssertionError: When at least one forbidden module appears in
            ``sys.modules`` after *probe_code* executes.

    Example::

        assert_no_leaked_module(
            "import kodawari.autopilot",
            "kodawari.cli",
        )
    """
    sentinel = "LEAKED:"
    check_code = textwrap.dedent(f"""
        import sys
        {probe_code}
        leaked = sorted(
            m for m in sys.modules
            if m == {forbidden_prefix!r} or m.startswith({forbidden_prefix!r} + ".")
        )
        if leaked:
            print("{sentinel}" + ",".join(leaked))
            sys.exit(1)
        sys.exit(0)
    """)
    result = run_python_isolated(check_code, timeout=timeout)
    if not result.ok:
        leaked_line = next(
            (
                line
                for line in result.stdout.splitlines()
                if line.startswith(sentinel)
            ),
            "",
        )
        leaked_mods = (
            leaked_line.removeprefix(sentinel).split(",") if leaked_line else []
        )
        raise AssertionError(
            f"Module namespace leak: forbidden prefix {forbidden_prefix!r} appeared "
            "in sys.modules after running probe code.\n"
            "  Leaked modules:\n    " + "\n    ".join(leaked_mods)
            + (f"\n\nstderr:\n{result.stderr}" if result.stderr else "")
        )

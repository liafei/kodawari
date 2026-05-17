"""Import-boundary guard: ``kodawari.autopilot.*`` must not transitively
pull ``kodawari.cli.*`` at module-load time.

Rationale: the autopilot engine is meant to be consumable as a library
(embedded, tested, or wrapped) without dragging CLI argparse/output
infrastructure. Any edge from autopilot → cli is a regression of the
P0 trust-boundary refactor.

The test runs in a clean subprocess so it does not pollute sys.modules of
the pytest worker (which would destabilise other tests that rely on
already-imported cli modules).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_autopilot_import_does_not_pull_cli() -> None:
    probe = textwrap.dedent(
        """
        import importlib
        import pkgutil
        import sys

        importlib.import_module("kodawari.autopilot")
        import kodawari.autopilot as pkg
        for info in pkgutil.walk_packages(pkg.__path__, prefix="kodawari.autopilot."):
            importlib.import_module(info.name)

        leaked = sorted(
            name for name in sys.modules
            if name == "kodawari.cli" or name.startswith("kodawari.cli.")
        )
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        sys.exit(0)
        """
    ).strip()

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        leaked_line = next(
            (line for line in result.stdout.splitlines() if line.startswith("LEAKED:")),
            "",
        )
        leaked = leaked_line.removeprefix("LEAKED:").split(",") if leaked_line else []
        raise AssertionError(
            "autopilot → cli import edge detected. The following cli modules "
            "got pulled in as a side effect of importing autopilot:\n  "
            + "\n  ".join(leaked)
            + (f"\n\nstderr:\n{result.stderr}" if result.stderr else "")
        )

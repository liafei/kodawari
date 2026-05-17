"""CLI logging bootstrap helpers."""

from __future__ import annotations

import logging


def configure_cli_logging(verbose: int = 0) -> None:
    level = logging.DEBUG if int(verbose or 0) > 0 else logging.INFO
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(levelname)s %(name)s: %(message)s",
        )
        return
    root.setLevel(level)
    for handler in root.handlers:
        handler.setLevel(level)


__all__ = ["configure_cli_logging"]

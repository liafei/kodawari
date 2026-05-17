"""Public kodawari package surface."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from typing import Any


_STABLE_SUBPACKAGES = ("gate", "patterns", "safety", "spec_generator")

try:
    __version__ = version("kodawari")
except PackageNotFoundError:  # pragma: no cover - local source tree without installation
    __version__ = "0.0.0+local"

__all__ = ["__version__", *_STABLE_SUBPACKAGES]


def __getattr__(name: str) -> Any:
    if name in _STABLE_SUBPACKAGES:
        module = import_module(f"kodawari.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module 'kodawari' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

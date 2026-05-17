"""Shim: real implementation lives at kodawari.autopilot.core.collaboration."""

import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.core.collaboration")

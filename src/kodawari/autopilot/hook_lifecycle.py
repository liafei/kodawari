"""Shim: real implementation lives at kodawari.autopilot.engine.hook_lifecycle."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.engine.hook_lifecycle")

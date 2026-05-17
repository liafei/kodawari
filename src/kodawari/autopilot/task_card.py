"""Shim: real implementation lives at kodawari.autopilot.planning.task_card."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.planning.task_card")

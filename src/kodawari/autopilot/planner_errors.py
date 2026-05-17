"""Shim: real implementation lives at kodawari.autopilot.planning.planner_errors."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.planning.planner_errors")

"""Shim: real implementation lives at kodawari.autopilot.review_runtime_policy."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.review_runtime_policy")

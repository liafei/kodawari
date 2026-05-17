"""Shim: real implementation lives at kodawari.autopilot.core._env_helpers."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.core._env_helpers")

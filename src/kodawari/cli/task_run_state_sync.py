"""Shim: real implementation lives at kodawari.cli.runtime.task_run_state_sync."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.cli.runtime.task_run_state_sync")

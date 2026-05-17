"""Shim: real implementation lives at kodawari.cli.gate.gate_cmd."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.cli.gate.gate_cmd")

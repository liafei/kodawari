"""Shim: real implementation lives at kodawari.cli.evidence.verify_report."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.cli.evidence.verify_report")

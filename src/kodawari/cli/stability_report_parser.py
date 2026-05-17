"""Shim: real implementation lives at kodawari.cli.status.stability_report_parser."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.cli.status.stability_report_parser")

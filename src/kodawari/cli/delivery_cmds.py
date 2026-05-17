"""Shim: real implementation lives at kodawari.cli.delivery.delivery_cmds."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.cli.delivery.delivery_cmds")

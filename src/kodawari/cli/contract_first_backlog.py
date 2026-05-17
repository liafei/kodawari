"""Shim: real implementation lives at kodawari.cli.contract.contract_first_backlog."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.cli.contract.contract_first_backlog")

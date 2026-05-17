"""Shim: real implementation lives at kodawari.cli.evidence.self_repair_cmd."""

import importlib as _importlib
import sys as _sys

_sys.modules[__name__] = _importlib.import_module("kodawari.cli.evidence.self_repair_cmd")

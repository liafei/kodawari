"""Shim: real implementation lives at kodawari.autopilot.review.peer_review_gateway."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.review.peer_review_gateway")

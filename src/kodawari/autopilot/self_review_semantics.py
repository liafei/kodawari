"""Shim: real implementation lives at kodawari.autopilot.review.self_review_semantics."""
import sys as _sys, importlib as _importlib
_sys.modules[__name__] = _importlib.import_module("kodawari.autopilot.review.self_review_semantics")

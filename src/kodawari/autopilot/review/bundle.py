"""Backward-compat shim — see ``review_bundle`` for the real implementation.

Deletion target: 2026-08-04. Update your imports to
``kodawari.autopilot.review.review_bundle``.
"""
from kodawari.autopilot.review.review_bundle import *  # noqa: F401,F403

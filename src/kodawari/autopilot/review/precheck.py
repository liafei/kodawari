"""Backward-compat shim — see ``review_precheck`` for the real implementation.

Deletion target: 2026-08-04. Update your imports to
``kodawari.autopilot.review.review_precheck``.
"""
from kodawari.autopilot.review.review_precheck import *  # noqa: F401,F403

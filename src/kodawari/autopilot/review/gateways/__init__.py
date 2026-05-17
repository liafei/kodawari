"""Review gateway backends — canonical import path for new code.

Provides vendor-neutral access to all reviewer implementations.
"""

from __future__ import annotations

from kodawari.autopilot.review.gateways.cli import (  # noqa: F401
    REAL_REVIEW_MODES,
)
from kodawari.autopilot.review.gateways.peer_review import (  # noqa: F401
    PeerReviewGatewayConfig,
    request_peer_review,
)

__all__ = [
    "PeerReviewGatewayConfig",
    "request_peer_review",
    "REAL_REVIEW_MODES",
]

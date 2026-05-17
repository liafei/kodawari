"""Peer-review gateway facade — canonical import path for new code.

Provides vendor-neutral symbol aliases for OpusGatewayConfig and
request_opus_review. The underlying opus_gateway.py is preserved intact;
this module exists so new code can reference "peer review" concepts
without encoding model vendor names in import paths.
"""

from __future__ import annotations

from kodawari.autopilot.review.opus_gateway import (
    OpusGatewayConfig as PeerReviewGatewayConfig,
    build_review_prompt,
    parse_review_content,
    request_opus_review as request_peer_review,
)

__all__ = [
    "PeerReviewGatewayConfig",
    "request_peer_review",
    "build_review_prompt",
    "parse_review_content",
]


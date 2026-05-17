"""Review subsystem public API.

New code should import from this package instead of from individual flat
modules under autopilot/. The flat modules remain compatibility modules for
this release; this package is the stable public entry point.

Example::

    from kodawari.autopilot.review import derive_runtime_review_evidence
    from kodawari.autopilot.review.gateways import request_peer_review
"""

from __future__ import annotations

from kodawari.autopilot.review.bridge import (  # noqa: F401
    normalize_self_review_payload,
    run_codex_self_review,
    run_post_execution_qa,
    summarize_peer_review,
    summarize_self_review,
    validate_dual_review_evidence,
)
from kodawari.autopilot.review.bundle import (  # noqa: F401
    REVIEW_BUNDLE_FILENAME,
    REVIEW_BUNDLE_SCHEMA_VERSION,
    ReviewBundleError,
    build_review_bundle,
    validate_peer_review_response,
    write_review_bundle,
)
from kodawari.autopilot.review.contract import (  # noqa: F401
    MISSING_PEER_REVIEW_ISSUE,
    MISSING_SELF_REVIEW_ISSUE,
    derive_runtime_review_evidence,
    resolve_review_evidence_requirements,
)
from kodawari.autopilot.review.precheck import (  # noqa: F401
    apply_deterministic_review_guard,
    compute_deterministic_findings,
    is_test_file,
    resolve_verified_test_evidence,
)
from kodawari.infra.review_evidence_artifact import (  # noqa: F401
    build_review_evidence_artifact,
    load_review_evidence_artifact,
    write_review_evidence_artifact,
    REVIEW_EVIDENCE_FILENAME,
    REVIEW_EVIDENCE_SCHEMA_VERSION,
)

__all__ = [
    "derive_runtime_review_evidence",
    "resolve_review_evidence_requirements",
    "MISSING_PEER_REVIEW_ISSUE",
    "MISSING_SELF_REVIEW_ISSUE",
    "REVIEW_BUNDLE_FILENAME",
    "REVIEW_BUNDLE_SCHEMA_VERSION",
    "ReviewBundleError",
    "build_review_bundle",
    "validate_peer_review_response",
    "write_review_bundle",
    "compute_deterministic_findings",
    "apply_deterministic_review_guard",
    "is_test_file",
    "resolve_verified_test_evidence",
    "normalize_self_review_payload",
    "run_codex_self_review",
    "run_post_execution_qa",
    "summarize_peer_review",
    "summarize_self_review",
    "validate_dual_review_evidence",
    "build_review_evidence_artifact",
    "load_review_evidence_artifact",
    "write_review_evidence_artifact",
    "REVIEW_EVIDENCE_FILENAME",
    "REVIEW_EVIDENCE_SCHEMA_VERSION",
]

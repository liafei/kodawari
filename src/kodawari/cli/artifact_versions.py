"""Schema-version helpers and migrations for machine artifacts.

.. deprecated::
    Import from ``kodawari.infra.artifact_versions`` instead.
    This module re-exports everything for backward compatibility.
"""

from kodawari.infra.artifact_versions import (  # noqa: F401
    AUTOPILOT_STATE_COMPAT_VERSIONS,
    AUTOPILOT_STATE_SCHEMA_VERSION,
    ArtifactSchemaVersionError,
    KNOWN_ARTIFACTS,
    MigrationResult,
    expected_schema_versions,
    infer_artifact_spec,
    load_versioned_artifact,
    migrate_payload_for_path,
    validate_schema_version,
)

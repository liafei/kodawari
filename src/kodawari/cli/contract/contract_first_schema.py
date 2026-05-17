"""Runtime schema validation helpers for contract-first artifacts.

.. deprecated::
    Import from ``kodawari.infra.contract_first_schema`` instead.
    This module re-exports everything for backward compatibility.
"""

from kodawari.infra.contract_first_schema import (  # noqa: F401
    ContractFirstSchemaValidationError,
    infer_contract_first_schema_name,
    load_contract_first_artifact,
    validate_contract_first_payload,
    write_contract_first_artifact,
)

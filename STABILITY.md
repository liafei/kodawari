# kodawari Stability Policy

`kodawari` is still pre-1.0, but this repository now treats the public
surface as an explicit contract instead of an accident of import paths.

## Stable Python API

The top-level package exports only the names listed in
`kodawari.__all__`:

- `__version__`
- `gate`
- `patterns`
- `safety`
- `spec_generator`

Symbols exported by those subpackages through their own `__all__` are the
supported Python API. Other modules are internal unless a later release adds
them to this list.

## Internal Modules

Internal implementation modules may move without a compatibility window. New
internal-only code should live under `workflow_sdk._internal` when practical.
Public packages must not import from `_internal` across package boundaries;
that rule is enforced by `tests/test_import_rule.py`.

## CLI Compatibility Tiers

- User commands are compatibility targets. Breaking flags or output fields
  require a deprecation window.
- Operator commands are best-effort stable. They may gain fields and
  remediation details without a compatibility window.
- Debug/internal commands are not stable contracts.

The default CLI help should prioritize user commands; deeper operational and
debug surfaces may be exposed through explicit advanced help or documentation.

## Artifact Schemas

JSON artifacts must carry `schema_version`. Version strings use
`<artifact>.v<MAJOR>` or `<artifact>.v<MAJOR>.<MINOR>`.

Adding optional fields is compatible. Removing fields, renaming fields, or
changing field semantics requires a new major schema version. Readers must
accept the previous major version for at least 90 days after a replacement is
introduced.

## Deprecation Window

Stable Python API names, user CLI command contracts, and artifact schema major
versions get at least 90 days of deprecation before removal. Deprecation notes
must name the replacement and the removal date.

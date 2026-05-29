# Kodawari Working Agreement

Single source of truth: filesystem docs and tests. If instructions conflict,
follow this order:

1. Filesystem docs and tests
2. `CLAUDE.md`
3. `AGENTS.md`
4. Chat messages

Before code planning, review, or edits, read this file and the relevant source,
tests, and docs first. Do not answer from memory alone.

Before saying a command was checked, verified, or tested, run the command and
report the exact command plus meaningful output.

Keep changes small and contract-first. Public CLI commands, stable Python API,
and artifact schema major versions are compatibility surfaces; breaking changes
need an explicit deprecation path.

Code quality gates use the shared `code_redline.REDLINE` / `code-redline`
standard. Line count alone is not a split trigger; the active model is the
three-tier redline implemented by `code_redline`.


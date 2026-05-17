# Contributing to kodawari

Thanks for taking the time to look at this. kodawari is an opinionated tool
with strict guarantees; the contribution process matches that posture.

## Ground rules

1. **No silent-pass paths.** Every production code path under
   `KODAWARI_REVIEW_ENABLED=1` must fail closed when a check cannot run.
   The no-fake-run policy is the project's design center, not a nice-to-have.
2. **One feature per PR.** Don't bundle refactors with bug fixes.
3. **Test what you ship.** Removing one line of an implementation must cause
   at least one test to fail. If a behavior has no failing test for the
   removal case, it isn't covered.
4. **Read the contract.** The artifact chain (PRD_INTAKE → ARCHITECTURE_PLAN
   → TASK_GRAPH → TASK_CARD) is schema-validated. Don't write past the
   schema; if a field is missing, propose a schema bump in the same PR.

## Setup

```bash
git clone <repo-url>
cd kodawari
python -m venv .venv
.venv/bin/activate            # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest tests/ -q              # ~3 seconds, ~200 tests
```

## Workflow for non-trivial changes

For anything touching the planning state machine, executor protocol,
reviewer wiring, or no-fake-run gates:

1. Open an issue describing the problem + proposed approach.
2. Wait for an acknowledgement that the direction is acceptable.
3. Write a focused diff (one concern at a time).
4. Include a regression test that fails without the change.
5. Update relevant docs in the same PR
   (`docs/CAPABILITY_MAP.md`, `docs/OPERATOR_RUNBOOK.md` for error codes).

Smaller fixes (typos, doc clarifications, obvious bugs with clear root cause)
can go straight to PR without an issue.

## Coding standards

- **Python 3.11+.** Type hints required for public APIs; encouraged elsewhere.
- **Imports.** Sort with isort defaults; use `from __future__ import annotations`.
- **Line length.** 100 chars.
- **Tests.** pytest. Fixtures in `tests/conftest.py` when shared; otherwise local.

## Red-line gate

The `kodawari gate` profile enforces:

- File length × complexity (1500 lines + complexity-sum > 30 → BLOCK,
  1000 + 20 → WARN).
- Cyclomatic complexity > 10 → BLOCK, 7–10 → WARN.
- Nesting depth > 4 → BLOCK.
- Max violations per checker: 50.

Run `kodawari gate --project-root . --gate-profile blocking` on your branch
before opening the PR; it's what CI runs.

## Release notes

Add an entry to `CHANGELOG.md` for any user-visible change.

## License

By contributing you agree your contributions are licensed under the MIT
License (see [LICENSE](LICENSE)).

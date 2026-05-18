# Writing a PRD for kodawari

> 中文版本：[WRITING_PRD.zh-CN.md](WRITING_PRD.zh-CN.md)

A PRD (Product Requirements Document) is the **single source of truth**
kodawari reads to plan, implement, and verify your feature. Get the PRD
right and the autopilot has a fighting chance. Get it wrong and you'll
spend cycles fighting "low confidence" warnings, wrong layer guesses, or
the planner inventing routes you didn't ask for.

This guide is a practical recipe — not an essay. Follow the structure
below and the intake heuristic will produce a `high` confidence
PRD_INTAKE.json on the first try.

## The 5 sections that matter

```markdown
# PRD: <one-line feature name>

## 目标 / Goals

<2–4 sentences. What problem this solves, for whom.>

## 范围 / Scope

<Concrete deliverables. Use bullets. If it's a REST API, list each
endpoint with method + path + request body + response shape. If it's a
CLI, list each subcommand with args + behavior.>

## 数据契约 / Data Contract

source of truth: <db.table_name | filesystem | upstream_service>

schema:
- <field 1>
- <field 2>

不变量 / Invariants:
- <thing that must always be true>
- <thing that must never happen>

## 分层 / Layers

- schema 层：`path/to/schema.py`
- repository 层：`path/to/repository.py`
- service 层：`path/to/service.py`
- route 层：`path/to/main.py`

## 不在范围 / Out of scope

- <thing the planner should NOT add as a task>
- <thing that's deferred to a future PRD>

## Acceptance Criteria

- <observable test that proves the feature works>
- <observable test that proves an edge case is handled>
```

## What the intake heuristic looks for

The intake parser is a lightweight regex+keyword pass — not an LLM. It
scans for these signals:

| Signal | What triggers it | Why it matters |
|---|---|---|
| **source of truth** | Literal phrase "source of truth:" or "数据源:" | Sets `source_of_truth` and `source_of_truth_canonical` fields. Required for layers other than `view`. |
| **layer keywords** | "schema", "repository", "service", "route", "model", "view" | Populates `layers[]`. Missing this = falls back to default 3-layer set + `confidence=low`. |
| **path_type** | Words like "read"/"read-only" vs "write"/"create"/"update"/"delete" | Sets `path_type=read|write|both`. |
| **out_of_scope** | Sections titled "不在范围" / "out of scope" / "Out of scope" | Pulls bullets into `out_of_scope[]` so planner doesn't propose them. |
| **module boundaries** | Explicit file paths (`app/main.py`, `app/service.py`) | Drives `module_boundaries[]`. Inferred from path mentions. |

## Common failures and fixes

### `confidence: low — layers fell back to default service/repository/route set`

Your PRD didn't mention layer names explicitly. Fix: add a `## 分层 / Layers` section that names each layer with a path.

### Planner invented a `frontend` task you didn't ask for

The intake heuristic saw a word like "page", "UI", "view", or "frontend"
somewhere in your PRD. Fix: either remove the word, or add an
`Out of scope` bullet that says "no frontend / no web UI".

### Greenfield run scaffolded `fastapi_api` but you wanted a CLI

The default archetype detector falls back to `fastapi_api` when no other
markers are present. Fix one of:

- Run `kodawari init --archetype <name>` explicitly before `task-plan`
- The archetype manifest written by `init` then locks the choice
- Available archetypes: `fastapi_api`, `flask_api`, `django_web`,
  `node_api`, `react_web`, `fullstack_fastapi_react`, `fullstack_django_react`

### Task got truncated to 3 files but you wanted 5

You're in `existing` mode but the task is bootstrap-shaped
(schema+model+repo+service+test). Fix: switch to `greenfield` mode via
`--mode greenfield` on `task-plan` (greenfield allows up to 5 files per task).

## A worked example

See [examples/hello-bookmark/PRD.md](../examples/hello-bookmark/PRD.md)
for a complete PRD that produces `confidence: high` + a clean 4-5 task
graph.

## Big PRDs: declare slices

If your feature is large enough that a single planning round can't cover
it cleanly, split it into **slices** by adding `## Slice N: <title>`
markers. kodawari auto-detects two or more slices and runs plan + work
once per slice in sequence (see [PIPELINE_DEEP_DIVE.md](PIPELINE_DEEP_DIVE.md)
Stage 1 for the loop semantics).

```markdown
# PRD: <feature name>

## 目标
<top-level outcome that applies to all slices>

## Slice 1: schema + repository
<all the slice-specific scope, contract, layers, acceptance sections
 from the 5-section recipe above>

## Slice 2: API endpoints
<same recipe, scoped to this slice>

## Slice 3: tests + docs
<same recipe>
```

Each slice gets its own `planning/<feature>/slice_NN/` directory and runs
end-to-end (plan + execute + per-task verify and peer review). A single
final review + release runs at the parent level once all slices pass.
`.multi_slice_state.json` tracks progress for resume support.

Synonyms accepted: `## Phase N:`, `## Part N:`, `## 切片 N:`, `## 阶段 N:`,
`## 部分 N:`. The numeric index + colon is required (so descriptive
headings like `## Slice options` won't be mis-detected).

A single slice or zero markers triggers the historical single-slice
flow — full back-compat with PRDs that don't know about slicing.

## When kodawari is the wrong tool

If your feature is genuinely ambiguous, exploratory, or open-ended
("redesign the auth layer"), kodawari will struggle — its strength is
shipping *bounded, contracted* features. Use a chat tool for the
exploration phase; switch to kodawari once you can write a PRD with the
5 sections above.

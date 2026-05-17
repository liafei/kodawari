# kodawari — Documentation Index

Quick-start: read [CAPABILITY_MAP.md](CAPABILITY_MAP.md) first — it answers
"does this backend support feature X?" and is the single reference kept by
all AI agents working on this repo.

For the user-facing path, use [USER_GUIDE.md](USER_GUIDE.md), then
[QUICKSTART.md](QUICKSTART.md) for exact smoke commands. Operators should use
[OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md). For public API and release
compatibility rules, use [../STABILITY.md](../STABILITY.md).

## Architecture

| Doc | Contents |
|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | User path: install, first workflow, status, failure reading |
| [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) | Operator path: lanes, env vars, real review, gates, release audit |
| [一、平台现状、架构与兼容总览.md](architecture/一、平台现状、架构与兼容总览.md) | Platform overview, current state, compatibility |
| [二、运行操作、门禁规则与后续路线.md](operations/二、运行操作、门禁规则与后续路线.md) | Operation guide, gate policy, roadmap |
| [三、中文架构流程图.md](architecture/三、中文架构流程图.md) | Architecture flow diagrams (Chinese) |
| [architecture/diagrams/](architecture/diagrams/) | Mermaid source for all flow diagrams |
| [DESKTOP_DISTRIBUTION.md](DESKTOP_DISTRIBUTION.md) | Desktop/Tauri distribution and BYO Python policy |

## Contracts & Migration

| Doc | Contents |
|---|---|
| [contracts/CONTRACT_CHANGES.md](contracts/CONTRACT_CHANGES.md) | Breaking-change log per contract version; update on every enum change |
| [contracts/ENV_VAR_MIGRATION.md](contracts/ENV_VAR_MIGRATION.md) | WORKFLOW_OPUS_* → WORKFLOW_REVIEWER_* migration schedule |
| [../STABILITY.md](../STABILITY.md) | Public API, CLI tier, artifact schema, and deprecation policy |

## Planning & Design

| Doc | Contents |
|---|---|
| [planning/优化改动方案.md](planning/优化改动方案.md) | 2026-04-21 optimisation plan (P0–P2 tasks) |
| [planning/4.21新重构.md](planning/4.21新重构.md) | 4.21 refactor session notes |
| [planning/gui升级任务.md](planning/gui升级任务.md) | GUI upgrade task list |
| [planning/Harness吸收方案1.0.md](planning/Harness吸收方案1.0.md) | Harness absorption design v1.0 |

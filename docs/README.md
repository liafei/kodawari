# kodawari Documentation

The full documentation set for **kodawari**, the AI coding autopilot that turns
a PRD into shipped, tested code. New here? Start with the project
[README](../README.md), then come back for the deep references below.

## Start here

| Doc | What it covers |
|---|---|
| [QUICKSTART.md](QUICKSTART.md) | First run from a fresh checkout — install, smoke commands, greenfield-from-empty-dir |
| [USER_GUIDE.md](USER_GUIDE.md) | The user path — install, run a feature, read status, interpret failures |
| [WRITING_PRD.md](WRITING_PRD.md) | How to write a PRD kodawari understands — read before your first real run |

## Understand the pipeline

| Doc | What it covers |
|---|---|
| [PIPELINE_DEEP_DIVE.md](PIPELINE_DEEP_DIVE.md) | What actually happens inside `work-all` — every stage and safety mechanism, with code locations |
| [CAPABILITY_MAP.md](CAPABILITY_MAP.md) | Capability × backend matrix — which backend supports which feature |
| [architecture/PLATFORM_OVERVIEW.zh-CN.md](architecture/PLATFORM_OVERVIEW.zh-CN.md) | Platform overview, current state, and compatibility scope (中文) |
| [architecture/ARCHITECTURE_DIAGRAM.zh-CN.md](architecture/ARCHITECTURE_DIAGRAM.zh-CN.md) | Annotated architecture flow diagrams (中文) |
| [ARCHITECTURE_FLOW.zh-CN.md](ARCHITECTURE_FLOW.zh-CN.md) | Single-page Mermaid flowchart of the full run (中文) |
| [architecture/diagrams/](architecture/diagrams/) | Mermaid source for all flow diagrams |

## Operate

| Doc | What it covers |
|---|---|
| [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) | Error codes, troubleshooting, multi-slice diagnostics |
| [operations/RUNBOOK_AND_GATES.zh-CN.md](operations/RUNBOOK_AND_GATES.zh-CN.md) | Deep operator/CI runbook — lanes, gate policy, real review, release audit (中文) |
| [operations/autopilot_invocation_runbook.zh-CN.md](operations/autopilot_invocation_runbook.zh-CN.md) | Advanced explicit-flag invocation examples and backend troubleshooting (中文) |
| [DESKTOP_DISTRIBUTION.md](DESKTOP_DISTRIBUTION.md) | Desktop/Tauri distribution and bring-your-own-Python policy |

## Reference & contracts

| Doc | What it covers |
|---|---|
| [contracts/ENV_VAR_REFERENCE.md](contracts/ENV_VAR_REFERENCE.md) | Every environment variable, what it does, and its default |
| [contracts/ENV_VAR_MIGRATION.md](contracts/ENV_VAR_MIGRATION.md) | `WORKFLOW_*` → reviewer-prefixed env var migration schedule |
| [contracts/CONTRACT_CHANGES.md](contracts/CONTRACT_CHANGES.md) | Breaking-change log per contract version |
| [contracts/DEPRECATED_IMPORTS.md](contracts/DEPRECATED_IMPORTS.md) | Deprecated import paths and their replacements |
| [../STABILITY.md](../STABILITY.md) | Public API surface, CLI tiers, artifact schema, and deprecation policy |
| [../CHANGELOG.md](../CHANGELOG.md) | Release history |

## 中文文档

中文读者可优先阅读：[平台总览](architecture/PLATFORM_OVERVIEW.zh-CN.md) ·
[运行 & 门禁手册](operations/RUNBOOK_AND_GATES.zh-CN.md) ·
[流程深度剖析](PIPELINE_DEEP_DIVE.zh-CN.md) ·
[如何写 PRD](WRITING_PRD.zh-CN.md) ·
[架构流程图](ARCHITECTURE_FLOW.zh-CN.md)。

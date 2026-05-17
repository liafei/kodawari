# 一、平台现状、架构与兼容总览

## 1. 文档目的

本文件用于在一份中文文档内收口以下主题：

- 当前 generic runtime / autopilot 产品语义
- 平台主链与 canonical truth 世界观
- 官方支持范围、archetype 与 capability
- `autopilot` 与 `status` 的正式角色
- 兼容入口、历史吸收与当前边界
- 当前证明基线与剩余差距

原始历史文档已完整归档到：`e:\code_rebuild\temp\kodawari_文档归档_2026-03-27_094833`。

## 2. 平台当前定位

`kodawari` 当前的正式定位是：

- 面向大多数 Python / Node Web 项目的通用开发工作流平台
- 用户主入口为 `autopilot`，观察入口为 `status`
- 阶段命令继续保留，但只作为 `operator / CI / debug` 路径
- `newsapp` 仅作为复杂 benchmark，不再作为平台语义来源
- 产品主线采用 `contract-first + canonical runtime truth + gate + observability`
- 官方原生执行路径固定为 `codex_cli`
- review 采用 `simulated` 与 `real_opus` 双车道，并要求对外语义诚实

## 3. 官方支持范围

### 3.1 官方 archetype

当前官方支持范围聚焦于：

- `fastapi_api`（FastAPI API 项目）
- `flask_api`（Flask API 项目）
- `django_web`（Django Web 项目）
- `node_api`（Node API 项目）
- `react_web`（React Web 项目）
- `fullstack_fastapi_react`（FastAPI + React 全栈项目）
- `fullstack_django_react`（Django + React 全栈项目）
- `monorepo_workspace`（多工作区项目）

### 3.2 官方 capability

能力扩展采用 `capability` 承载，当前主线已吸纳：

- `postgres_db`（数据库能力）
- `docker_deploy`（部署能力）
- `capacitor_mobile`（移动壳能力）
- `monorepo_workspace`（多工作区能力）
- `worker_scheduler`（任务调度能力）
- `docs_runbook`（文档/运行手册能力）

## 4. 主入口与世界观

### 4.1 用户主入口

默认用户路径固定为：

- `kodawari autopilot --prd <path>`：从 PRD 或既有真值恢复，自动推进 planning 与 runtime
- `kodawari status`：读取当前 canonical truth，展示是否继续自动推进、等待决策还是等待环境

这意味着：

- 正常开发不要求用户手工串 `prd-intake -> architecture-plan -> task-plan -> task-prepare -> task-run -> review -> verify -> qa -> ship-readiness`
- 系统应优先自动推进；只有在必须拍板、环境前置缺失或预算/无进展触发时才暂停

### 4.2 operator / CI / debug 入口

以下阶段命令继续保留，但定位已经下沉：

- `prd-intake`
- `architecture-plan`
- `init`
- `task-plan`
- `task-prepare`
- `task-run`
- `review`
- `verify`
- `qa`
- `ship-readiness`

它们的用途是：

- operator 分阶段排障
- CI 固定回归
- 平台内部 debug 与 proof
- 典型固定入口包括 `kodawari lane-history-fetch`、`kodawari lane-trend`、`.github/workflows/kodawari-always-on.yml`、`.github/workflows/kodawari-integration.yml`、`.github/workflows/kodawari-standing-proof.yml`
- lane/operator 报告现在统一补上 `root_cause_bucket` 语义，用来压缩宽泛 runtime 异常

## 5. 兼容与历史吸收

### 5.1 `workflow-claude` 吸收策略

历史 `workflow-claude` 中仍有价值的语义，当前按以下原则吸收到 `kodawari`：

- 历史源码只作为只读证据
- 运行时语义只在 `kodawari` 内实现
- 兼容入口只保留用户仍可能调用、且对主链有意义的部分
- 用户主路径统一收口到 `autopilot` 与 `status`

### 5.2 兼容命令族

当前仍保留的兼容命令族包括：

- `kodawari compact`
- `kodawari research`
- `kodawari develop`
- `kodawari quick-develop`
- `kodawari optimize-existing-develop`

这些入口已经被路由到统一的 `kodawari` CLI 体系下，不再保留第二套运行内核。

### 5.3 历史索引与归档映射

当前归档目录中保留了完整历史材料，其中关键索引文件包括：

- `WORKFLOW_CLAUDE_SOURCE_INDEX.md`
- `WORKFLOW_CLAUDE_TEST_DAMAGE_INDEX.md`
- `WORKFLOW_CLAUDE_ABSORPTION_PRIORITY.md`

这些原始文档已经迁移到：

- `e:\code_rebuild\temp\kodawari_文档归档_2026-03-27_094833`

## 6. 主链与 canonical truth

### 6.1 planning 真值

当前 planning 主链使用以下 canonical artifacts：

- `PRD_INTAKE.json`
- `REPO_INVENTORY.json`
- `ARCHITECTURE_PLAN.json`
- `TASK_GRAPH.json`
- `TASK_CARD_ACTIVE.json`

主链阶段为：

- `prd-intake`（PRD 解析）
- `architecture-plan`（架构规划）
- `init`（greenfield 骨架初始化）
- `task-plan`（任务图生成）
- `task-prepare`（活动任务投影）

### 6.2 runtime 真值

当前 runtime 主链使用以下 canonical artifacts：

- `.execution_request.json`
- `.execution_result.json`
- `.review_bundle.json`
- `.review_evidence.json`
- `.verify_report.json`
- `.qa_report.json`
- `.ship_readiness.json`
- `.decision_request.json`
- `.decision_response.json`

对应阶段为：

- `execution`
- `review`
- `verify`
- `qa`
- `ship-readiness`
- `decision bridge`

### 6.3 交互状态语义

当前自动驾驶运行态已收口为：

- `RUNNING`：系统可以继续自动推进
- `AWAITING_DECISION`：需要业务/架构/发布拍板
- `AWAITING_ENVIRONMENT`：外部环境前置缺失，系统无法自动修复
- `BLOCKED`：系统已尝试推进，但因预算、无进展或结构化失败而阻断
- `PASS`：当前链路通过

## 7. 核心架构总览

### 7.1 核心模块

- `kodawari.cli.main`（主 CLI 入口）
- `kodawari.cli.parser_registry`（命令注册中心）
- `kodawari.cli.autopilot_cmd`（自动驾驶编排入口）
- `kodawari.cli.autopilot_workflow_runtime`（自动驾驶运行态装配）
- `kodawari.cli.status_cmd`（状态聚合输出）
- `kodawari.cli.contract_first_cmd`（规划阶段兼容入口）
- `kodawari.autopilot.engine`（引擎装配层）
- `kodawari.autopilot.local_adapter`（本地适配层）
- `kodawari.autopilot.architecture_plan`（架构规划真值模型）
- `kodawari.autopilot.repo_inventory`（仓库结构真值模型）
- `kodawari.autopilot.execution_artifacts`（执行工件总调度）
- `kodawari.autopilot.execution_codex_cli`（Codex CLI 后端）
- `kodawari.autopilot.verify_surfaces`（verify surface 规划器）
- `kodawari.autopilot.decision_bridge`（决策桥接层）
- `kodawari.cli.status_contract_first`（planning 状态聚合）
- `kodawari.cli.status_runtime`（runtime 状态聚合）
- `kodawari.gate.checker_*`（质量门禁检查器）
- `kodawari.autopilot.engine_*_mixin`（自动循环引擎分层 mixin）

### 7.2 执行、评审与验证语义

当前 runtime 语义固定为：

- 执行真值由 `.execution_request.json` 与 `.execution_result.json` 承载
- 官方 native executor 为 `codex_cli`
- review 真值必须显式区分 `simulated` 与 `real_opus`
- verify 不再等价于单命令壳，而是支持 multi-surface planner
- `qa` 与 `ship-readiness` 必须消费 execution / review / verify 的 canonical truth，而不是只看 legacy summary

### 7.3 决策桥语义

当前自然对话决策桥已经进入主线，使用：

- `.decision_request.json`（决策请求工件）
- `.decision_response.json`（决策响应工件）

当前固定支持的 `decision_kind` 为：

- `intent_clarification`（需求意图澄清）
- `architecture_freeze`（架构冻结确认）
- `task_plan_freeze`（任务图冻结确认）
- `release_approval`（发布审批）

平台职责是：

- 在需要拍板的点写出结构化请求
- 再次运行 `autopilot` 时自动消费响应并恢复执行

## 8. 官方 generic proof 基线

### 8.1 通用 canary matrix

当前官方 generic canary matrix 固定为 8 条 archetype 样例：

- `fastapi_api`
- `flask_api`
- `django_web`
- `node_api`
- `react_web`
- `fullstack_fastapi_react + docker_deploy + postgres_db`
- `fullstack_django_react + capacitor_mobile`
- `monorepo_workspace`

### 8.2 复杂 benchmark

复杂 benchmark 固定保留：

- `newsapp` 级项目

它的用途是：

- 验证多表面、跨层、脚本、部署与复杂协作压力
- 不作为平台内部语义来源

### 8.3 当前已证明的事实

截至 `2026-03-28`，平台已经完成并证明：

- planning 真值链与 runtime 真值链均可被 `status` 直接消费
- `autopilot` 已接上 contract-first planning bridge，可承接从 PRD 开始的自动推进
- `codex_cli` 已有 native executor proof
- multi-surface verify 已覆盖 `backend`、`frontend`、`workspace`、`scripts_deploy`、`docs`、`mobile_wrapper`
- blocked-path 已覆盖 `architecture_plan_required`、`verify_surface_ambiguous`、`verify_recipe_missing`、surface coverage inconsistency、`SCOPE_DRIFT_BLOCKED`
- always-on lane 已纳入 `newsapp` benchmark 自动驾驶 happy/block proof
- integration lane 已纳入 `newsapp` real-review 语义 proof（env-gated）
- standing-proof workflow 已固定 `lane-history-fetch -> lane-trend` 周级趋势流水线（历史 artifact 拉取与样本完整度仍受外部可用性影响）
- lane triage / lane-trend / stability-report 已可统一聚合 `root_cause_bucket`
- 全量 passed/skip 数与 lane 状态以 CI 产物为准，不在本文手写固定数字（推荐基线来源：`planning/lane_stability_always-on.json`、`planning/lane_stability_integration.json`、`planning/lane_weekly_trend.json`）
- integration env 缺失时的结构化 `SKIP` 仅表示环境未就绪，不能算 real-review pass 或 standing-proof 通过

当前阶段判断按“可证明工件”口径收口为：

- 主链能力已收口，后续重点在 integration 真环境连续稳定度与 observability 根因压缩
- 进度判断优先依据 lane 稳定性与 triage 趋势工件，不采用长期固化百分比

## 9. 验收与门禁基线

当前验收世界观固定为：

- 先看 canonical truth 是否一致
- 再看 verify / qa / ship-readiness 是否真实消费这些真值
- 再看 `status` 是否能直接暴露真实运行语义
- 最后看 operator lane 与 integration lane 是否持续可复现

门禁状态模型沿用：

- item 级：`PASS` / `PARTIAL` / `FAIL`
- total 级：`PASS` / `BLOCKED`
- 默认 profile：`advisory`

## 10. 历史恢复成果与文档映射

恢复初期与当前主线之间的关键差异已经完成修补：

- `autopilot` 已从旧 planning markdown 占位流，收口为可承接 contract-first 真值的自动驾驶入口
- `status` 已从偏 legacy summary 的视角，收口为 canonical truth 对齐视角
- `state` 与稳定性报告相关超长文件已经拆分到可维护范围
- 单文件超过 `1000` 行的红线已清零

下列历史主题已并入本文件：

- 平台定位、generic runtime、autopilot 产品语义
- 架构总览、兼容吸收、恢复背景
- archetype / capability 支持范围
- canonical truth 世界观
- generic canary matrix 与 `newsapp` benchmark 的角色划分

### 10.1 改造升级清单当前断点

截至 `2026-04-02`，当前主线文档口径已经推进到：

- 历史恢复与 V2 改造任务 `WS-001 ~ WS-260` 已在权威清单中回写完成
- `docs/二、运行操作、门禁规则与后续路线.md` 中追加的 `WS-270 ~ WS-281` 也已完成回写，覆盖 `code health baseline`、`ratchet`、`duplicate-code`、`module ownership`、`domain SoT`
- 当前不再存在“文档里还是 Pending，但代码已经落地”的已知断点

当前剩余问题已经收敛为两类：

- `real_opus` standing proof 的真实环境长期常绿
- ratchet / ownership 在真实项目中的持续运营，而不是功能本身未接线

若后续继续新增同主题任务，建议从 `WS-282` 续编，避免编号再次倒挂。

完整原文仍保留在归档目录中，不做信息删除。

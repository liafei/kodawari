# 二、运行操作、门禁规则与后续路线

> ⚠️ **本文是面向 operator/CI/排障的深度运行手册**。普通用户上手请先看
> [QUICKSTART.md](../QUICKSTART.md) + [USER_GUIDE.md](../USER_GUIDE.md)；流程内核
> 见 [PIPELINE_DEEP_DIVE.zh-CN.md](../PIPELINE_DEEP_DIVE.zh-CN.md)。本文里
> 出现的 `--executor-backend XXX --max-cycles N` 等显式 flag 形式从 v0.1.2 起
> **大多数情况下不需要**——`.claude/workflow/defaults.yaml` + 内置默认已经
> 覆盖。仅在需要 override 默认时才显式传。

## 1. 文档目的

本文件是面向 operator、CI 与排障场景的中文运行手册，集中说明：

- canonical 5 动词与 `wf-*` facade 主入口使用方式
- 官方 generic canary matrix
- 本地与 integration 两条验证 lane
- `codex_cli` 使用要求
- real review 环境变量语义
- `status` 新字段解释
- blocked-path 错误码与排障方式
- 门禁规则、代码质量红线与最终收口路线

原始历史文档已完整归档到：`e:\code_rebuild\temp\kodawari_文档归档_2026-03-27_094833`。

## 2. 产品主入口与 operator 边界

### 2.1 正常用户路径

当前默认用户路径固定为：

```powershell
.\scripts\kodawari.ps1 setup --project-root .
.\scripts\kodawari.ps1 plan --project-root . --feature sample-feature --prd .\planning\PRD.md
.\scripts\kodawari.ps1 work --project-root . --feature sample-feature --prd .\planning\PRD.md
.\scripts\kodawari.ps1 review --project-root . --feature sample-feature
.\scripts\kodawari.ps1 release --project-root . --feature sample-feature
.\scripts\kodawari.ps1 status --project-root . --feature sample-feature
```

说明：

- canonical CLI 推荐固定为 `setup -> plan -> work -> review -> release`。
- shell facade `wf-setup/wf-plan/wf-work/wf-review/wf-release/wf-status` 与 canonical CLI 语义等价。
- `status` 继续作为只读观察入口，展示当前真值、交互状态与下一步动作。

### 2.2 operator / CI / debug 路径

以下命令保留，但只用于 operator、CI 与排障：

- `autopilot`
- `work-all`
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

这意味着：

- 可以继续用这些命令做分阶段回归。
- 但它们不再是产品文档对最终用户推荐的主路径。

### 2.3 Backend Capability 现状

以下表格同时描述当前 descriptor 口径和 runtime 真值。
其中 `claude_code` 已经把 `supports_worktree_isolation` 回写到 descriptor；`hooks` 与 `memory` 仍保持保守，并通过 runtime truth 字段区分 `kernel_only` 与 native host capability。

| backend / capability | 当前状态 | 说明 |
|------|------|------|
| `codex_cli.implemented` | implemented | 当前已接入标准 execution backend registry，可作为执行后端使用 |
| `codex_cli.supports_deterministic_changed_files` | implemented | 当前实现会基于允许写入文件的哈希变化回推 `changed_files` |
| `codex_cli.supports_agent_teams` | planned | 当前 `execution_codex_cli.py` 还是 `codex exec` subprocess，不是 native team orchestration |
| `codex_cli.supports_worktree_isolation` | planned | 当前没有 backend 内建的隔离执行目录/工作树接线 |
| `codex_cli.supports_hooks` | planned | 当前没有 native host hooks 接线 |
| `codex_cli.supports_memory` | planned | 当前没有 native host memory 接线 |
| `claude_code.implemented` | implemented | 当前已接入标准 execution backend registry，可作为执行后端使用 |
| `claude_code.supports_deterministic_changed_files` | implemented | 当前实现会基于允许写入文件的哈希变化回推 `changed_files` |
| `claude_code.supports_agent_teams` | planned | 当前 `execution_claude_code.py` 只是 `claude -p <prompt>` subprocess，不是 native Agent Teams |
| `claude_code.supports_worktree_isolation` | implemented | 当前 backend 已在 `planning_dir/.parallel_workers/claude_code/` 下使用 `directory_isolation` 执行并回写允许修改文件；descriptor 已恢复 `True`，但这仍是隔离目录方案，不是 git-native worktree |
| `claude_code.supports_hooks` | runtime-guarded / descriptor-false | 当前 backend 已有 backend-local preflight execution guard，会对 override command fail-closed；这不等于 Claude host hooks，所以 descriptor 仍保持 `False` |
| `claude_code.supports_memory` | compact-injected / descriptor-false | 当前 backend 会把 `semantic_compact.json` 中的 compact context 注入 prompt；这代表 kernel-level context injection，不等于 native host memory，所以 descriptor 仍保持 `False` |

运行约束：

- 若 capability 仍处于 `planned`，`status` 或文档不得把它写成已可用。
- 宿主平台“理论支持”不等于 `kodawari` 当前 backend “已经接线并实测通过”。
- 后续若某项能力从 `planned` 升级到 `implemented`，必须同时满足：
  - 代码接线完成
  - 有测试
  - descriptor 与运行文档一起回写
- `claude_code` 当前新增的 runtime 事实如下：
  - 隔离执行使用 `directory_isolation`，不是 git-native worktree。
  - compact context 来自 `semantic_compact.json`，不是 native host memory。
  - preflight guard 是 backend 内部 fail-closed 保护，不是 host hook surface。

## 3. 仓库本地运行方式

推荐入口：

```powershell
.\scripts\kodawari.ps1 setup --help
.\scripts\kodawari.ps1 plan --help
.\scripts\kodawari.ps1 work --help
.\scripts\kodawari.ps1 review --help
.\scripts\kodawari.ps1 release --help
.\scripts\kodawari.ps1 status --help
.\scripts\kodawari.ps1 gate --help
.\scripts\kodawari.ps1 telemetry --help
.\scripts\kodawari.ps1 wf-work --help
.\scripts\kodawari.ps1 wf-status --help
```

`scripts\kodawari.ps1` 会保留调用者当前目录，并通过 `WORKFLOWCTL_REPO_ROOT` 定位 SDK 代码；跨项目运行时仍应显式传入 `--project-root <target>`。

显式可执行入口：

```powershell
.\.workflow_runtime\local-env\.venv\Scripts\kodawari.exe setup --help
.\.workflow_runtime\local-env\.venv\Scripts\kodawari.exe plan --help
.\.workflow_runtime\local-env\.venv\Scripts\kodawari.exe work --help
.\.workflow_runtime\local-env\.venv\Scripts\kodawari.exe status --help
.\.workflow_runtime\local-env\.venv\Scripts\kodawari.exe review --help
.\.workflow_runtime\local-env\.venv\Scripts\kodawari.exe release --help
.\.workflow_runtime\local-env\.venv\Scripts\kodawari.exe gate --help
```

模块入口：

```powershell
python -m kodawari.cli.main setup --help
python -m kodawari.cli.main plan --help
python -m kodawari.cli.main work --help
python -m kodawari.cli.main status --help
```

启动前准备：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_kodawari.ps1
```

如果环境不允许升级 `pip`：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_kodawari.ps1 -SkipPipUpgrade
```

仓库内 lane recipe 入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane_repeat.ps1 -Repeat 3
powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane_repeat.ps1 -Repeat 3 -FailIfSkipped
```

GitHub Actions 示例入口：

- `.github/workflows/kodawari-always-on.yml`
- `.github/workflows/kodawari-integration.yml`
- `.github/workflows/kodawari-standing-proof.yml`

辅助参数：

- `-ListOnly`：只打印 lane 对应 pytest 组合，不执行
- `-PytestArgs ...`：在固定 recipe 后追加 pytest 过滤参数，例如 `-PytestArgs '-k', 'surface'`
- `run_integration_lane.ps1 -FailIfSkipped`：当缺少 integration 环境变量时，不允许以 skip 结束
- `run_lane_stability.ps1`：按固定 lane 重复执行并产出稳定性 summary JSON
- `planning/lane_stability_always-on.json` / `planning/lane_stability_integration.json`：固定稳定性 summary 输出位置
- `planning/lane_triage_always-on.json` / `planning/lane_triage_always-on.md` / `planning/lane_triage_integration.json` / `planning/lane_triage_integration.md`：固定 operator triage 输出位置
- triage JSON 会固定输出 `root_cause_bucket` / `root_cause_label`，用于把 `env_missing`、`gate_blocked`、`verify_setup`、`external_gateway` 等根因桶直接暴露给值班侧
- `.github/workflows/*` 会把上述 summary + triage 作为 `actions/upload-artifact@v4` 产物上传，便于 nightly/dispatch 留痕
- workflow 同时会把 triage markdown 写入 `GITHUB_STEP_SUMMARY`，让值班侧直接看到固定恢复动作
- `kodawari lane-history-fetch --repo <owner/repo> --max-history-days 7`：自动下载最近 lane artifact 到 `planning/lane_history`
- `.github/workflows/kodawari-standing-proof.yml` 会固定执行 `lane-history-fetch -> lane-trend`
- `planning/lane_history_manifest.json`：最近一轮 standing-proof 历史拉取清单
- `kodawari lane-trend --artifacts-root <history-dir> --required-pass-streak 3`：把最近下载的 triage artifact 聚成周级连续稳定度报告，并输出 `root_cause_bucket_counts`
- 对 non-stable lane，`lane-trend` 会额外输出 `incident_candidates` / `recommended_incidents`，其中包含 `severity/title/summary/component/impact/tag/evidence_files` 与 `kodawari incident-ingest` 模板命令
- 默认输出为 `planning/lane_weekly_trend.json` / `planning/lane_weekly_trend.md`

## 4. 自动驾驶运行语义

### 4.1 自动推进主链

在 happy path 下，`work` / `work all` 负责自动推进：

- `prd-intake`
- `architecture-plan`
- `init`
- `task-plan`
- `task-prepare`
- `execution`
- `review`
- `verify`
- `qa`
- `ship-readiness`

如果相关 planning/runtime 真值已存在且合法，`work` / `work all` 会直接从可恢复阶段继续。

### 4.2 决策点与环境阻断

`work` / `work all` 只在以下三类情况允许停下来：

- 业务、架构或发布需要人工拍板
- 外部环境前置条件缺失，系统无法自动修复
- 已触发预算或无进展阈值，系统尝试自动修复后仍未成功

对应交互状态为：

- `RUNNING`
- `AWAITING_DECISION`
- `AWAITING_ENVIRONMENT`
- `BLOCKED`
- `PASS`

### 4.3 决策桥工件

当前自然对话桥接采用：

- `.decision_request.json`
- `.decision_response.json`

当前固定支持的 `decision_kind`：

- `intent_clarification`
- `architecture_freeze`
- `task_plan_freeze`
- `release_approval`

operator 的职责是：

- 读取 `.decision_request.json`
- 通过外层对话界面向用户问清楚问题
- 把回应写回 `.decision_response.json`
- 再次运行 `work` / `work all`，由系统自动恢复执行

## 5. 官方验证矩阵与两条 lane

### 5.1 官方 generic canary matrix

当前官方 archetype matrix 固定为：

- `fastapi_api`
- `flask_api`
- `django_web`
- `node_api`
- `react_web`
- `fullstack_fastapi_react + docker_deploy + postgres_db`
- `fullstack_django_react + capacitor_mobile`
- `monorepo_workspace`

复杂 benchmark 固定保留：

- `newsapp` 级项目

### 5.2 always-on lane

作用：

- generic canary matrix happy path
- blocked path
- `codex_cli` native executor proof
- multi-surface verify proof
- `autopilot` 自动驾驶主线 proof
- runtime observability regression

仓库内推荐入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane.ps1
```

当前 recipe 固定执行以下 pytest 组合：

- `tests/test_generic_runtime_proof.py`
- `tests/test_autopilot_codex_cli_smoke.py`
- `tests/test_verify_surface_runtime_proof.py`
- `tests/test_status_interaction_state.py`
- `tests/test_autopilot_autodrive_decisions.py`
- `tests/test_runtime_observability_logging.py`

operator / CI 说明：

- 这条 lane 面向常绿回归，优先验证 generic canary、blocked-path、`codex_cli` smoke、verify surface proof 与状态/日志回归
- 如果只想查看当前 recipe 是否漂移，可先运行 `.\scripts\run_always_on_lane.ps1 -ListOnly`
- 如果需要临时聚焦某一组样例，可追加 `-PytestArgs '-k', '<expr>'`，但不应直接修改 recipe 定义来做一次性排障
- GitHub Actions 示例 job 固定在 `.github/workflows/kodawari-always-on.yml`
- `pull_request` 下固定使用 `run_always_on_lane_repeat.ps1 -Repeat 1`，保证 PR 与 nightly 走同一稳定性入口
- `schedule` / `workflow_dispatch` 默认走 `run_always_on_lane_repeat.ps1 -Repeat 3`，并上传 `planning/lane_stability_always-on.json`
- summary JSON 的 `summary_version` 当前固定为 `lane.stability.v1`
- triage JSON / markdown 的版本当前固定为 `lane.triage.v1`
- 当前固定分类至少覆盖 `lane.stable_pass`、`lane.flaky_failure`、`lane.consistent_failure`
- triage 还会补充 `root_cause_bucket`，把分类进一步压成 operator 可消费的根因桶

全量 passed/skip 数与 lane 状态以 CI 产物为准，不在本文手写固定数字。

推荐基线来源：

- `planning/lane_stability_always-on.json`
- `planning/lane_stability_integration.json`
- `planning/lane_weekly_trend.json`

### 5.3 integration lane

作用：

- 真实 `real_opus` review proof
- integration 环境下的真实 executor / review 联动验证

运行要求：

- 必须显式提供 real review 所需环境变量
- 没有 key 时允许 `skip`
- 不允许在 integration lane 中退化成 simulated 后继续算绿

仓库内推荐入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane.ps1
```

稳定性重复回归入口：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane_repeat.ps1 -Repeat 3
powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane_repeat.ps1 -Repeat 3 -FailIfSkipped
```

当前 recipe 固定执行以下 pytest 组合：

- `tests/test_generic_runtime_real_review.py`
- `tests/test_cli_real_e2e_smoke.py`

operator / CI 说明：

- `run_integration_lane.ps1` 会先检查 `WORKFLOW_REVIEWER_API_KEY` 与 `WORKFLOW_REVIEWER_BASE_URL`
- 缺少环境变量时，默认以结构化 `SKIP` 结束，便于本地或无 key CI 复用同一入口
- 结构化 `SKIP` 仅表示 integration 环境未就绪，不能计作 real-review pass，也不能计作 standing-proof 绿灯
- 如果这是专门的 integration job，推荐使用 `.\scripts\run_integration_lane.ps1 -FailIfSkipped`，避免 job 因缺 key 被误判为正常完成
- 建议在同一 job 中保留 `WORKFLOW_REVIEW_ENABLED=1` 与 `WORKFLOW_REVIEW_REQUIRED=1`，确保 real review 语义 fail-closed
- GitHub Actions 示例 job 固定在 `.github/workflows/kodawari-integration.yml`，并显式映射 `WORKFLOW_REVIEWER_API_KEY`、`WORKFLOW_REVIEWER_BASE_URL`、`WORKFLOW_REVIEW_ENABLED=1`、`WORKFLOW_REVIEW_REQUIRED=1`
- integration workflow 固定走 `run_integration_lane_repeat.ps1 -Repeat 3 -FailIfSkipped`
- workflow 会上传 `planning/lane_stability_integration.json`，用于 real review standing proof 留痕
- triage 分类会额外区分 `lane.integration_env_missing` 与 `lane.integration_env_missing_fail_closed`
- 值班侧优先读 `lane_triage_integration.md`，再决定这是环境事故还是产品回归
- 若需要按周看恢复进度，优先看 `lane_weekly_trend.json` / `lane_weekly_trend.md` 里的 `latest_root_cause_bucket`、`root_cause_bucket_counts`、`incident_candidates` 与 `recommended_incidents`

## 5.4 operator 执行顺序建议

推荐按以下顺序执行：

1. `powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_kodawari.ps1`
2. `powershell -ExecutionPolicy Bypass -File .\scripts\run_always_on_lane.ps1`
3. 若 integration 环境已就绪，再执行 `powershell -ExecutionPolicy Bypass -File .\scripts\run_integration_lane.ps1 -FailIfSkipped`
4. 失败时先看脚本打印出的固定 pytest 目标，再根据失败测试回到 `kodawari status`、`kodawari review`、`kodawari verify` 做分阶段排障
5. 每周把下载下来的 lane triage artifact 目录交给 `kodawari lane-trend --artifacts-root <history-dir> --required-pass-streak 3`，判断 standing proof 是否已恢复到连续稳定
6. 若 `recommended_incidents` 非空，选择 repo-local `--planning-dir` 或 `--feature` 后，直接执行 payload 里的 `suggested_command`，把 standing-proof 问题转入 `incident-ingest -> field-report`

## 6. `codex_cli` 使用要求

当前官方原生执行后端固定为 `codex_cli`。

执行要求固定为：

- kodawari 先写 `.execution_request.json`
- 执行层消费 request truth 再调用 `codex_cli`
- backend 必须给出 deterministic `changed_files`
- 显式 `verification_only_noop` / no-write 任务允许 `changed_files=[]`，但必须在 `.execution_result.json` 暴露 no-op 标记与 scoped verify 证据；review 以 execution / verify / gate artifact 审查，不要求伪造 diff
- 最终必须写出 `.execution_result.json`
- 如果缺少 binary、返回码失败、`changed_files` 不可解释或结果不完整，必须结构化 `BLOCKED` 或 `FAIL`

常用相关参数与环境变量：

- `--executor-backend codex_cli`
- `--executor-command`：可选模板覆盖，不改变官方主路径定义
- `WORKFLOW_CODEX_EXECUTABLE`：覆盖 `codex_cli` 可执行路径

## 7. real review 环境变量语义

当前 real review 规则固定为：

- `WORKFLOW_REVIEWER_API_KEY` 非空且 `WORKFLOW_REVIEW_ENABLED` 未显式关闭时，自动请求 real review
- `WORKFLOW_REVIEW_ENABLED=0`：显式关闭 auto-enable
- `WORKFLOW_REVIEW_ENABLED=1`：显式开启
- `WORKFLOW_REVIEW_REQUIRED=1`：要求 fail-closed，不允许缺 real review 时继续伪装成功
- `WORKFLOW_REVIEWER_BASE_URL`：指定真实 review gateway
- `WORKFLOW_OPUS_*` 旧变量仍兼容读取，但会触发 `DeprecationWarning`；删除日期见 `docs/contracts/ENV_VAR_MIGRATION.md`

review 输出必须显式暴露：

- `review_mode`
- `real_review_requested`
- `real_review_required`
- `fallback_used`

解释：

- `review_mode=simulated`：模拟评审车道
- `review_mode=real_opus`：真实评审车道
- `fallback_used=true`：本轮发生了退化或兼容 fallback，需要结合 evidence 排障

## 8. `status` 新字段解释

`kodawari status` 当前重点字段包括：

- `interaction_state`：当前交互状态，决定系统是继续自动推进还是等待外部输入
- `decision_kind`：当前等待的决策类型
- `decision_id`：当前决策请求标识
- `decision_request_present`：当前目录下是否存在待处理决策请求
- `next_action_type`：下一步动作类型，取值为 `auto_continue | await_decision | await_environment | resolve_blocked | completed`
- `repo_inventory_present`：仓库真值是否存在
- `architecture_plan_present`：架构规划真值是否存在
- `planning_requirements`：当前场景要求哪些 planning 工件
- `planning_truth_source`：planning 真值来源
- `execution_truth_source`：execution 真值来源
- `review_truth_source`：review 真值来源
- `verify_truth_source`：verify 真值来源
- `execution_backend`：当前实际使用的执行后端
- `review_mode`：当前评审模式
- `real_review_requested`：是否请求真实评审
- `real_review_required`：是否要求真实评审
- `fallback_used`：是否发生运行时回退
- `verify_scope_mode`：verify 覆盖模式
- `verify_surfaces`：本次 verify 覆盖的 surface 列表
- `tokens_used`：累计 token 消耗
- `token_budget`：预算上限
- `budget_exhausted`：是否已耗尽预算

判读原则：

- 先看 `interaction_state`
- 再看 `planning_* / execution_* / review_* / verify_*` 真值来源是否齐全
- 再看 `review_mode`、`verify_surfaces`、`budget_exhausted` 是否与期望一致

## 9. blocked-path 错误码与处理方式

### 9.1 `architecture_plan_required`

含义：

- greenfield repo 或 multi-surface existing repo 缺 `ARCHITECTURE_PLAN.json`

处理方式：

- 重新让 `autopilot` 从 `--prd` 开始自动生成，或由 operator 先跑 `architecture-plan`
- 确认 architecture truth 后再继续执行

### 9.2 `verify_surface_ambiguous`

含义：

- changed files 与 `REPO_INVENTORY.json` 无法确定 deterministic verify surface

处理方式：

- 先修正 surface roots、surface mapping 或 verify recipe
- 只有在业务上确实需要时才临时用显式 verify 命令覆盖

### 9.3 `verify_recipe_missing`

含义：

- 已选中的 surface 没有 deterministic verify recipe

处理方式：

- 在 `ARCHITECTURE_PLAN.json` 或 `REPO_INVENTORY.json` 中补齐 verify recipe
- 不允许用 broad fallback 伪装成通过

### 9.4 `SCOPE_DRIFT_BLOCKED`

含义：

- strict-scope 下，execution `changed_files` 超出允许范围

处理方式：

- 检查 `.execution_result.json`
- 收窄 task scope 或修正 executor prompt
- 必要时重新准备任务卡，再让 `autopilot` 继续

### 9.5 `AWAITING_ENVIRONMENT`

含义：

- 当前不是代码修复问题，而是外部前置条件缺失，例如执行器不存在、API key 缺失、权限不足或网络不可达

处理方式：

- 先补齐环境前置
- 再重新运行 `autopilot`

## 10. 门禁规则与代码质量红线

### 10.1 Gate 规则

当前 gate 规则保留：

- checker item 级状态：`PASS` / `PARTIAL` / `FAIL`
- total gate 状态：`PASS` / `BLOCKED`
- 默认 profile：`advisory`

`advisory` 的语义是：

- 默认报告违规
- 默认不直接阻断总状态
- 只有在更严格 profile 下，或运行时证据触发强阻断时，才会变成真正的 `BLOCKED`

### 10.2 代码质量红线

当前明确执行的质量红线为：

- 共享 canonical 来源固定为 `code-redline` 包中的 `code_redline.REDLINE`
- 最大嵌套层级 `4`：超过即 `BLOCK`
- 圈复杂度 `7–10`：`WARN`
- 圈复杂度 `>10`：`BLOCK`
- 文件 `>1000` 行且 file complexity-sum `>20`：`WARN`
- 文件 `>1500` 行且 file complexity-sum `>30`：`BLOCK`
- 文件 `>1500` 行但 complexity-sum `<=20`：仅记 `DASHBOARD`
- 单个 checker 最多记录 `50` 个违规
- `strict` 只是 `blocking` 的兼容别名，不是单独的 legacy 红线

执行原则：

- 行数单独不是 split trigger，必须和复杂度一起看
- 新增能力优先新增模块
- 不继续把新语义塞回旧大文件
- touched 文件在同一轮内就要满足门禁，不允许“先接功能，下轮再拆”

## 11. 当前仅剩问题

这一节只列当前仍未收口的问题，不重复列已经完成的实现项。

### 11.1 `real_opus` standing proof 仍受环境限制

当前状态：

- `integration lane`、CI workflow、脚本入口和 fail-closed 语义都已经收好
- standing-proof CI workflow 也已固定，可自动汇总最近 lane artifact
- 但如果运行环境里没有 `WORKFLOW_REVIEWER_API_KEY` 与 `WORKFLOW_REVIEWER_BASE_URL`，这条 lane 只能结构化 `SKIP`
- `SKIP` 结果只能作为“环境缺失”证据，不能替代 real-review pass 或 standing-proof 通过证据

这意味着：

- 现在缺的不是代码接线
- 而是真实 integration 环境下的持续常绿证明

### 11.2 `newsapp` benchmark 保持可选，不再阻塞主线 lane

当前状态：

- 平台已经把 `newsapp` 放回 benchmark 角色，而不是平台语义来源
- 当前仓库内保留 `newsapp` benchmark proof，作为外部复杂场景回归入口
- canonical always-on / integration 主链 lane 已切换到 repo-local fixture 组合，不再依赖 `newsapp`

还缺的部分：

- `newsapp` 级外部 benchmark 的长期环境可用性治理（可选）
- 当外部网关波动时的告警与恢复节奏固化（运营层，不是主链接线层）

### 11.3 剩余差距的本质

到当前阶段为止，剩余差距主要不是“底层功能没写”。

真正还差的是：

- 真实环境下的 standing proof 持续稳定度
- observability 的持续压缩（已经完成第一轮 `root_cause_bucket` 收口，后续继续减少 `runtime_error` / `unknown`）
- 把 proof 结果转成更稳定的运维节奏（告警、重试、值班手册）

### 11.4 AI 代码债治理主链已接通，剩余工作转为持续治理

当前状态：

- `scripts/snapshot_code_health.py`、`kodawari gate --ratchet --baseline <path>`、`scripts/update_code_health_baseline.py` 已接通同一套 `code_health.baseline.v1` 快照与单向 ratchet
- `build_contract_compliance_report()` 已把 `scope_drift`、`layer_boundary`、`source_of_truth_conflict`、`runtime_contract_scatter`、`duplication`、`import_rules`、`domain_source_of_truth` 聚合到统一治理面
- `module_ownership.schema.json`、`checker_import_rules.py`、`checker_duplication.py`、`load_domain_source_of_truth()`、ownership 注入 Opus / 实现前上下文都已落地
- `source_of_truth.py` 已承载领域级 `canonical_for` 映射，AI 在实现前可以拿到“唯一实现在哪里”的结构化提示

还缺的部分：

- 真实项目中的 `module_ownership.yaml` 仍需要持续补齐，并纳入团队日常维护，而不是只停留在 fixture 层
- ratchet baseline 需要继续跟随修复结果单向更新，并进入 CI / operator 的固定节奏
- 真实环境 standing proof 与运维手册仍是当前主线的收尾重点，不属于这批代码债治理任务本身

这意味着：

- 当前缺的已经不是 `WS-270 ~ WS-281` 这批功能本身，而是把它们变成长期、稳定、可执行的治理制度
- 这批任务已经把“重复代码检测 + 模块边界 + 领域 SoT + baseline ratchet”接成了同一条主链
- 本文档断点已推进到 `WS-281`；若继续追加同主题任务，建议从 `WS-282` 续编

## 12. 最终收口路线

### 12.1 当前还差什么

当前平台已经具备通用主线，但距离真正的产品化收口还差几件事：

- integration lane 已有固定 standing-proof 流水线，但真实环境仍需要持续跑稳（不是偶发通过）
- operator 侧需要把失败分型、告警和恢复动作进一步模板化
- observability 已新增 `root_cause_bucket` / `root_cause_bucket_distribution`，但还需要继续压缩宽泛异常与提升日志覆盖

### 12.2 最终收口顺序

建议保持这个顺序：

1. 持续观察 real review integration lane 的连续稳定性（standing-proof workflow 已固定，重点转向真环境常绿）
2. 固化 operator CI recipe 与排障模板（失败分型 -> 固定恢复动作）
3. 持续压缩 runtime-critical 的宽泛异常与日志盲区
4. 最后再扩更广 archetype，而不是提前扩面

### 12.3 双模型互审改造任务（WS-225 ~ WS-234）

本节用于收口当前双模型互审改造，范围固定为 `WS-225 ~ WS-234`。截至 `2026-03-30`，本轮任务已在主工作区完成实现、测试与文档回写，不再保留重复段或 `deferred` 残余项。

当前执行原则：

- 主证据优先使用 SSOT artifact：`engine context`、`review_bundle`、contract-first 工件、verify/gate 结果
- 机器能确定的事实先做 deterministic precheck，再交给 Opus 做解释与裁决
- `implementer_note` 已作为可选补充通道接通，但默认仍是非权威证据，不能覆盖 contract truth

验收回归：

- `pytest -q tests/test_autopilot_opus_gateway.py tests/test_execution_runtime_artifacts.py tests/test_autopilot_local_adapter.py tests/test_review_precheck.py tests/test_collaboration.py tests/test_autopilot_engine.py`
- 当前结果：`57 passed`

#### Phase 0：接通已有管线（WS-225 ~ WS-227）

`WS-225: 扩 Opus prompt 的 compact_context` `Done`

- 写入范围：`src/kodawari/autopilot/opus_gateway.py`
- 当前结果：Opus prompt 已接入 `requirements_excerpt`、`architecture_decisions`、`archetype`、`capabilities`、`surface`、`task_invariants`、`task_card_files`、`scope_risk_warnings`、`current_stage`、`effort_tier`
- 约束落地：`compact_context` 序列化预算软上限 `<= 8000` 字符；超限时优先裁剪旧决策并回写截断标记

`WS-226: 在 review_bundle 中补最小 contract excerpt` `Done`

- 写入范围：`src/kodawari/autopilot/review_bundle.py`
- 当前结果：`review_bundle` 已补入 `PRD_INTAKE.json`、`ARCHITECTURE_PLAN.json`、`TASK_GRAPH.json` 的最小 contract excerpt
- 约束落地：全部字段采用 best-effort defensive extraction；缺失时写空字符串或空数组，不因旧工件/缺工件抛异常

`WS-227: 强化 Opus prompt 规则` `Done`

- 写入范围：`src/kodawari/autopilot/opus_gateway.py`
- 当前结果：prompt 已明确要求先审全局 contract/context，再审局部实现；上下文不足必须显式报错；局部 pass 但全局冲突必须 reject

#### Phase 1：Deterministic Precheck（WS-228 ~ WS-230）

`WS-228: 新增 review_precheck.py` `Done`

- 写入范围：`src/kodawari/autopilot/review_precheck.py`
- 当前结果：已输出 `review.precheck.v1`，覆盖 `out_of_scope_files`、`missing_test_files`、`cross_boundary_files`、`verify_surface_gaps`、`invariant_conflicts`
- 职责边界：precheck 是执行后审计层，与执行前 `execution_guard` / implementation 阶段 scope 检查互补，不替代前置拦截

`WS-229: 把 deterministic findings 接入 review_bundle 和 Opus prompt` `Done`

- 写入范围：`src/kodawari/autopilot/local_adapter.py`、`src/kodawari/autopilot/review_bundle.py`、`src/kodawari/autopilot/opus_gateway.py`、`src/kodawari/schemas/runtime/review_bundle.schema.json`
- 当前结果：`_real_opus_review()` 会先计算 findings，再写入 `review_bundle["deterministic_findings"]`，同时 prompt 中新增 authoritative findings 段
- 行为约束：Opus 可以补充解释，但不能推翻 deterministic finding；本地 guard 也会对 findings 做最终兜底降级

`WS-230: 增加对抗测试` `Done`

- 写入范围：`tests/`
- 当前结果：已覆盖越界改动、缺测试、跨 boundary、上下文缺失、`precheck-pass != auto-approve` 等关键风险

#### Phase 2：Verdict 生效链路（WS-231 ~ WS-233）

`WS-231: 扩 peer_review_response 字段并保留新字段` `Done`

- 写入范围：`src/kodawari/schemas/runtime/peer_review_response.schema.json`、`src/kodawari/autopilot/opus_gateway.py`
- 当前结果：gateway 已保留 `global_consistency_verdict`、`local_implementation_verdict`、`deterministic_finding_responses`、`evidence_refs`

`WS-232: 让双 verdict 存入 ReviewFeedback` `Done`

- 写入范围：`src/kodawari/autopilot/collaboration.py`、`src/kodawari/autopilot/engine_review_mixin.py`
- 当前结果：`ReviewFeedback` 与 `review_history` 已使用具名字段持久化双 verdict 与证据引用，不使用 metadata bag

`WS-233: global fail 降级 approved` `Done`

- 写入范围：`src/kodawari/autopilot/collaboration.py`、`src/kodawari/autopilot/engine_review_mixin.py`
- 当前结果：`record_opus_review()` 会在 `global_consistency_verdict == FAIL` 时强制降级最终 `approved = False`
- 补充修正：peer review summary 现已读取最终 `review_feedback` 真值，而不是原始 review payload，避免出现“内部已 reject、摘要仍像 pass”的显示偏差

#### Phase 3：Implementer Note（WS-234）

`WS-234: implementer_note` `Done`

- 写入范围：`src/kodawari/autopilot/execution_artifacts.py`、`src/kodawari/autopilot/execution_codex_cli.py`、`src/kodawari/autopilot/execution_claude_code.py`、`src/kodawari/autopilot/review_bundle.py`、`src/kodawari/autopilot/opus_gateway.py`、`src/kodawari/schemas/runtime/execution_request.schema.json`、`src/kodawari/schemas/runtime/execution_result.schema.json`
- 当前结果：`implementer_note` 已作为可选字段贯通 `execution_request -> execution_result -> review_bundle -> Opus prompt`
- 使用约束：字段仅承载 `claimed_intent`、`claimed_invariants_preserved`、`claimed_risks`；在 `review_bundle` 中固定标记为 `non-authoritative`
- 结论：实现者补充说明通道已可用，但默认仍不替代 SSOT artifact，也不要求每次执行都必须生成

### 12.4 AI 代码债治理与边界硬约束迭代（WS-270 ~ WS-281）

本节用于收口“AI 代码债治理 / 模块边界硬约束 / 领域级 SoT / baseline ratchet”改造，范围固定为 `WS-270 ~ WS-281`。截至 `2026-04-02`，当前状态为 `Done`：代码接线、定向回归和文档回写都已完成。

本轮验收回归：

- `python -m pytest tests/test_source_of_truth_domain.py tests/test_code_health_snapshot.py tests/test_gate_duplication.py tests/test_import_rules_checker.py tests/test_gate_ratchet.py tests/test_contract_first_compliance.py tests/test_gate_cli.py tests/test_autopilot_opus_gateway.py tests/test_autopilot_engine.py -q`
- 当前结果：`60 passed`

当前可复用基础：

- `GateEngine.evaluate()` 已能稳定输出文件长度与函数指标
- `build_contract_compliance_report()` 已能稳定输出 `scope_drift`、`layer_boundary`、`layer_boundary_debt`、`source_of_truth_conflict`、`runtime_contract_scatter`、`review_evidence` 等 contract/compliance 真值
- `WS-225 ~ WS-234` 已经把 Opus 的全局上下文、deterministic precheck 与双 verdict 生效链路接通
- `source_of_truth.py` 已具备 canonicalization 基础能力
- `EngineContextMixin._build_implementation_context()` 已是实现前上下文的真实注入点，可承接 ownership / canonical module 提示

本轮方案固定吸收以下修正，不再回退：

- ratchet 不能只接 `GateEngine`，必须同时聚合 `GateEngine.evaluate()` 与 `build_contract_compliance_report()` 两条独立通道
- 领域级 SoT 的扩展路径固定为顶层 `src/kodawari/source_of_truth.py`，不写入 `autopilot` 子包
- 重复代码检测第一版优先使用 Python 生态内工具，不引入 Node.js 必选依赖；`pylint` 缺失时必须结构化降级而不是静默跳过
- `WS-276` 的 ownership/import checker 要承担统一入口角色，逐步替代现有 `check_layer_boundary_simple` 与 `check_layer_boundary_ast` 的主角色，而不是新增第三套平行规则
- `WS-278` 的实现前上下文注入点固定为 `EngineContextMixin._build_implementation_context()`，不再引用不存在的函数名

#### Phase A：Debt Ratchet（WS-270 ~ WS-272）

`WS-270: 代码健康基线快照` `Done`

- 写入范围：`scripts/snapshot_code_health.py`、`planning/code_health_baseline.json`
- 完成内容：
  - 调 `GateEngine.evaluate()` 提取 metrics 指标
  - 调 `build_contract_compliance_report()` 提取 compliance 指标
  - 调重复代码检测器提取 `total_duplicate_blocks`
  - 聚合为 `code_health.baseline.v1` JSON，并固定记录 `tool_versions`、`source_commit`、`generated_at`
- 基线指标至少包含：
  - `files_over_500_lines`
  - `files_over_1000_lines`
  - `functions_over_50_lines`
  - `functions_complexity_over_6`
  - `total_duplicate_blocks`
  - `layer_boundary_violations`
  - `layer_boundary_debt_files`
  - `sot_conflict_count`
  - `runtime_contract_scatter_conflicts`
- 约束：
  - snapshot 脚本必须同时聚合 metrics 通道与 compliance 通道，不能只扫 `GateEngine`
  - 若重复代码工具不可用，必须把“工具不可用”写成结构化字段，不能静默成功
- 验收：`python scripts/snapshot_code_health.py --src src/kodawari` 一条命令可稳定产出 baseline JSON
- 当前结果：已新增 `scripts/snapshot_code_health.py` 与 `kodawari.gate.code_health.collect_code_health_snapshot()`，输出 `code_health.baseline.v1`，统一聚合 metrics、compliance、duplication 与 `tool_versions/source_commit/generated_at`

`WS-271: Ratchet 比较器` `Done`

- 写入范围：`src/kodawari/gate/gate_ratchet.py`、`src/kodawari/cli/gate_cmd.py`
- 完成内容：
  - 新增 `compare_against_baseline(current: dict, baseline: dict) -> RatchetResult`
  - `kodawari gate` 新增 `--ratchet --baseline <path>` 参数
  - 对所有量化指标执行 “`current > baseline -> FAIL`” 比较，并显式输出回归项与回归幅度
- 约束：
  - 独立模块实现，不修改 `GateEngine` 核心逻辑
  - ratchet 比较的是聚合后的 metrics + compliance 数字，不是只比 gate 当前 payload
- 验收：人为让某项指标变差时，CLI 返回非零并打印具体回归项
- 当前结果：`kodawari gate` 已支持 `--ratchet --baseline <path>`，对聚合指标做回归比较，出现 regression 时返回非零并输出结构化 regression 列表

`WS-272: Baseline 单向更新` `Done`

- 写入范围：`scripts/update_code_health_baseline.py`
- 完成内容：比较 `current` 与 `baseline`，只在 `current < baseline` 时更新对应字段
- 约束：不允许任何指标自动上调；未知字段不得被清空
- 验收：修复一个超长函数后，`functions_complexity_over_6` 可从旧基线单向下降
- 当前结果：`scripts/update_code_health_baseline.py` 已落地，只在指标下降时更新 baseline，且保留未知字段与非回归指标

#### Phase B：重复代码检测（WS-273 ~ WS-274）

`WS-273: 集成 duplicate-code 检测器` `Done`

- 写入范围：`src/kodawari/gate/checker_duplication.py`、相关 tests
- 第一版实现策略：
  - 优先使用 `pylint` 的 duplicate-code 能力作为 Python 主路径
  - 若本地/CI 环境不存在兼容的 `pylint`，返回结构化 `WARN` / `SKIP` 与 `checker_unavailable` 证据
  - `jscpd` 仅保留为后续跨语言 enhancement，不作为当前必选依赖
- 完成内容：
  - 输出结构化 duplication payload：总 clone 数、按文件对分组、按规模排序
  - 接入 compliance report，默认 `WARN`，不做第一版 hard block
- 约束：
  - 文档与代码不得把它描述为“零额外依赖”；若要常态化进入 CI，再单独回写依赖策略
  - 结果必须可进入 baseline / ratchet，不能只打印控制台文本
- 验收：对已知存在明显重复片段的目录能稳定报出 structured payload
- 当前结果：`checker_duplication.py` 已集成 `pylint R0801` 主路径，不可用时返回结构化 `WARN/SKIP`；payload 已稳定输出 `duplicate_block_count`、`blocks`、`evidence`

`WS-274: 将 duplication 纳入 ratchet baseline` `Done`

- 写入范围：`scripts/snapshot_code_health.py`、`src/kodawari/gate/gate_ratchet.py`
- 完成内容：把 `total_duplicate_blocks` 纳入 baseline 与 ratchet 比较
- 验收：重复块数只能降不能升
- 当前结果：`total_duplicate_blocks` 已进入 `code_health.baseline.v1` 快照与 ratchet 比较链路，和其它质量指标统一收口

#### Phase C：模块边界硬约束（WS-275 ~ WS-278）

`WS-275: 定义 module_ownership 声明格式` `Done`

- 写入范围：`src/kodawari/schemas/module_ownership.schema.json`
- 完成内容：定义如下核心字段：
  - `owner`
  - `path`
  - `public_api`
  - `description`
  - `forbidden_imports`
  - `canonical_for`
- 关键设计：
  - `canonical_for` 就是领域级 SoT 的声明载体，不再单独造 registry
  - 需要附至少一个 `newsapp` 或 repo-local fixture 示例，证明 schema 可验证
- 验收：schema 校验可用，fixture 可过验证
- 当前结果：`src/kodawari/schemas/module_ownership.schema.json` 已落地，fixture 可通过 `jsonschema` 校验，字段涵盖 `owner/path/public_api/description/forbidden_imports/canonical_for`

`WS-276: 统一 Import / Ownership Checker` `Done`

- 写入范围：`src/kodawari/gate/checker_import_rules.py`、相关 tests
- 完成内容：
  - 读取 `module_ownership.yaml`
  - 对 `changed_files` 进行 AST import 解析
  - 检查 `forbidden_imports` 违规
  - 检查是否引用了非 `public_api` 的内部符号
  - 输出 `import_rule_violations`
- 角色约束：
  - 该 checker 是统一入口，逐步替代现有 `check_layer_boundary_simple` 与 `check_layer_boundary_ast` 的主角色
  - 当前已有的 route -> repository 规则要迁移成 ownership YAML 中的显式 forbidden rule
  - 旧 checker 第一阶段可保留，但要明确标记 `deprecated`
- 验收：如 `route.py` import service/repository 的内部 helper，gate 能返回结构化失败
- 当前结果：`checker_import_rules.py` 已支持 ownership manifest 读取、AST import 解析、`forbidden_imports` 与 non-public API 检查，并输出结构化 `import_rule_violations`

`WS-277: Ownership 注入 Opus review context` `Done`

- 写入范围：`src/kodawari/autopilot/opus_gateway.py`
- 完成内容：
  - 如果存在 ownership YAML，则提取 `changed_files` 相关模块的 `public_api`、`canonical_for`、`forbidden_imports`
  - 将其注入 Opus prompt 的 authoritative context 段
- 依赖：`WS-225` 已完成的 compact context 扩展链路
- 验收：review prompt 中可见 ownership 声明，且不依赖 implementer note 才能看到
- 当前结果：`opus_gateway.py` 的 authoritative compact context 已注入 `ownership_context`，包含 `public_api`、`forbidden_imports`、`canonical_for`

`WS-278: Ownership 注入实现前上下文` `Done`

- 写入范围：`src/kodawari/autopilot/engine_context_mixin.py`
- 实际注入点：`EngineContextMixin._build_implementation_context()`
- 完成内容：
  - 在实现前上下文中注入：
    - `canonical_for`
    - `public_api`
    - `forbidden_imports`
    - “唯一实现在哪里”的说明文本
  - 让 executor 在实现前就知道应该复用哪个 canonical module，而不是 review 时才发现重复实现
- 结论：这是本轮最重要的预防层，不是事后检测层
- 验收：executor 收到的上下文中包含 ownership / canonical module 信息
- 当前结果：`EngineContextMixin._build_implementation_context()` 已注入 `ownership_context` 与 `ownership_hints`，让 executor 在实现前就能看到 canonical module 与可复用 API

#### Phase D：领域级 SoT 扩展（WS-279 ~ WS-280）

`WS-279: source_of_truth.py 支持领域 SoT` `Done`

- 写入范围：`src/kodawari/source_of_truth.py`
- 完成内容：
  - 新增 `load_domain_source_of_truth(ownership_path: Path) -> dict[str, str]`
  - 从 ownership YAML 的 `canonical_for` 生成 `{业务语义 -> canonical 模块}` 映射
- 约束：
  - 本任务只做声明与归一化，不做语义推断
  - 路径固定为顶层包，不写进 `autopilot`
- 验收：能从 YAML 稳定得到诸如 `{"feed assembly logic": "feed_service", "ranking rules": "scoring_service"}` 的映射
- 当前结果：`load_domain_source_of_truth()` 已从 ownership manifest 的 `canonical_for` 生成 `{语义 -> canonical 模块}` 映射，并支持 JSON-backed YAML / YAML 两种读取路径

`WS-280: Domain SoT 检查接入 compliance` `Done`

- 写入范围：`src/kodawari/gate/checker_compliance.py`、相关 tests
- 完成内容：
  - 新增 `domain_source_of_truth` check
  - 初版行为为 `WARN`：当非 canonical 模块文件疑似重写 canonical 逻辑时，产生 evidence
  - 将该 evidence 一并传给 Opus review 做语义级补强
- 约束：
  - 初版不做“自动语义定罪”，只做保守暴露
  - 若证据不足，只能 `WARN`，不能硬判 `FAIL`
- 验收：compliance report 中出现 `domain_source_of_truth` check，并能暴露疑似重复实现风险
- 当前结果：`checker_compliance.py` 已新增 `domain_source_of_truth` check，初版以 `WARN + evidence` 暴露疑似 canonical drift，并随 compliance report 一并进入 review 证据面

#### Phase E：Architecture Fitness Ratchet（WS-281）

`WS-281: 现有 fitness checks 纳入 ratchet` `Done`

- 写入范围：`scripts/snapshot_code_health.py`、`src/kodawari/gate/gate_ratchet.py`
- 完成内容：把以下指标纳入 baseline 与 ratchet：
  - `layer_boundary_violations`
  - `layer_boundary_debt_files`
  - `sot_conflict_count`
  - `runtime_contract_scatter_conflicts`
  - `import_rule_violations`
  - `total_duplicate_blocks`
- 验收：现有 architecture / compliance check 从“一次性检查”升级为“持续只降不升”的 fitness ratchet
- 当前结果：ratchet 快照已纳入 `layer_boundary_violations`、`layer_boundary_debt_files`、`sot_conflict_count`、`runtime_contract_scatter_conflicts`、`import_rule_violations`、`domain_sot_conflict_count`、`total_duplicate_blocks`

#### 本轮执行顺序

推荐依赖顺序固定为：

1. `Phase A: WS-270 -> WS-271 -> WS-272`
2. `Phase C: WS-275 -> WS-276 -> WS-277 -> WS-278`
3. `Phase B: WS-273 -> WS-274`
4. `Phase D: WS-279 -> WS-280`
5. `Phase E: WS-281`

说明：

- `Phase A` 与 `Phase C` 可并行启动，但 `WS-281` 必须等待 `A + B + C`
- `WS-277` 依赖已经完成的 `WS-225`
- `WS-278` 的注入点已明确为 `_build_implementation_context()`，不需要再做路径探索

#### 本轮完成后的实际效果

完成 `WS-270 ~ WS-281` 后，平台现在已经具备以下能力：

- 能对“代码质量有没有比昨天更差”给出结构化、可回归的 ratchet 结论
- 能把现有 `scope / layer / source_of_truth / runtime_contract_scatter` 检查接入长期 baseline，而不是只做一次性体检
- 能通过 `module_ownership` 把模块边界、public API、canonical module 声明同时提供给实现模型和评审模型
- 能把“AI 代码冗余”和“边界越界”从 review 后发现，前移为实现前预防 + review 时复核
- 能让“唯一真值”不只存在于存储层，也开始覆盖领域逻辑层

本轮不承诺的事项：

- 不自动消灭历史存量技术债
- 不承诺第一版就做跨语言语义级 clone detection
- 不承诺只靠机器规则就完全替代人工架构判断

本轮的目标不是“自动重构一切”，而是先让新增债务进入可观测、可阻断、可单向收紧的治理闭环

### 12.5 watercare-app canary 稳定化迭代（WS-282 ~ WS-295）

本节承接 `WS-281` 之后的真实实战修复，范围固定为 `WS-282 ~ WS-295`。这组任务不是凭空扩展功能，而是直接来源于 `watercare-app` 上使用 `kodawari` 跑真实需求时暴露出的 canary 级问题证据。

证据来源固定为 `watercare-app/planning/medication-adherence-summary/` 下的 runtime artifact，核心事实包括：

- `.execution_result.json` 已证明 `claude_code` backend 可以真实改动目标文件，并正确落到 `app/schemas.py`、`tests/test_api.py`
- `.review_result.json` 暴露 review 的 `changed_files` 被整仓 `git diff` 污染，出现跨项目假 scope drift
- `.verify_report.json` 与 `.autopilot_rounds.jsonl` 暴露 verify 会错误放大到整个 `tests/test_api.py`，而不是 task 级验证目标
- `.review_evidence.json` 暴露 `claude_code` 路径下仍在要求不适用的 Codex/Opus evidence，contract 不一致
- `.run_stderr.log` 暴露 Windows `UnicodeDecodeError` 与 `RevisionConflictError`
- `work` 运行时的 preflight dirty files 暴露了 `newsapp/**`、`kodawari/**` 等与 `watercare-app` 无关的噪音

结论固定为：

- `WS-270 ~ WS-281` 已经解决“长期治理能力”问题
- `WS-282 ~ WS-295` 要解决的是“真实项目 canary 路径能否稳定闭环”的执行真值问题
- 这组任务的验收标准不是单测数量，而是 `watercare-app` 上 `plan -> work -> review -> status` 的真链路不再被伪信号污染

#### 2026-04-02 进展备注

基于最新一轮 `watercare-app` 实跑证据，当前状态更新如下：

- `WS-282 / WS-283` 可视为已有效落地：
  - `review changed_files.source` 已切到 `state.changed_files`
  - review 范围已收敛到 `app/schemas.py`、`tests/test_api.py`
  - `scope_drift` 当前为 `PASS`
- `WS-290` 的 phase-1 已落地：
  - `status` 已暴露 `artifact_truth`
  - 当前可见的 authoritative truth 至少包括 `authoritative_changed_files`、`review_result`、`review_evidence`、`verify_report`
- 当前仍未收口的 blocker 固定为：
  - `WS-284 verify narrowing`：verify 仍在跑 `pytest -q tests/test_api.py`，实跑结果仍是 `21 failed, 2 passed`
  - `WS-291 review evidence contract alignment`：`review_evidence` 仍报 `Missing Codex self-review evidence` / `Missing Opus peer-review evidence`
  - `work` 仍会被 `CLAUDE_CODE_CHANGED_FILES_MISSING` 阻塞

这意味着：

- changed-files 真值和 review scope 漂移问题已经明显收敛
- artifact authority order 已开始对外可见
- 但 verify target、review evidence contract 与 rerun/resume 稳定性仍是 watercare canary 的当前主阻塞项

#### 本轮吸收的修正

与最初的 `WS-282 ~ WS-289` 草案相比，本轮固定吸收以下修正，不再回退：

- 新增 `WS-290`：先收口 artifact 真值冲突与 stale artifact reset，再继续修 review / verify
- 新增 `WS-291`：把 `review_evidence` contract 与实际 backend 对齐，避免 `claude_code` 路径被错误要求 Codex/Opus evidence
- 新增 `WS-293`：显式处理 inherited gate debt，避免历史债务被误算成当前 task regression
- 新增 `WS-294`：把 preflight dirty-file 观察范围收窄到 `--project-root`，切断 `newsapp/**` 噪音
- 新增 `WS-295`：修 loop finish / max-cycles bookkeeping，避免 `VERIFY pass` 后仍被 `OPUS_REVIEW max_cycles` 假阻塞

#### 任务清单

`WS-282: project-root scoped changed-files truth`

- 目标：让 task delta / review changed files 严格受 `--project-root` 约束，不再把工作区其它项目的 dirty diff 混进当前 feature
- 修复点：
  - changed-files truth 解析必须优先认 task 自己的 execution truth
  - `git diff` fallback 必须按 `project_root` 过滤
  - `watercare-app` review 不再出现 `newsapp/**`、`kodawari/**` 泄漏
- 验收：`kodawari review --project-root e:/code_rebuild/watercare-app` 输出的 changed files 只包含 `watercare-app` 范围内文件

`WS-283: review truth precedence reorder`

- 目标：重排 review 阶段 `changed_files` 真值优先级，避免 review 被 worktree baseline 或全仓 `git diff` 抢权
- 修复后的固定顺序：
  1. `.execution_result.json.changed_files`
  2. `state.task_delta_changed_files`
  3. 显式 `--changed-file`
  4. `project_root` 过滤后的 `git diff`
- 验收：只要 execution artifact 已给出 changed files，review 就不得再退回整仓 diff

`WS-284: task-granularity verify targeting`

- 目标：把 verify 从“文件级过宽回归”收窄到“task 级验证目标”，避免一个 task 只改两个 schema/tests，却跑整份测试文件导致假失败
- 修复点：
  - runtime 侧 verify changed files 解析要优先使用 task delta 真值
  - planning 侧 verify recipe 也要收窄，不能继续把整个文件当默认 target
- 验收：`watercare-app` 的 medication adherence task 只运行与该 task 相关的 verify recipe，不再误伤不相关 case

`WS-285: rerun / resume semantics for existing valid task changes`

- 目标：允许“已有合法变更的 task”进行稳定 rerun / resume，而不是一看到工作树已有变更就误判 `CHANGED_FILES_MISSING`
- 修复点：
  - 若已有 `.execution_result.json` 与当前 task truth 一致，应允许恢复而不是 fail-closed
  - 要区分“当前 task 的已知合法改动”和“外部未知污染”
- 验收：同一 task 已落地修改后再次执行 `work`，可以恢复或复核，而不是直接因为 changed-files 推断失败而阻塞

`WS-286: executor UX / productization`

- 目标：把 canary 路径中 executor 相关报错、提示和恢复动作产品化，避免 operator 只能靠读源码猜下一步
- 修复点：
  - 错误码、恢复建议、当前 backend 真值要在 CLI 层显式展示
  - 对 `claude_code` / `codex_cli` 的差异行为给出结构化提示
- 验收：当执行失败或被 guard/gate 阻断时，CLI 输出能直接告诉 operator 下一步动作

`WS-287: Windows runtime stability`

- 目标：收口真实 Windows 执行环境中的编码与状态写入稳定性问题
- 修复点：
  - 消除 `.run_stderr.log` 已暴露的 `UnicodeDecodeError: 'gbk'`
  - 收口 `RevisionConflictError` 的并发/重入写入冲突
- 验收：在 Windows 本地重复跑 canary，不再出现上述两类 runtime 错误

`WS-288: planning relevance narrowing`

- 目标：让 planning / status / review 只读取与当前 feature 真正相关的 artifact，不再被历史 planning 噪音放大
- 修复点：
  - relevance 计算要基于当前 feature/task 关联文件，而不是目录内所有遗留 artifact
  - 这项必须早于 executor UX 收口，否则 operator 看到的提示仍会被旧 artifact 污染
- 验收：`status` 与 `review` 只引用当前 feature 相关 artifact，不把旧轮次无关证据当成本轮真值

`WS-289: full watercare canary replay`

- 目标：在上述修复完成后，用 `watercare-app` 重新跑一轮完整 canary，验证问题不是只在单测里消失，而是真实链路恢复稳定
- 验收链路：
  - `kodawari plan`
  - `kodawari work`
  - `kodawari review`
  - `kodawari status`
- 完成定义：`watercare-app` 真链路不再出现假 scope drift、verify 扩散、artifact 污染、review evidence 错配与 loop 假阻塞

`WS-290: artifact truth reconciliation / stale artifact reset`

- 目标：先统一 planning 目录下多个 artifact 的 authority order，再决定谁该被读取、谁该失效
- 修复点：
  - 明确 `.execution_result.json`、`.review_result.json`、`.review_evidence.json`、`.verify_report.json`、`.gate_result.json`、`.workflow_chain.json`、`.status_snapshot.json`、`.autopilot_state.json` 的真值优先级
  - 当上游真值变化时，下游 stale artifact 必须被重置或标记失效，不能继续污染 `status/review/verify`
- 验收：同一 planning 目录中存在旧 artifact 时，系统仍只消费当前有效 truth，不再被历史结果误导

`WS-291: review evidence contract alignment`

- 目标：让 `review_evidence` 的 contract 与实际 backend / review mode 对齐
- 修复点：
  - `claude_code` backend 不应继续被要求 `codex_cli` 证据
  - simulated review、real Opus review、backend-local review 的证据要求要各自匹配
- 验收：`watercare-app` 走 `claude_code` 路径时，`.review_evidence.json` 不再出现“不存在的 Codex/Opus 证据缺失”假告警

`WS-293: inherited gate debt handling`

- 目标：把历史已知 gate debt 与“本轮 task 新引入 regression”分开，避免当前 task 为历史遗留问题背锅
- 典型场景：`app/main.py` 之类的既有债务文件不应被自动算成当前 feature 的新增违规
- 验收：gate / review / status 能明确区分 inherited debt 与 task-introduced regression

`WS-294: worktree preflight scope narrowing`

- 目标：把 preflight dirty-files 检查限定到当前 `project_root`，不再读取 monorepo 其它项目的脏文件噪音
- 验收：`work` 前置检查在 `watercare-app` 下只显示 `watercare-app` 内 dirty files，不再带出 `newsapp/**` 或 `kodawari/**`

`WS-295: loop finish / max-cycles bookkeeping`

- 目标：修复 autopilot loop 在后段阶段已 pass 时，仍被旧的 `OPUS_REVIEW max_cycles` 状态拖回阻塞的问题
- 修复点：
  - stage finish 条件要以当前 authoritative stage result 为准
  - `VERIFY pass`、`RULES_GATE pass` 后不得被旧 review bookkeeping 误覆盖
- 验收：真实 canary 中若后段已通过，loop 能正确收口，不再出现“实际上已完成、状态上仍 blocked”的假象

#### 修正后的执行顺序

本轮执行顺序固定为：

1. `WS-282: project-root scoped changed-files truth`
2. `WS-283: review truth precedence reorder`
3. `WS-290: artifact truth reconciliation / stale artifact reset`
4. `WS-284: task-granularity verify targeting`
5. `WS-291: review evidence contract alignment`
6. `WS-285: rerun / resume semantics for existing valid task changes`
7. `WS-294: worktree preflight scope narrowing`
8. `WS-288: planning relevance narrowing`
9. `WS-293: inherited gate debt handling`
10. `WS-287: Windows runtime stability`
11. `WS-295: loop finish / max-cycles bookkeeping`
12. `WS-286: executor UX / productization`
13. `WS-289: full watercare canary replay`

顺序解释：

- `WS-282 + WS-283` 先修 changed-files 真值，否则后面的 review / verify / status 都会继续吃错输入
- `WS-290` 必须提前，因为 stale artifact 不先清，后面的任何 canary 复跑都可能是伪修复
- `WS-284` 与 `WS-291` 分别收口 verify 真值和 review evidence contract，是 canary 主链的两个核心假失败来源
- `WS-285`、`WS-294`、`WS-288`、`WS-293` 负责把 rerun、preflight、planning relevance 与 inherited debt 这些“真实 operator 会遇到的噪音源”逐个切掉
- `WS-287` 与 `WS-295` 收口平台稳定性和 loop 收尾问题
- `WS-286` 放在后段，是因为 UX 必须建立在真值已经正确的前提上
- `WS-289` 永远最后执行，作为整轮 watercare canary 实战验收，不得提前“以单测通过代替”

#### 本轮完成后的目标效果

完成 `WS-282 ~ WS-295` 后，`kodawari` 在真实项目 canary 上应达到以下状态：

- `changed_files`、`review`、`verify`、`status`、`loop` 共享同一套 project-root scoped truth，不再各自读不同的伪真值
- planning 目录中即使存在多轮历史 artifact，也能被 authority order 与 stale reset 机制正确隔离
- `watercare-app` 这类真实项目的 canary 路径可以稳定复跑，不会因为 monorepo 噪音、旧 artifact 或 inherited debt 被假阻塞
- operator 在 Windows 本地环境下可以看到更稳定的执行结果与更明确的恢复动作
- 后续继续做 `newsapp` 或其它真实项目扩展时，平台面对的将是“真实产品问题”，而不再是“工作流自己制造的伪噪音”

## 13. 是否需要图或示意

当前 3 份中文文档已经能完整承载产品语义与 operator 信息，短期内不需要“靠流程图才能读懂主线”。

当前主仓已经补回中文架构流程图与 Mermaid 源文件：

- `docs/三、中文架构流程图.md`：中文架构流程图与读图说明
- `docs/diagrams/autopilot_flow.mmd`：与中文文档同步的 Mermaid 源文件

如果后续还要继续提升新同事上手速度，最值得补的下一张图是：

- `autopilot` 单入口全链时序图：展示 `autopilot -> planning truth -> runtime truth -> decision bridge -> status` 的关系

这张图属于“提升理解效率”，不是当前 operator 文档可用性的阻塞项。

## 14. 历史文档映射

下列历史主题已并入本文件：

- operator 运行说明
- generic runtime proof lane
- `codex_cli` 使用要求
- real review 环境变量语义
- `status` 字段解释
- blocked-path 错误码与排障方式
- 代码质量红线与最终收口路线

完整原文仍保留在归档目录中，不做信息删除。

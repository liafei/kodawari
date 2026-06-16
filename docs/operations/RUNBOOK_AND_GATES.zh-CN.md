# 运行操作、门禁规则与后续路线

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

- 当前缺的已经不是这批代码债治理功能本身，而是把它们变成长期、稳定、可执行的治理制度
- 这批任务已经把“重复代码检测 + 模块边界 + 领域 SoT + baseline ratchet”接成了同一条主链

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

## 13. 是否需要图或示意

当前 3 份中文文档已经能完整承载产品语义与 operator 信息，短期内不需要“靠流程图才能读懂主线”。

当前主仓已经补回中文架构流程图与 Mermaid 源文件：

- `docs/architecture/ARCHITECTURE_DIAGRAM.zh-CN.md`：中文架构流程图与读图说明
- `docs/architecture/diagrams/autopilot_flow.mmd`：与中文文档同步的 Mermaid 源文件

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

# Unified Workflow Escalation Design

> 目标：把所有"LLM 推理能力边界 + 任务设计边界 + 外部依赖暂态失效"类的失败统一接到 `kodawari decide` 决策层。
> 不再每碰一种失败 case 就单独打补丁。
>
> 当前现状（截至本提案）：只有 `IMPLEMENT 阶段 + GATE_BLOCKED + gate_complexity` 一个具体子路径接到了 decide；其它 ~25 个失败 case 都直接 `BLOCKED` 退出。

---

## 1. 失败点全集

按 workflow 生命周期 11 个 phase 列出，每行：`failure_code | 触发点 | 当前行为 | 推荐 escalation 分类`

### Phase A: Input

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `PRD_FILE_NOT_FOUND` | resolve_autopilot_prd_path | exit | **不上报**（用户输入错） |
| `TASK_CARD_PATH_INVALID` | engine.__init__ | exit | **不上报** |

### Phase B: Planning Context Collection

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `CONTEXT_COLLECTION_IO` | collect_planning_context | exit | **不上报** |
| `TASK_INPUT_INFEASIBLE` | feasibility 判定 missing surfaces | early_exit | **上报: PLANNING_INFEASIBLE_PREREQ** — 让 Planner 决定是补前置任务还是修 PRD |

### Phase C: Planning Conversation Loop（**当前实战卡死的主要区**）

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `PLANNER_HTTP_502_PERSISTENT` | planner round 502 × 3 | exit | **上报: PLANNING_TRANSPORT_FAIL** — 提示换 transport |
| `PLANNER_TIMEOUT` | planner wall-clock | exit | **上报: PLANNING_MODEL_TOO_SLOW** — 提示换模型 / 缩 context |
| `PLANNER_OUTPUT_TRUNCATED_EMPTY` | finish_reason=length 输出空 | exit | **上报: PLANNING_OUTPUT_OVERSIZE** — 提示加大 max_tokens 或拆任务 |
| `PLANNER_REVIEWER_DEADLOCK` | reviewer findings 7 round 不收敛 | exit | **上报: PLANNING_TASK_TOO_LARGE** — 让 Planner 出"拆 feature"方案 ← **本次实战的** |
| `PLANNER_STUBBORN_ROUND_LIMIT` | planner 2 round 不修 findings | exit | **上报: PLANNING_TASK_TOO_LARGE** 同上 |
| `PLANNING_CONTEXT_OVERSIZE` | 502 / context too big | exit | **上报: PLANNING_CONTEXT_PRESSURE** — 提示缩 PRD 或拆任务 |

### Phase D: Planning Finalization

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `TASK_CARD_SCHEMA_INVALID` | contract_first_schema 校验 | exit | **不上报**（planner 出错代码 bug，开发者修） |

### Phase E: IMPLEMENT 阶段

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `EXECUTOR_STALLED_NO_WRITE_PROGRESS` | 模型反复读不写 → recovery 耗尽 | exit | **上报: EXECUTOR_STUCK** — 换模型 / 拆任务 / 用户写代码 |
| `EXECUTOR_STALLED_REDUNDANT_READS` | 同 path/offset 重复读 | recovery → exit | **上报: EXECUTOR_STUCK** 同上 |
| `EXECUTOR_STALLED_FRAGMENTED_READS` | 同文件被切多窗口 | recovery → exit | **上报: EXECUTOR_STUCK** 同上 |
| `EXECUTOR_STALLED_REPEATED_SEARCH` | search 同 query 反复 | recovery → exit | **上报: EXECUTOR_STUCK** 同上 |
| `EXECUTOR_STALLED_PATCH_PLAN_REQUIRED` | patch_plan_required mode 但 LLM 不走 | recovery → exit | **上报: EXECUTOR_STUCK** 同上 |
| `EXECUTOR_STALLED_PATCH_FAILURES` | patch apply 反复失败 | recovery → exit | **上报: EXECUTOR_PATCH_BROKEN** — task card precondition 错 / 重新出 plan |
| `EXECUTOR_STALLED_BUDGET_PRESSURE` | token budget 超 | recovery → exit | **上报: EXECUTOR_BUDGET** — 缩 task / 换便宜模型 |
| `MAX_TOOL_ITERATIONS` | 60 次 tool call 满 | exit | **上报: EXECUTOR_STUCK** 同上 |
| `TASK_BLOCKED_BY_PRECONDITION` | declare_task_infeasible 显式调 | escalate（已有） | **上报: EXECUTOR_PRECONDITION_MISSING** — Planner 插前置任务 |
| `INVALID_TOOL_CALL` | LLM 返回非法 JSON arg | exit | **上报: EXECUTOR_TRANSPORT** — gateway/model bug |
| `PATCH_PLAN_MISSING` | task_card.patch_plan 缺失 | exit | **上报: TASK_CARD_DESIGN_BUG** — Planner 重出 |

### Phase F: VERIFY 阶段

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `VERIFY_FAILED` | pytest 失败 | recovery（pytest_recovery） | **不上报**（recovery 已覆盖；耗尽后走 EXECUTOR_STUCK） |
| `VERIFY_FAILED_RETRYABLE` | pytest 暂时性失败 | retry | **不上报** |
| `VERIFY_HANG_TIMEOUT` | pytest 卡死 | exit | **上报: VERIFY_HANG** — test 卡死，让用户介入 |
| `PYTEST_COLLECTION_NAMEERROR` | import error | recovery（已覆盖） | **不上报** |

### Phase G: GATE 阶段（**完整覆盖所有 redline 类型，当前仅 1/8 接通**）

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `GATE_BLOCKED:gate_complexity` | function_metrics complexity | escalate（已有） | 保持: user_redesign 重构 |
| `GATE_BLOCKED:gate_nesting` | nesting > 4 | exit | **上报: GATE_REFACTOR_NEEDED** 类同 complexity |
| `GATE_BLOCKED:file_length` | 文件 > 1500 行 | exit | **上报: GATE_FILE_SPLIT_NEEDED** — 拆 module |
| `GATE_BLOCKED:file_complexity_sum` | 文件复杂度总和超 | exit | **上报: GATE_FILE_SPLIT_NEEDED** 同上 |
| `GATE_BLOCKED:scope_contract` | 改了 read_only_files | exit | **上报: TASK_CARD_FILES_TO_CHANGE_WRONG** — Planner 调 task card |
| `GATE_BLOCKED:import_rules` | 跨包不当依赖 | exit | **上报: ARCHITECTURE_VIOLATION** — Planner 改设计 |
| `GATE_BLOCKED:duplication` | 重复代码 | exit | **上报: GATE_REFACTOR_NEEDED** 同 complexity |
| `GATE_BLOCKED:semantics` | 语义错误 | exit | **上报: GATE_REFACTOR_NEEDED** 同上 |
| `GATE_BLOCKED:compliance` | 合规违反 | exit | **上报: COMPLIANCE_BLOCK** — 必须用户决定 |

### Phase H: REVIEW 阶段

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `REVIEW_BLOCKED_PERSISTENT` | reviewer 持续 blocking | recovery 耗尽 | **上报: EXECUTOR_STUCK**（同 IMPLEMENT 类） |
| `REVIEWER_UNAVAILABLE` | reviewer gateway 挂 | degraded | **不上报**（自动降级跑） |

### Phase I: Recovery 自身失败

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `RECOVERY_ATTEMPTS_EXHAUSTED` | per-signature 2 attempts 用完 | exit | **上报: RECOVERY_EXHAUSTED** — 当前 gate_complexity 已覆盖；扩到全部 stall 类 |
| `RECOVERY_TOTAL_ATTEMPTS_EXHAUSTED` | total cap 8 用完 | exit | **上报: RECOVERY_EXHAUSTED** 同上 |
| `RECOVERY_SYNTHESIZER_TIMEOUT` | synthesizer LLM timeout | exit | **不上报**（synthesizer 默认 off） |

### Phase J: Task Cycle 推进

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `TASK_BLOCKED:writable_file_mismatch` | task A 需改的文件不在 files_to_change | exit | **上报: TASK_CARD_FILES_TO_CHANGE_WRONG**（同 Phase G scope_contract） |
| `task_cycle_blocked` | 任一子 task 阻塞 | exit | **上报: SUBTASK_BLOCKED** — 让 Planner 决定怎么办 |

### Phase K: Delivery / Release

| failure_code | 触发点 | 当前 | 建议 |
|---|---|---|---|
| `RELEASE_DECISION_REQUIRED` | release_gate 触发人工决策 | wait | **不上报**（已有 decision_request 机制） |
| `WORKFLOW_CHAIN_ARTIFACT_MISSING` | 关键工件缺 | exit | **不上报**（开发者修） |

---

## 2. Escalation 分类收敛

把上面散列的失败码归到 **8 个 escalation kind**：

| kind | 触发 failure codes | decide 出的方案模板 |
|------|---|---|
| `EXECUTOR_STUCK` | EXECUTOR_STALLED_*（除 PATCH_FAILURES）/ MAX_TOOL_ITERATIONS / RECOVERY_EXHAUSTED / REVIEW_BLOCKED_PERSISTENT | 1) 换模型 2) 缩 task scope 3) 用户接管手写 4) skip task |
| `EXECUTOR_PATCH_BROKEN` | EXECUTOR_STALLED_PATCH_FAILURES / PATCH_PLAN_MISSING | 1) 重出 plan 2) 调 task_card.patch_plan 3) skip |
| `EXECUTOR_PRECONDITION_MISSING` | TASK_BLOCKED_BY_PRECONDITION | 1) 插前置任务 2) 改 task_card requires 3) skip |
| `GATE_REFACTOR_NEEDED` | gate_complexity / gate_nesting / duplication / semantics | 1) 重构方案 N 选 1（已有 complexity 路径）2) 调阈值 3) skip |
| `GATE_FILE_SPLIT_NEEDED` | file_length / file_complexity_sum | 1) 拆 module 方案 2) 调阈值 3) skip |
| `TASK_CARD_DESIGN_BUG` | scope_contract / writable_file_mismatch / import_rules / TASK_CARD_FILES_TO_CHANGE_WRONG | 1) Planner 重出 task card 2) 用户手动改 3) skip |
| `PLANNING_TASK_TOO_LARGE` | PLANNER_REVIEWER_DEADLOCK / PLANNER_STUBBORN_ROUND_LIMIT / PLANNING_INFEASIBLE_PREREQ | 1) **拆 feature 成 N 个 sub-features** 2) 缩 PRD scope 3) abort |
| `PLANNING_ENV_FAIL` | PLANNER_HTTP_502 / PLANNER_TIMEOUT / PLANNER_OUTPUT_TRUNCATED / PLANNING_CONTEXT_OVERSIZE | 1) 换 transport/model 2) 缩 context max chars 3) abort |
| `VERIFY_HANG` | pytest 卡死 | 1) kill + 标 skip 2) 改 verify_cmd 3) 用户检查 |
| `COMPLIANCE_BLOCK` | gate_compliance | 1) 必须用户决定 / 不自动 |

10 种 kind 覆盖所有上面 ~25 个 failure code。

---

## 3. 统一 escalation 协议

### 3.1 统一文件名

替代当前 `.executor_redesign_*` 单一文件，改为按 phase 命名：

| 阶段 | request 文件 | response 文件 | context 文件 |
|---|---|---|---|
| Planning | `.planning_decision_request.json` | `.planning_decision_response.json` | `.planning_decision_context.json` |
| Executor | `.executor_decision_request.json` | `.executor_decision_response.json` | `.executor_decision_context.json` |
| Gate | `.gate_decision_request.json` | `.gate_decision_response.json` | `.gate_decision_context.json` |
| Review | （走 Executor 路径） | | |

每个 request 包含统一 schema：
```json
{
  "schema_version": "workflow.decision_request.v1",
  "escalation_kind": "PLANNING_TASK_TOO_LARGE",  // 10 种之一
  "failure_code": "PLANNER_REVIEWER_DEADLOCK",   // 原始 failure code
  "phase": "planning",
  "task_id": "T?",                                // executor 类才有
  "feature": "social_dual_page_split",
  "failure_summary": "<message>",
  "context": {                                    // kind-specific extras
    "round_count": 7,
    "blocking_findings_history": [2, 3, 7, 1, 3, 6],
    "last_plan_tasks_count": 6,
    ...
  },
  "completed_task_ids": [...],
  "escalation_count": 1
}
```

### 3.2 统一 escalation count（防无限循环）

每个 phase 独立计数：
- `.planning_decision_context.json` 最多 2 次
- `.executor_decision_context.json` 最多 2 次
- `.gate_decision_context.json` 最多 2 次

任一超 2 → 转 final BLOCKED。

### 3.3 统一 decide 命令

```bash
kodawari decide --planning-dir <dir>
# Auto-detects which *_decision_request.json exists, picks the right
# escalation_kind, calls Planner with kind-specific prompt template
```

decide 命令内部按 kind 路由：

```python
if kind == "PLANNING_TASK_TOO_LARGE":
    # Ask Planner: "split this feature into 3 sub-features"
elif kind == "EXECUTOR_STUCK":
    # Ask Planner: "give 3 alternatives to unstuck this task"
elif kind == "GATE_REFACTOR_NEEDED":
    # 已有 (current gate_complexity path)
elif kind == "GATE_FILE_SPLIT_NEEDED":
    # Ask Planner: "split this file into 2-3 modules"
elif kind == "TASK_CARD_DESIGN_BUG":
    # Ask Planner: "correct task_card files_to_change / requires"
...
```

每个 kind 一个独立的 Planner prompt 模板。

### 3.4 统一 resume 协议

autopilot 启动时按顺序检查（任一存在就处理）：
1. `.planning_decision_response.json` → 应用拆分 feature / 换模型 / 缩 context
2. `.executor_decision_response.json` → 应用换模型 / skip task / 注入 must_fix
3. `.gate_decision_response.json` → 应用重构方案 / 拆 module / 调阈值

应用完立刻标记 `consumed_at`，避免重复消费。

---

## 4. 改动清单（draft，等 agent 评审后定稿）

### 4.1 新建 / 改造

| 文件 | 改动 | LOC 估 |
|---|---|---|
| `autopilot/escalation/__init__.py` | **新模块** — 统一 escalation 入口 | 30 |
| `autopilot/escalation/kinds.py` | **新** — 10 个 EscalationKind 枚举 + classifier | 100 |
| `autopilot/escalation/handler.py` | **新** — 统一 write_decision_request / read_decision_response / consume markers | 250 |
| `autopilot/escalation/planner_prompts.py` | **新** — 10 个 kind 各自的 Planner prompt template | 200 |
| `cli/runtime/decide_cmd.py` | 重写 — 按 kind 路由到对应 prompt template + 处理 4 种 phase 的 response | +200 / -150 |
| `autopilot/engine/engine_recovery_mixin.py` | 扩展 — 所有 RECOVERY_*_EXHAUSTED 触发 EXECUTOR_STUCK escalation | +60 |
| `autopilot/planning/planning_orchestrator.py` | 扩展 — escalation_required 时改写 .planning_decision_request.json 不直接退出 | +100 |
| `autopilot/engine/engine_session_mixin.py` | 扩展 — 启动时 detect 所有 3 种 decision_response.json | +40 |
| `cli/runtime/autopilot_cmd.py` | 扩展 — resume 时应用拆分 feature / 换模型逻辑 | +80 |
| `cli/runtime/gate_config_cmd.py` | 暴露阈值调整 API（给 GATE escalation 用） | +20 |
| `gui/redesign_chooser.py` | 改 — 显示 kind-specific UI（拆分用 tree 视图 vs 重构用 list） | +50 |

### 4.2 删除 / 兼容

- `.executor_redesign_request.json` → 兼容读取，但内部转 `.executor_decision_request.json` 统一 schema
- `is_gate_complexity_exhausted()` → 拆成 `is_escalatable_failure()` 多 kind dispatcher
- 旧 `escalation_handler.py` → 改名 `legacy_escalation_compat.py` 仅做 schema 翻译

### 4.3 数据流改动

- `engine_recovery_mixin._maybe_prepare_executor_recovery` 的 escalation 分支 → 走新 `escalation.handler.maybe_escalate(failure_event, phase="implement")`
- `planning_orchestrator` 末尾 escalation_required 分支 → 走新 `escalation.handler.maybe_escalate(planning_diagnostics, phase="planning")`
- `gate_engine` 触发 GATE_BLOCKED 后 → 加 `escalation.handler.maybe_escalate(gate_check, phase="gate")` hook

### 4.4 测试

| 类型 | 范围 | 测试数 |
|---|---|---|
| 单元测试 | 10 个 EscalationKind 的 classify + 各自 prompt 生成 + response 解析 | 30 |
| 单元测试 | 4 种 phase 的 write/read decision file + escalation count | 12 |
| 集成测试 | PLANNING_TASK_TOO_LARGE 全链路：deadlock → escalate → decide → split feature → resume | 1 |
| 集成测试 | EXECUTOR_STUCK 全链路 | 1 |
| 集成测试 | GATE_FILE_SPLIT_NEEDED 全链路 | 1 |
| 回归 | 现有 ReadCache / opus_tool_use 测试不破坏 | 141 |

总计：~45 个新测试 + 141 回归。

### 4.5 实施顺序（不破坏现有）

1. **Step 1**：建 `escalation/` 模块 + `EscalationKind` + 单元测试（30 个）
2. **Step 2**：实现 phase=executor 的 escalation（与现有 gate_complexity 路径并行，新代码 + 旧代码兼容）
3. **Step 3**：实现 phase=planning 的 escalation（覆盖本次实战 deadlock 场景）
4. **Step 4**：扩展 phase=gate 的 escalation 覆盖所有 redline 类型
5. **Step 5**：写 `decide_cmd` 的 kind dispatcher + Planner prompt templates
6. **Step 6**：写 autopilot resume 应用逻辑（拆分 feature / 换模型 / 注入 must_fix）
7. **Step 7**：迁移旧 `gate_complexity` 路径到新 escalation 系统
8. **Step 8**：删除/废弃旧文件名

总工作量：~24 小时含测试。

---

## 5. 关键设计决策

### 5.1 为什么按 phase 分文件而不是统一一个文件

- 不同 phase 的 escalation 时机不同（planning 早，gate 晚）
- 一个 feature 可能同时有多个 phase 的 escalation 排队（例如 T3 gate failed 后正在等 decide，T2 又被发现 import_rules 违反）
- 分文件让并发不冲突

### 5.2 为什么 escalation_kind 是固定枚举而不是字符串

- 每个 kind 对应一组 Planner prompt + UI 模板 + resume 应用逻辑
- 用户选项是预定义的（不是开放式自由输入）—— 这是"拆分 / 换模型 / skip"这种结构化决策
- 枚举防止字段拼写错误

### 5.3 是否每个 kind 都允许 "skip"

- 是。skip 语义：跳过这个任务/feature，autopilot 继续走下一个（如果存在）
- 但 `COMPLIANCE_BLOCK` 不允许 skip——合规违反必须解决

### 5.4 用户选择"拆分 feature"后怎么执行

- decide 命令调 Planner 出 3 个 sub-feature spec（每个含小 PRD + task description）
- 写入 `.planning_split_proposal.json`
- autopilot resume 检测到 → 创建 3 个新 planning_dir → 串行（或并行）跑各 sub-feature
- 当前 feature 标记 SUPERSEDED_BY_SPLIT

### 5.5 防无限循环

- 每个 phase 独立 escalation_count，最多 2 次
- 超过 2 次 → 整个 workflow 标 FINAL_BLOCKED，不再 escalate
- 用户必须人工介入修代码 / PRD / 模型配置

---

## 6. 风险

| 风险 | 缓解 |
|---|---|
| 改动面太大，引入新 bug | 严格按 Step 1-8 分批，每步独立测试；旧路径保留 90 天 |
| 拆分 feature 后子 features 之间依赖 | sub-feature spec 必须声明 depends_on；autopilot 按依赖序串行 |
| Planner 出的拆分方案质量差 | decide UI 让用户审核拆分前可编辑；用户决定权 |
| 多 escalation 排队 | 分 phase 文件 + 优先级顺序（planning > executor > gate） |
| 旧代码迁移引入回归 | Step 7 单独 review；保留旧 `gate_complexity` 路径作为 dual-code 直到 Step 8 |

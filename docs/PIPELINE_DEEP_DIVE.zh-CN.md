# 内部流程深度剖析

> English version: [PIPELINE_DEEP_DIVE.md](PIPELINE_DEEP_DIVE.md)

`kodawari work-all --feature X --prd Y` 两个 flag 背后跑 8 个 stage，每个边界
都有严格保证。本文给想理解内核、贡献代码、或排查非常规故障的开发者。

## 整体阶段图

```
                              kodawari work-all
                                    │
                                    ▼
            STAGE 0  复杂度 tier 探测（lite/standard/heavy）
                                    │
                                    ▼
            STAGE 1  PRD slice 探测（## Slice N: 标记）
                          │
                ┌─────────┴─────────┐
                ▼                   ▼
         单 slice 流程           多 slice 循环
            （历史默认）        （E1，本轮新增）
                │                   │
                └─────────┬─────────┘
                          ▼
            STAGE 2  规划对话（planner ↔ plan_reviewer 多轮）
                          │
                          ▼
            STAGE 3  架构方案 + scaffold + SCAFFOLD_MANIFEST
                          │
                          ▼
            STAGE 4  TASK_GRAPH 生成（greenfield 单 task 最多 5 文件）
                          │
                          ▼
            STAGE 5  task_cycle 自动循环
                     每个 task：
                       DESIGN → IMPLEMENT → VERIFY（真跑 pytest）
                              → RULES_GATE → PEER_REVIEW
                              → FIX_ROUND（如 reviewer 卡）
                              → PROCEED_TO_GATE
                          │
                          ▼
            STAGE 6  Review bundle 聚合
                          │
                          ▼
            STAGE 7  Release gate → AWAITING_DECISION 等人决策
```

## 每个 stage 的职责

### Stage 0：复杂度 tier 探测
- 文件：`autopilot/planning/complexity_detector.py`
- 看 PRD 长度、变更文件数、声明 layer、历史信号
- 三档：`lite`（单文件）/ `standard`（2-5 task feature）/ `heavy`（多 task + greenfield）
- 自动决定 `max_cycles` / `max_rounds` 上限
- 用户能看到 `[autopilot] auto-detected tier=heavy` 提示

### Stage 1：PRD slice 探测（E1，本轮实现）
- 文件：`autopilot/planning/prd_contract.py::extract_prd_slices`
- 扫描 PRD 中的 `## Slice N: <title>` / `## 切片 N: ` / `## Phase N:` / `## Part N:` 标记
- 0 或 1 个标记 → 单 slice 模式（历史默认）
- 2 个或更多 → 多 slice 循环：每个 slice 独立跑 plan+work，父级跑一次 review+release
- Resume：完成的 slice 持久化到 `.multi_slice_state.json`，重跑时跳过

### Stage 2：规划对话（双模型互审）
- 文件：`autopilot/planning/planning_orchestrator.py::run_planning_conversation`
- 这是你记得的「PRD 双模型互审」环节
- 多轮循环（默认上限 7 轮）：
  1. planner 起草 plan
  2. plan_reviewer 审计 → `must_fix[]` / 评分 / `gate_recommendation`
  3. planner 改 plan（每个变更进 `change_log[]`）
  4. reviewer 再审
- 收敛条件三选一：reviewer `approved=true` / 双模型评分 ≥8.5+8.0 触发 relaxed_score_auto_approve / Phase B/C 连续 meta_blocker 触发 demote

#### 规划对话内的安全网

| 机制 | 防什么 |
|---|---|
| Phase B meta_blocker streak demote | reviewer 死循环要求「引用第 X 轮的发现」 |
| Phase C 末轮单发恢复 | 末轮 meta blocker + 双高分时强制 demote |
| G prompt validator boundary | reviewer 越界进入 orchestrator 校验领域 |
| L5 prompt approval semantics | `approved=true + 空 findings` 是合法终态 |
| review_evidence_scout | 抓递归 `evidence_resolutions` 循环 |

### Stage 3：架构方案 + scaffold
- 解析 archetype（fastapi_api / flask_api / django_web 等）
- greenfield 优先读 `SCAFFOLD_MANIFEST.json` 的显式 archetype（A2/A3）
- 写 `ARCHITECTURE_PLAN.json` + `REPO_INVENTORY.json` + （greenfield 时）`SCAFFOLD_MANIFEST.json`

### Stage 4：TASK_GRAPH 生成
- 把 `final_plan` 转成拓扑排序的 task 图
- 每个 task 含：`task_id` / `depends_on` / `layer_owner` / `core_files` / `verify_cmd` / `invariants`
- greenfield 单 task 最多 5 文件（A4）；existing 上限 3
- 生成对应 `TASK_CARD_T1.json` ... `TASK_CARD_Tn.json`

### Stage 5：task_cycle 自动循环
- 文件：`cli/runtime/autopilot_workflow_runtime.py::_task_cycle_runtime`
- `next_task_selector` 按依赖顺序挑下一 task
- 每个 task 完整跑：
  - **DESIGN**：planner/opus 写 ADR + 技术方案
  - **IMPLEMENT**：executor 用 strict tool-use 写代码（read/str_replace/write_new_file/check_complexity/finish_execution）
  - **VERIFY**：真跑 pytest（E1 修：不再 silent skip）
  - **RULES_GATE**：静态代码 redline BLOCK/WARN/DASHBOARD
  - **PEER_REVIEW**：impl_reviewer 审实现（E2 修：尊重 --real-peer-review）
  - **FIX_ROUND**：如果有 must_fix，executor 重写 → 回到 VERIFY
  - **PROCEED_TO_GATE**：标记 task 完成

#### task_cycle 安全机制

| 机制 | 防什么 |
|---|---|
| Read-loop stall recovery | mimo 类弱 instruction-following 模型的读循环卡死 |
| `action_only_mode` 重试 | stall detector 触发后强制 retry 不能读，只能写 |
| Wall-clock 看门狗（D1） | 总时长超 `--max-wall-clock-seconds` → 写 ABORT_REPORT.json + exit 124 |
| blocked_by 闭包追溯（B1） | T_k FAIL → 下游 task 标 `blocked_by: [T_k]` |
| `_no_fake_run_strict` | `WORKFLOW_REVIEW_ENABLED=1` 时 verify/review silent-pass 路径全部 fail-closed |
| Scope-drift guard | executor 不能改 task 声明 `core_files` 之外的文件 |

#### no-fake-run policy（不假跑）

production-strict（`WORKFLOW_REVIEW_ENABLED=1` + 非 pytest + 非 WORKFLOW_SDK_TEST_MODE）下三个 fail-closed 门：

1. **Verify**：pytest 没真跑 → `_build_compat_verify_payload` 强制 `passed=false`
2. **Self-review**：`LOCAL_DEFAULT_NOT_A_REVIEW` / `NOOP_FALLBACK_NOT_A_REVIEW` 错码挡 silent-pass
3. **Peer review**：empty 评审记录 → `approved=False, approved_reason="no_peer_review_ran"`；reviewer 降级在 production strict 阻塞 proceed

Dev / subscription 模式不设 `WORKFLOW_REVIEW_ENABLED` 时保留旧 simulation 行为，本地迭代不受影响。

### Stage 6：Review bundle
- 跨所有 task 聚合：变更文件、peer review 摘要、verify 工件、gate 结论
- 写 `.review_bundle.json` 给 release gate 消费
- 多 slice 模式下，**只在父 planning_dir 跑一次**，合并所有 slice 的证据

### Stage 7：Release gate
- 设计上停在 `AWAITING_DECISION`，不自动 ship
- 用户跑 `kodawari decide --feature X --action accept`（ship）或 `--action reject`（停）
- 这是故意的摩擦——kodawari 的整个姿态是「no silent pass」。让 LLM 驱动的 pipeline 直接推 main 正是 no-fake-run policy 拒绝的行为

## Artifact 链路（单一事实来源）

每个 stage 写入 `planning/<feature>/` 的 typed schema 校验 JSON：

```
PRD.md
  ↓ Stage 1+2
PRD_INTAKE.json + PLANNING_CONVERSATION.json（多轮审计 trail）
  ↓ Stage 3
ARCHITECTURE_PLAN.json + REPO_INVENTORY.json + SCAFFOLD_MANIFEST.json
  ↓ Stage 4
TASK_GRAPH.json + TASK_CARD_T1..Tn.json
  ↓ Stage 5（每 task）
.autopilot_rounds.jsonl + .autopilot_state.json
.execution_result.json + .review_result.json + .verify_report.json
.run_truth.json（aggregate）
  ↓ Stage 6
.review_bundle.json
  ↓ Stage 7
RELEASE.md（或 ABORT_REPORT.json 如撞 wall-clock / hard-stop）
```

多 slice 模式镜像到 `planning/<feature>/slice_NN/`，父级 `.multi_slice_state.json` 追总进度。

## 配置层级

| 层 | 机制 | 范围 |
|---|---|---|
| CLI flag | `--max-cycles 10` | 单次调用 |
| 项目设置 | `.claude/workflow/defaults.yaml` | 项目级 |
| 模型配置 | `.claude/workflow/models.yaml` | 项目级（transports + roles） |
| 环境变量 | `WORKFLOW_*` 见 `docs/contracts/ENV_VAR_REFERENCE.md` | shell 级 |
| 内置默认 | `workflow_defaults.BUILTIN_DEFAULTS` | 进程默认 |

优先级（高→低）：CLI flag > defaults.yaml > env var > 内置默认。

## 关键安全保证在代码里的位置

| 保证 | 文件 |
|---|---|
| No-fake-run verify | `autopilot/core/runtime_checks.py::_build_compat_verify_payload` |
| No-fake-run reviewer | `autopilot/engine/engine_review_mixin.py::_default_review_feedback` |
| No-fake-run peer summary | `autopilot/review/review_bridge.py::summarize_peer_review` |
| Read-loop stall recovery | `autopilot/recovery/stall_recovery.py` |
| `action_only_mode` 重试 | `autopilot/execution/execution_openai_tool_use.py` |
| Wall-clock 看门狗 | `cli/runtime/autopilot_cmd.py::_start_wall_clock_watchdog` |
| Scope-drift guard | `autopilot/execution/execution_isolation.py` |
| blocked_by 闭包追溯 | `cli/contract/next_task_selector.py::_trace_blocking_ancestors` |
| Greenfield archetype 锁定 | `cli/contract/generic_bootstrap.py::_scaffold_archetype_hint` |
| Multi-slice 循环（E1） | `cli/runtime/work_all_runtime.py::_run_work_all_multi_slice` |

## 相关文档

- [QUICKSTART.md](QUICKSTART.md) — 首次跑通走查
- [USER_GUIDE.md](USER_GUIDE.md) — 操作手册
- [WRITING_PRD.zh-CN.md](WRITING_PRD.zh-CN.md) — 如何写 kodawari 喜欢的 PRD
- [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) — 错码索引
- [contracts/ENV_VAR_REFERENCE.md](contracts/ENV_VAR_REFERENCE.md) — env var 全索引
- [CAPABILITY_MAP.md](CAPABILITY_MAP.md) — backend × capability 矩阵

# kodawari

> *拘り*（kodawari）—— 日语「对细节的执着、匠人不愿妥协的精神」。

**English version**: see [README.md](README.md).

**kodawari** 是一个自主软件交付的 autopilot。给它一份 PRD，它把这个 feature
交付出来：planner 起草契约，reviewer 审查计划，executor 写代码，一道严格的
gate 拒绝任何"没真验证"的 silent-pass。

关键词是 **严格**。kodawari 的设计核心是 **no-fake-run policy**：每个
"verify" 都真跑 `pytest`，每个 "peer review" 都真调一次 reviewer 模型，每个
"approval" 都锚定在真 artifact 上。没有任何 fallback 路径把 `passed=true`
偷渡过 gate。

## 为什么用 kodawari？

如果你用过其它 AI 编程工具，kodawari 的定位是：

| 工具 | 形态 | kodawari 的差别 |
|---|---|---|
| **Claude Code / Codex CLI** | 交互式 REPL，单 model 单轮 | 多模型角色分离（planner ≠ reviewer ≠ executor）+ 契约优先的 artifact 链路。5 轮聊天 ≠ 一份 PRD 驱动的完整交付 |
| **Cursor / Windsurf** | IDE 内嵌编辑器 + copilot | Headless / 可脚本化 / CI 友好。不锁定编辑器。审计为先：每步都写 JSON artifact |
| **Aider** | Git-aware 增量结对编程 | Greenfield 一等公民（空目录 → 完整 feature）；verify+review gate 严格 fail-closed；planner 可以否决自己 |
| **OpenHands / Devin** | 通用自主 agent | 范围更窄（Python 项目交付）、no-fake-run 保证更强、爆炸半径更小。不试图"什么都做"；试图"把一件事真做完不说谎" |

适合 kodawari 的场景：
- 想要 PRD → 已交付 feature 的 **流水线**，而不是聊天
- 需要硬保证 "verify passed" 真的意味着 `pytest` 跑了并返回 0
- 多 LLM，每个角色独立可配（如：便宜的 planner + 高端 reviewer + 本地 executor）
- CI 友好，每一步都吐机器可读的 artifact

如果你想要一个嘴贫的结对程序员，去用其它工具——kodawari 故意是 opinionated +
process-heavy 的。

## Quickstart

```bash
# 1. 安装（Python 3.11+）
pip install -e .

# 2. 生成配置（交互式 — 选 preset，写出 models.yaml、.env.example、
#    以及项目级 defaults.yaml 设置文件）
kodawari init-wizard

# 3. 自检（不打网络；验证 project root / planning dir / env vars）
kodawari doctor preflight --feature my-feature --prd ./PRD.md

# 4. 跑
kodawari work-all --feature my-feature --prd ./PRD.md
```

就这样 —— 跑只需要 2 个 flag。剩下的内置默认全部合理（peer review 开启、
每个 task 5 cycle、1 小时 wall-clock 上限、advisory gate）。任何想微调的
都在 `.claude/workflow/defaults.yaml`（wizard 生成）里改，或者在命令行
显式传 flag 来一次性 override。

## 工作流程

```
            PRD.md
              │
              ▼
   ┌──────────────────────┐
   │  prd-intake          │  抽取 source-of-truth / layers / 约束
   └──────────────────────┘
              │
              ▼
   ┌──────────────────────┐
   │  architecture-plan   │  archetype + surfaces + 模块边界
   └──────────────────────┘
              │
              ▼
   ┌──────────────────────┐
   │  init                │  scaffold + 持久化 SCAFFOLD_MANIFEST
   └──────────────────────┘
              │
              ▼
   ┌──────────────────────┐
   │  task-plan           │  TASK_GRAPH.json（5-7 个垂直切片任务）
   └──────────────────────┘
              │
              ▼
   ┌─────────── 每个 task ────────────────────────────────────┐
   │  design → implement → verify → rules_gate → peer_review  │
   │                                          │               │
   │                                          ▼               │
   │                         fix_round（reviewer 卡时）       │
   └──────────────────────────────────────────────────────────┘
              │
              ▼
       release gate → ship
```

三个 LLM 角色，每个独立配模型 + transport：

- **Planner** — 起草和修改计划；读 PRD context / 上轮 reviewer findings / repo inventory
- **Reviewer**（plan + impl）— 审计计划和代码；可以用 must-fix 卡住
- **Executor** — 通过严格的 tool-use 协议写代码（read / search / patch / verify）；不能越过声明的文件 scope

角色配置在 `.claude/workflow/models.yaml`（由 `init-wizard` 生成）。可以混合
provider：Claude planner + GPT reviewer + 自托管 executor 等。

## 独特保证

- **No-fake-run**：每个 reviewer 调用、每个 verify 命令、每个 gate 决策都锚定到真 artifact。`KODAWARI_REVIEW_ENABLED=1` 下 silent-pass fallback 路径全部 fail-closed。
- **契约优先**：PRD → INTAKE → ARCHITECTURE_PLAN → TASK_GRAPH → TASK_CARD 是 schema 校验的强类型链路。下游消费方读这些 artifact 作为单一事实来源。
- **Greenfield 一等公民**：scaffold + 持久化 manifest + planner 模式信号，意味着空目录 + PRD 端到端跑通，无需人工提示 archetype。
- **Wall-clock 看门狗**：`--max-wall-clock-seconds` 超时写 `ABORT_REPORT.json` 并 exit 124（POSIX 超时约定）—— 没有永远跑下去的 loop。
- **闭包追溯的依赖跳过**：task 失败时下游 task 报 `blocked_by: [<failed-ancestor>]` 而非直接的 missing dep —— 根因一眼可见。

## 状态

**v0.1 — 首个公开发布**。完整 greenfield 跑通验证（spec → FastAPI 服务 + 测试，
5/5 task 完成，6/6 verify 真跑 pytest，6/6 peer review 含 1 次 fix-loop 自愈）。
非玩具项目推荐用 production strict mode。

已知限制：
- PRD intake 启发式偏保守。非 FastAPI 形态（CLI / lib / data pipeline）能跑但
  可能产生低置信度 intake；`kodawari init` 显式指定 `--archetype` 是 workaround
- Release gate 设计上停在 `AWAITING_DECISION` —— 必须显式 `kodawari decide`
  才能 ship
- env vars 当前还是 `WORKFLOW_*` 前缀（pre-rename 遗留）；`KODAWARI_*` 重命名
  规划在 v0.2

## 目录结构

```
src/kodawari/             # 主包
  autopilot/              # engine, planning, execution, review
  cli/                    # kodawari CLI 命令
  gate/                   # 代码质量 gate profile
  schemas/                # 每个 artifact 的 JSON-Schema 契约
tests/                    # 200+ 测试
docs/                     # QUICKSTART / USER_GUIDE / OPERATOR_RUNBOOK 等
adapters/                 # claude-code / codex-cli 集成插件
scripts/                  # 引导 + 维护脚本
examples/hello-bookmark/  # 5 分钟可走完的 hello-world 示例
```

## 文档导航

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — 首次跑通走查（30s noop、10min Claude 订阅、空目录起步）
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — 完整操作手册
- [docs/WRITING_PRD.zh-CN.md](docs/WRITING_PRD.zh-CN.md) — **如何写 kodawari 喜欢的 PRD**（首次跑通必看）
- [docs/OPERATOR_RUNBOOK.md](docs/OPERATOR_RUNBOOK.md) — 错码索引、故障排查
- [docs/CAPABILITY_MAP.md](docs/CAPABILITY_MAP.md) — capability × backend 兼容矩阵
- [docs/contracts/ENV_VAR_REFERENCE.md](docs/contracts/ENV_VAR_REFERENCE.md) — 所有 env var 完整索引
- [examples/hello-bookmark/](examples/hello-bookmark/) — 5 分钟可走完的端到端例子

## 协议

MIT — 见 [LICENSE](LICENSE)。

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

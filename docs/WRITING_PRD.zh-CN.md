# 如何写 kodawari 喜欢的 PRD

> English version: [WRITING_PRD.md](WRITING_PRD.md)

PRD（Product Requirements Document）是 kodawari 读来做 planning、implementation、
verification 的**单一事实来源**。PRD 写对，autopilot 有戏跑通。PRD 写错，
你会浪费 cycle 跟 "low confidence" 警告 / 错的 layer 推断 / planner 凭空发明
你没要的 route 对线。

这份指南是**实用配方**，不是论文。按下面的结构写，intake 启发式第一次就能产
出 `confidence: high` 的 PRD_INTAKE.json。

## 5 个关键段落

```markdown
# PRD: <一行 feature 名>

## 目标

<2-4 句。解决什么问题、对谁。>

## 范围

<具体可交付物。用 bullet。REST API 的话每个 endpoint 列出 method+path
+ 请求体 + 响应 shape。CLI 的话每个子命令列出 args + 行为。>

## 数据契约

source of truth: <db.表名 | filesystem | upstream_service>

schema:
- <字段 1>
- <字段 2>

不变量:
- <永远为真的事情>
- <永远不能发生的事情>

## 分层

- schema 层：`path/to/schema.py`
- repository 层：`path/to/repository.py`
- service 层：`path/to/service.py`
- route 层：`path/to/main.py`

## 不在范围

- <planner 不应该当 task 加上的事>
- <推迟到下一份 PRD 的事>

## Acceptance Criteria

- <能证明 feature 工作的可观测测试>
- <能证明 edge case 被处理的可观测测试>
```

## intake 启发式看什么

intake parser 是轻量级 regex + 关键词扫描，**不是 LLM**。它扫这些信号：

| 信号 | 触发条件 | 为什么重要 |
|---|---|---|
| **source of truth** | 字面短语 "source of truth:" 或 "数据源:" | 设置 `source_of_truth` 和 `source_of_truth_canonical`。除 `view` 之外的 layer 必填 |
| **layer 关键词** | "schema"、"repository"、"service"、"route"、"model"、"view" | 填 `layers[]`。缺这个 = fallback 到默认 3 层 + `confidence=low` |
| **path_type** | "read"/"read-only" 词 vs "write"/"create"/"update"/"delete" | 设置 `path_type=read\|write\|both` |
| **out_of_scope** | 标题 "不在范围"/"out of scope" 的段落 | 把 bullet 拽进 `out_of_scope[]`，planner 不会提它 |
| **module 边界** | 显式文件路径（`app/main.py`、`app/service.py`） | 驱动 `module_boundaries[]`。由路径提及推断 |

## 常见失败和修法

### `confidence: low — layers fell back to default service/repository/route set`

PRD 没显式提到 layer 名。修：加 `## 分层` 段落，每个 layer 配一个路径。

### Planner 凭空加了你没要的 `frontend` task

intake 启发式在 PRD 里某处看到了 "page"、"UI"、"view" 或 "frontend" 之类的词。
修：要么去掉那个词，要么加一条 `不在范围` bullet 写"不做 frontend / 不做 web UI"。

### Greenfield 跑出来 scaffold 了 `fastapi_api` 但你想要 CLI

默认 archetype 探测器在没有其它 marker 时 fallback 到 `fastapi_api`。修法二选一：

- 跑 `kodawari init --archetype <name>` 显式指定，在 `task-plan` 之前
- `init` 写出来的 archetype manifest 之后会锁住这个选择
- 可选 archetype：`fastapi_api`、`flask_api`、`django_web`、`node_api`、
  `react_web`、`fullstack_fastapi_react`、`fullstack_django_react`

### Task 被截断到 3 个文件但你想要 5 个

你在 `existing` 模式，但 task 是 bootstrap 形态（schema+model+repo+service+test）。
修：在 `task-plan` 上加 `--mode greenfield` 切到 greenfield 模式（允许每个
task 最多 5 个文件）。

## 完整例子

看 [examples/hello-bookmark/PRD.md](../examples/hello-bookmark/PRD.md) —— 一份
能产出 `confidence: high` + 干净 4-5 task graph 的完整 PRD。

## 大 PRD：声明 slice

如果你的 feature 大到一次规划对话搞不定，给 PRD 加 `## Slice N: <title>`
（或 `## 切片 N:` / `## Phase N:` / `## Part N:` / `## 阶段 N:` / `## 部分 N:`）
标记。kodawari 自动探测到 2 个或更多 slice，按顺序跑每个 slice
的 plan + work（详见 [PIPELINE_DEEP_DIVE.zh-CN.md](PIPELINE_DEEP_DIVE.zh-CN.md)
Stage 1 的循环语义）。

```markdown
# PRD: <feature 名>

## 目标
<跨 slice 共享的高层目标>

## Slice 1: schema + repository 层
<这个 slice 的范围 / 契约 / 分层 / acceptance（按上面 5 段模板）>

## Slice 2: API endpoints
<同样 5 段，scope 到这个 slice>

## Slice 3: tests + docs
<同样 5 段>
```

每个 slice 在 `planning/<feature>/slice_NN/` 独立目录跑完整 plan + execute
+ 每个 task 的 verify 和 peer review。所有 slice 完成后父级跑**一次** review
+ release。`.multi_slice_state.json` 记录进度，支持中途失败 resume。

可识别的同义标记：`## Phase N:`、`## Part N:`、`## 切片 N:`、`## 阶段 N:`、
`## 部分 N:`。**必须**带数字 + 冒号（这样 `## Slice options` 这种描述性
标题不会被误识别）。

只有 1 个或 0 个标记 → 走历史的单 slice 流程，BC 完整保留。

## kodawari 不适合的场景

如果你的 feature 本身是模糊的、探索性的、open-ended（"重新设计 auth 层"），
kodawari 会很挣扎 —— 它的强项是交付 **bounded、contracted** 的 feature。
探索阶段用 chat 工具；能写出上面 5 段的 PRD 之后切到 kodawari。

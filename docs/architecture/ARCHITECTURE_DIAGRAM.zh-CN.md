# 三、中文架构流程图

## 1. 为什么之前看起来“没有了”

旧现场里的 `workflow_flowchart*.mmd` 与早期双仓口径已经不再可靠。
当前主线已经收束为“`kodawari` 单仓承载 planning + runtime + review + gate + status”，所以仓内之前只保留了一张最小英文图，避免继续传播过时结构。

本文件把现行主线重新画成中文版本，和当前 `docs/一、平台现状、架构与兼容总览.md`、`docs/二、运行操作、门禁规则与后续路线.md` 对齐。

## 2. 中文主架构图

```mermaid
flowchart TD
    U["用户 / CI / Operator<br/>canonical 5 动词 或 autopilot / work all"] --> C["CLI 门面层<br/>kodawari.cli.main + parser_registry"]
    H["历史 workflow-claude 壳层语义"] -. 已吸收到单仓主线 .-> C

    C --> F["命令门面<br/>setup / plan / work / review / release / status<br/>兼容: autopilot / work all / wf-*"]
    F --> PB["合同优先规划桥<br/>ensure_contract_first_planning()"]

    subgraph P["规划真值层"]
        P1["PRD_INTAKE.json<br/>需求摄取"]
        P2["REPO_INVENTORY.json<br/>仓库盘点"]
        P3["ARCHITECTURE_PLAN.json<br/>架构规划"]
        P4["TASK_GRAPH.json<br/>任务图"]
        P5["TASK_CARD_ACTIVE.json / Plans.md<br/>当前活动任务"]
    end

    PB --> P1 --> P2 --> P3 --> P4 --> P5

    P5 --> R["运行时装配层<br/>autopilot_cmd / work_all_runtime / release_cmd"]
    R --> E["AutopilotEngine<br/>session + implementation + review mixins"]

    subgraph X["执行与评审主循环"]
        X1[".execution_request.json<br/>执行请求"]
        X2["执行后端<br/>codex_cli 为官方主路径<br/>claude_code 为兼容后端"]
        X3[".execution_result.json<br/>changed_files / runtime truth"]
        X4[".review_bundle.json / .review_evidence.json<br/>评审证据包"]
        X5["评审双车道<br/>Codex 自审 + Peer Review<br/>simulated / real_opus"]
        X6["verify / qa / ship-readiness<br/>多表面验证与发布判断"]
    end

    E --> X1 --> X2 --> X3 --> X4 --> X5 --> X6
    X5 -- 需要修复 --> L["修复后进入下一轮 work-loop"]
    L --> X1

    P3 --> D{"是否需要拍板?"}
    P4 --> D
    X6 --> D
    D -- 是 --> D1["决策桥<br/>.decision_request.json"]
    D1 --> D2["人工 / 上层系统响应<br/>.decision_response.json"]
    D2 --> R
    D -- 否 --> G["Release / Gate 收口"]

    subgraph Q["质量门禁与可观测性"]
        Q1["gate checker_*<br/>重复实现 / 越层 import / 合同一致性 / ratchet"]
        Q2[".gate_result.json / GATE.md<br/>PASS 或 BLOCKED"]
        Q3["status / unified_status<br/>RUNNING / AWAITING_DECISION / AWAITING_ENVIRONMENT / BLOCKED / PASS"]
        Q4["workflow_chain / stability_report / lane trend<br/>报告与留痕"]
    end

    G --> Q1 --> Q2 --> Q3 --> Q4
    P1 --> Q3
    P3 --> Q3
    X3 --> Q3
    X5 --> Q3
    X6 --> Q3
```

Mermaid 源文件同步放在：

- `docs/diagrams/autopilot_flow.mmd`

## 3. 简化版中文架构图

```mermaid
flowchart TD
    A["用户入口"] --> B["命令入口层"]
    B --> C["规划阶段"]
    C --> C1["需求整理"]
    C --> C2["架构规划"]
    C --> C3["任务拆解"]

    C3 --> D["执行主循环"]
    D --> D1["代码实现"]
    D --> D2["结果产物"]

    D2 --> E["评审与验证"]
    E --> E1["自审"]
    E --> E2["同伴评审"]
    E --> E3["验证与质量检查"]

    E3 --> F{"是否需要人工拍板"}
    F -- 需要 --> G["发起决策请求"]
    G --> H["人工确认"]
    H --> D
    F -- 不需要 --> I["门禁收口"]

    I --> J["状态汇总"]
    J --> K["报告与留痕"]

    E2 -- 需修复 --> D
    C --> J
    D2 --> J
    E --> J
```

这张图对应的 Mermaid 源文件放在：

- `docs/diagrams/simple_cn_flow.mmd`

## 4. 读图说明

- 顶层入口分成两类：对最终用户推荐的是 `setup -> plan -> work -> review -> release -> status`，对 operator / CI 保留 `autopilot`、`work all` 这类快速入口。
- 规划链已经固定为 contract-first 真值链，核心是 `PRD_INTAKE.json -> REPO_INVENTORY.json -> ARCHITECTURE_PLAN.json -> TASK_GRAPH.json -> TASK_CARD_ACTIVE.json`。
- runtime 主循环以 `AutopilotEngine` 为内核，先产出 execution truth，再进入 review、verify、qa 与 ship-readiness。
- review 已不是单点动作，而是 `Codex 自审 + Peer Review` 的双层闭环；若 `must_fix` 未清零，就重新回到 work-loop。
- 一旦遇到意图澄清、架构冻结、任务图冻结或发布审批，系统会落地 `.decision_request.json`，等 `.decision_response.json` 回来后继续推进。
- 最终不是“只跑完一个命令就算结束”，而是统一收口到 gate、status 与 report，让 `PASS / BLOCKED / AWAITING_*` 真值对外保持一致。

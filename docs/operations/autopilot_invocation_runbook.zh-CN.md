# Autopilot 调用手册（newsapp 实战验证版）

> ⚠️ **本文档保留了 v0.1.2 之前的冗长 CLI 例子作为历史/高级排查参考**。
> 新用户上手请用简化形式 `kodawari work-all --feature X --prd Y`（默认值已
> 经合理）。完整流程见 [PIPELINE_DEEP_DIVE.zh-CN.md](../PIPELINE_DEEP_DIVE.zh-CN.md)。
> 下文里所有 `--executor-backend XXX --max-cycles N --real-peer-review …`
> 的形式都仍然能用，但通常不需要显式传——它们的 default 已经覆盖。

> 2026-04-24 首次跑通：p1c-google-trends-rss T1/T2/T3 连续 3 个 task 全部 PASS，gate 0 violations。
> 本文记录**当时生效的完整调用方式**以及 codex 后端失败时的排查清单。

---

## 1. 成功调用过的命令模板

### 1.1 推荐：claude_code 执行 + codex 审查（本次验证）

```bash
cd e:/code_rebuild/newsapp
WORKFLOW_PLANNER_TIMEOUT=600 \
WORKFLOW_CLAUDE_AUTH_MODE=host \
WORKFLOW_CODEX_AUTH_MODE=host \
WORKFLOW_PLANNING_MAX_ROUNDS=3 \
WORKFLOW_REVIEWER_CODEX_EXECUTABLE="C:/Users/liafei/AppData/Roaming/npm/codex.cmd" \
  kodawari autopilot \
    --project-root e:/code_rebuild/newsapp \
    --feature p1c-google-trends-rss \
    --task "实现 Google Trends RSS 外部榜后端" \
    --prd newsapp/planning/p1c-google-trends-rss/PRD_SLICE.md \
    --executor-backend claude_code \
    --tier lite \
    --task-cycle
```

### 1.2 关键 CLI 参数含义

| 参数 | 作用 | 本次取值 |
|------|------|---------|
| `--executor-backend` | 实现阶段用哪个后端 | `claude_code` |
| `--tier` | 档位预设（lite/standard/heavy/auto） | `lite` |
| `--task-cycle` | 强制启用 task_cycle（覆盖 tier 默认） | 开 |
| `--feature` | planning 目录名 | `p1c-google-trends-rss` |
| `--prd` | PRD 文件路径（会作为 planner 权威源） | `PRD_SLICE.md` |
| `--task` | planner 的任务方向描述 | 一句话概述 |

### 1.3 必需 env 变量

| Env 变量 | 默认 | 本次值 | 作用 |
|----------|------|--------|------|
| `WORKFLOW_PLANNER_TIMEOUT` | 300 | **600** | newsapp context 大，300s 会超时 |
| `WORKFLOW_CLAUDE_AUTH_MODE` | host | host | 把 `~/.claude/.credentials.json` 同步到隔离 HOME |
| `WORKFLOW_CODEX_AUTH_MODE` | host | host | 把 `~/.codex/auth.json` 同步到隔离 HOME |
| `WORKFLOW_PLANNING_MAX_ROUNDS` | 3 | 3 | planner↔reviewer 互审轮次上限（**不受 `--tier` 影响**） |
| `WORKFLOW_REVIEWER_CODEX_EXECUTABLE` | `codex`（PATH 查找） | `C:/Users/liafei/AppData/Roaming/npm/codex.cmd` | Windows 下 npm 全局包不一定在 subprocess PATH，需显式指定 |

### 1.4 models.yaml 正确写法（`.claude/workflow/models.yaml`）

```yaml
schema_version: "models.v1"

planner_model: claude-sonnet-4-6

executor_models:
  claude_code: claude-sonnet-4-6
  codex_cli: gpt-5.3-codex

executor_model: gpt-5.3-codex  # fallback 当 backend 未列入 executor_models

reviewer_model: gpt-5.3-codex
reviewer_backend: codex
review_enabled: true
```

**关键点**：`executor_models` 是 per-backend 映射；如果只写扁平 `executor_model: gpt-5.3-codex`，
切成 `--executor-backend claude_code` 时会把 gpt 模型名传给 claude CLI，claude 会报"model unavailable"。

---

## 2. codex 后端为什么经常调不起来

### 2.1 最常见 5 个失败原因（按出现频率）

| 症状 | 根因 | 排查命令 |
|------|------|---------|
| `codex_cli exited 1, stderr: auth error` | 隔离 CODEX_HOME 里没同步到 `auth.json` | `ls e:/code_rebuild/tmp_codex_home/.codex/auth.json` |
| `model 'gpt-5.3-codex' not available` | `models.yaml` 的 executor_model 与用户 codex 账号实际可用模型不一致 | `grep ^model C:/Users/liafei/.codex/config.toml` |
| 启动后卡 >10 分钟不返回 | codex CLI 在等待 tokens 刷新；host auth.json 里 tokens 过期 | 检查 `~/.codex/auth.json` 的 `last_refresh` |
| `CODEX_CLI_MISSING` | codex 不在 PATH 或 `WORKFLOW_CODEX_EXECUTABLE` 指错 | `where codex` |
| 沙箱化失败 `EPERM` | isolated workspace 创建失败，常见于 OneDrive/杀软盘 | 切到非托管目录 |

### 2.2 auth 同步逻辑（引用源码）

[execution_codex_cli.py:114-158](../../src/kodawari/autopilot/execution/execution_codex_cli.py#L114-L158)：

- `WORKFLOW_CODEX_AUTH_MODE=host`（默认）→ 自动把 `~/.codex/auth.json` 拷贝到隔离 HOME
- `WORKFLOW_CODEX_AUTH_MODE=isolated` → 不拷贝，codex 起来就无凭据，必然失败
- 如果隔离 HOME 已存在 `auth.json` 则**不覆盖**（为保留旧会话）

### 2.3 诊断步骤（按顺序）

```bash
# 1. 确认 codex 本机能手动跑通
codex --help

# 2. 确认 host auth.json 存在且未过期
python -c "import json; d=json.load(open('C:/Users/liafei/.codex/auth.json','r',encoding='utf-8')); print('tokens present:', 'tokens' in d)"

# 3. 确认 executor_models 映射正确
cat newsapp/.claude/workflow/models.yaml

# 4. 删隔离 HOME 强制重建
rm -rf e:/code_rebuild/tmp_codex_home/.codex/auth.json

# 5. 小范围 dry-run
WORKFLOW_CODEX_AUTH_MODE=host kodawari autopilot --feature X --tier lite --executor-backend codex_cli ...
```

---

## 3. 已修复的历史坑（不要踩回去）

### 3.1 `node_home_probe_failed_exit_0`（2026-04-23 修）

`_probe_node_realpath_for_home` 里原先写了 `returncode != 0 or 1`，`0 or 1 == 1`，把成功当失败。
[execution_claude_code.py](../../src/kodawari/autopilot/execution/execution_claude_code.py) 已修，别再手抖加 `or 1`。

### 3.2 Claude auth 未同步到隔离 HOME（2026-04-23 修）

新增 `WORKFLOW_CLAUDE_AUTH_MODE=host` 逻辑，对齐 codex。
必须同时同步 `.credentials.json` + `.claude.json`，单独拷其一会提示重新登录。

### 3.3 Planner 路径漂移（2026-04-23 修）

PRD Excerpt 权重从 4 调到 0.6（最高），并在 planner prompt 加了 "PRD authority" 段。
如果又发现 planner 发明不存在的路由/函数名，优先检查 `planning_context.py` 的 priority。

### 3.4 gpt 模型名传给 claude CLI（2026-04-23 修）

加了 `executor_models` per-backend 映射；see §1.4。
**不要**为此类问题在 kodawari 里加"过滤 claude 非法 model"的 shim —— 根因在 models.yaml 配置。

### 3.5 Claude CLI 403 "Request not allowed"（2026-04-24 修）

VS Code 扩展的 Claude 会话和 `claude -p` CLI **不共享登录态**。
扩展用 Windows Credential Manager，CLI 用 `~/.claude/.credentials.json`。
即使扩展里已登录 Claude Max，CLI 首次使用仍需单独 `claude login`。
修法：在 PowerShell 里运行 `claude login`，完成 OAuth 流程，然后 `claude -p "hello"` 验证。

### 3.6 codex reviewer "executable not found"（2026-04-24 修）

Windows npm 全局安装路径（`%APPDATA%\npm`）不在 workflow subprocess 的 PATH 里。
`WORKFLOW_REVIEWER_CODEX_EXECUTABLE` 需显式指向完整路径，see §1.3。

---

## 4. planning 互审的**真实**行为（重要）

- **轮次上限**：`WORKFLOW_PLANNING_MAX_ROUNDS`（默认 3），**不走 `--tier`**。
  tier 里的 `review_max_rounds` 用在 peer_review / collaboration，不是 planner↔reviewer 互审。
  来源：[autopilot_contract_bridge.py:233](../../src/kodawari/cli/contract/autopilot_contract_bridge.py#L233)

- **放行条件**：本轮 Reviewer 返回的 findings 中，severity 在 lane 阈值内的数量为 0。
  lite={blocking}, standard={blocking,critical}, heavy={blocking,critical,high}。
  来源：[planning_orchestrator.py:754-763](../../src/kodawari/autopilot/planning/planning_orchestrator.py#L754-L763)

- **轮间 findings 传递**：（2026-04-24 修）**全量 findings** 注入下一轮 planner prompt（修前只传 blocked 子集，导致 reviewer 白跑）。
  来源：[planning_orchestrator.py:759](../../src/kodawari/autopilot/planning/planning_orchestrator.py#L759)

- **顽固轮次检测**（2026-04-24 新增）：连续 2 轮 plan 无实质变化（7 维度对比）→ 自动触发 `escalation_required`，不再空转。

- **过不了怎么办**：
  - `decision_policy=auto-skip`（lite 默认）→ 无视，继续执行
  - `decision_policy=soft-gate`（standard 默认）→ critical/blocking 硬阻断；high×2+ 硬阻断；仅 medium/low 则警告继续
  - `decision_policy=approval-required`（heavy 默认）→ 挂起等人工

---

## 5. 出错了查哪里

| 文件 | 内容 |
|------|------|
| `planning/<feature>/.autopilot_state.json` | 当前 stage、任务状态、error_history |
| `planning/<feature>/.execution_request.json` | 最近一次下发给后端的 payload |
| `planning/<feature>/.execution_result.json` | 最近一次后端返回 |
| `planning/<feature>/PLANNING_CONVERSATION.json` | 互审原始记录 |
| `planning/<feature>/DELIVERY_REPORT.md` | 人读交付报告 |
| `planning/<feature>/.lane_observation.json` | lane 预测 vs 实际（看是否错分档） |

---

## 6. 版本戳

- 首次成功跑通：2026-04-24
- 验证特性：newsapp p1c-google-trends-rss（T1 provider / T2 cache service / T3 route 全过）
- 执行后端：`claude_code` (Claude Sonnet 4.6 via subscription)
- 审查后端：`codex` (gpt-5.3-codex)
- commit 范围：e36014a..HEAD

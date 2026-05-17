# 修复方案：减少 `EXECUTOR_STALLED_NO_WRITE_PROGRESS` 的读循环

## 问题摘要

在 v9 跑（deepseek-v4-flash 执行 T3-T5）970 个 tool calls 实证：

- `read_file_partial` 459 次（47%）
- `search_file` 242 次（25%）
- `get_file_hash` 144 次（15%）
- `str_replace` 28 次（3%）

单文件 `channel_upgrade_engine.py` 被 `read_file_partial` **235 次**。

根因 70% 在架构 + 30% 在模型推理深度：

1. **prompt 主动鼓励重读**：`tool_use_prompt.compact_tool_result_content` line 141 的 instruction 字面写"Call search_file or read_file_partial again if exact text is needed"。
2. **stall 检测只惩不防**：`max_redundant_read_count=8` 只匹配**完全相同**的 `path+offset+limit`；LLM 用相邻 offset 可绕过。
3. **没有 read range cache**：runtime 不记得"我已经读过 1-100 行"，模型每次都觉得需要再看。
4. **tool_manifest 不暴露 read 工具**：manifest 列了 8 个写工具，runtime 还多挂了 read_file_partial/search_file，工具集合和描述不一致。

## 改动总览（4 项，从小到大）

| # | 改动 | 文件 | 行数估计 |
|---|------|------|--------:|
| 1 | compaction instruction 不再鼓励重读 | tool_use_prompt.py:141 | 1 行改 |
| 2 | read range cache + dedupe | tool_use_runtime.py + 新 read_cache.py | ~80 行 |
| 3 | user_prompt 显式列出 "already-read ranges" | tool_use_prompt.py:user_prompt | ~15 行 |
| 4 | 第一次 partial read 自动给 full file（小文件 ≤ 1500 行） | tool_use_runtime.py:_read_file_partial_handler | ~20 行 |

## 改动 1：compaction instruction

**当前**：
```python
# tool_use_prompt.py:141
"instruction": "This older read/search result was compacted to keep the tool loop small. Call search_file or read_file_partial again if exact text is needed."
```

**改成**：
```python
"instruction": "Older read/search result compacted. Earlier reads of this file are still listed in your context as 'Already-read ranges'. Do NOT re-read the same range — refer to your prior message history. Only read a NEW range (different lines) if absolutely needed."
```

**为什么**：把模型从"再读一次"的默认引导，掰到"先看历史"。

## 改动 2：read range cache + dedupe（核心）

新文件：`kodawari/autopilot/execution/tool_use_read_cache.py`

```python
class ReadCache:
    """Track which (path, [start, end)) ranges have been read in this session.

    On a read_file_partial(path, offset=A, limit=L) call:
    - Compute new range [A, A+L)
    - Compare against self.ranges[path] (sorted list of (start, end) tuples)
    - If new range is FULLY covered by an existing range: return a "redundant" result
      flagged with redirect_to_history=True so the executor wrapper short-circuits
      the actual file read and returns a small JSON pointer to the prior tool call
    - If new range PARTIALLY overlaps: merge into existing ranges, return only the
      genuinely new bytes
    - If new range is DISJOINT: record and serve fresh
    """

    def __init__(self):
        # path -> sorted list of (start, end) non-overlapping ranges
        self.ranges: dict[str, list[tuple[int, int]]] = {}

    def check(self, path: str, offset: int, limit: int) -> ReadCacheDecision:
        ...

    def record(self, path: str, offset: int, limit: int) -> None:
        ...

    def summary_for_prompt(self) -> list[str]:
        """Return human-readable lines like
           'channel_upgrade_engine.py: lines 1-100, 150-300'
        for injection into the user_prompt."""
        ...
```

接入点：`tool_use_runtime._handle_read_file_partial`（具体名以 grep 为准）

```python
def _handle_read_file_partial(self, args):
    path = args["path"]
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 0)
    decision = self.read_cache.check(path, offset, limit)
    if decision.fully_redundant:
        return {
            "ok": True,
            "redundant_read": True,
            "instruction": (
                f"You already read {path} lines {decision.cached_start}-{decision.cached_end}. "
                "Refer to that earlier tool result above. Do NOT re-read this range."
            ),
            "path": path,
        }
    self.read_cache.record(path, offset, limit)
    return self._actual_read(...)  # 原来的逻辑
```

**红绿测试**：单元测试覆盖：
- 完全包含（offset=10,limit=20 已读 → offset=12,limit=10 命中冗余）
- 部分重叠
- 完全不重叠
- 多次小窗口拼成大窗口后，新查询命中冗余

## 改动 3：user_prompt 显式列 already-read ranges

`tool_use_prompt.user_prompt`：

```python
def user_prompt(runtime):
    ...
    already_read = runtime.read_cache.summary_for_prompt()  # ← 新增
    already_read_block = ""
    if already_read:
        already_read_block = (
            "\n\nAlready-read file ranges (in your message history above — DO NOT re-read these):\n"
            + "\n".join(f"  - {line}" for line in already_read)
            + "\n"
        )
    return (
        f"{preamble}"
        "Implement this workflow task using the tool manifest.\n"
        ...
        f"{already_read_block}"
        f"Task request: ..."
    )
```

**为什么**：把"你已经看过什么"显式拍到模型脸上，比靠它自己回忆 history 可靠。

## 改动 4：第一次 partial read → full file（小文件）

`tool_use_runtime._handle_read_file_partial`：

```python
def _handle_read_file_partial(self, args):
    path = args["path"]
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 0)

    # 第一次读这个文件且没指定 limit，或者文件 < 1500 行 → 给完整文件
    if (
        path not in self.read_cache.ranges
        and offset == 0
        and (limit == 0 or self._file_line_count(path) <= 1500)
    ):
        return self._actual_read_full(path)  # 一次读完，避免后续反复 partial

    # 原有逻辑
    ...
```

阈值 1500 行的依据：刚好是 `file_max_lines` 红线（防止 LLM 一次性读爆 context），同时 newsapp 90% 文件 < 1500 行。

## 不改的事

- **不动 max_redundant_read_count=8**：作为兜底保留
- **不动 max_read_windows_per_path=8**：作为兜底保留
- **不去删 read_file_partial 工具**：兼容现有 codex_cli / claude_code 路径
- **不动 tool_manifest 暴露规则**：是另一个独立问题，留作后续

## 验证

1. 单元测试：`tests/autopilot/execution/test_read_cache.py` 覆盖 cache 算法
2. 集成测试：用 deepseek-v4-flash 重跑 T5（之前耗尽 8 个 attempt 退出），观察：
   - `read_file_partial` 调用数从 459 降到 ≤30（目标）
   - `EXECUTOR_STALLED_NO_WRITE_PROGRESS` 不再触发，或触发次数 ≤ 1
   - T5 单次 cycle 内完成
3. 回归：跑 T2 的 user_redesign 路径，确保 prompt 改动没破坏 user_redesign preamble

## 风险

| 风险 | 缓解 |
|------|------|
| read cache 错过真实需要的重读（如文件被 str_replace 修改后） | cache 在收到 str_replace/write_new_file 成功后 invalidate 该 path 的 ranges |
| 第一次给 full file 让小文件占太多 context | 1500 行阈值，超过自动 partial |
| `already-read ranges` 注入打乱 user_redesign preamble | preamble 在最前，already-read 块在 middle，task_request 在最后；通过单元测试锁定位置 |
| LLM 把"已读"理解成"不要再 reason"导致漏读真需要的部分 | 只阻塞"完全相同 range 的 re-read"；不同 range 仍允许 |

## 实施顺序

1. 改动 1（1 行注释改动）— 30 秒
2. 改动 2（read cache 模块 + runtime 接入） — 1 小时含测试
3. 改动 3（user_prompt 注入） — 30 分钟含测试
4. 改动 4（first-read full file） — 30 分钟含测试
5. 跑 T5 实战验证

不需要改 `escalation_handler` 或 `engine_recovery_mixin`——这些都是 executor 层内部改动，与上报机制正交。

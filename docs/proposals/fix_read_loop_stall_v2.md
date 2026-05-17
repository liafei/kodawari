# 修复方案 v2：减少 read 循环导致的 EXECUTOR_STALLED_NO_WRITE_PROGRESS

> v2 吸纳了 Plan agent 与 Explore agent 的独立评审反馈。修复了 v1 的 4 项 BLOCKING/HIGH：
> - Change 2 的 contract 不返回 content 会破坏 read_file 共享 handler 的契约
> - Change 3 注入点错误（user_prompt 只在 session 启动时跑一次，不会刷新）
> - Change 4 阈值自相矛盾（1500 行 > 24K bytes，会被 compactor 立刻打回）
> - v1 把接入点写成 `_handle_read_file_partial`（不存在）；实际是 `tool_use_runtime._tool_dispatch` 字典里的 lambda → `_read_file()` 共享 handler

## 修订后的根因比例（按 Plan agent 反馈）

| 因子 | 占比 |
|------|-----:|
| prompt 设计本身鼓励重读（compaction 注释 + 多条互相矛盾的指令） | 60% |
| stall 检测 key 用 exact `offset:limit`，相邻偏移即可绕过 | 25% |
| 模型推理深度（flash 比 pro 更易陷入） | 15% |

## 改动总览（6 项，从小到大）

| # | 改动 | 文件:line | 来源 |
|---|------|----------|------|
| 1 | compaction stamp 改为"已读 + 引用 line 范围"正向描述 | tool_use_prompt.py:141 | v1 改进 |
| 2 | read cache：cache 命中时返回**真实** content + 内部标记 `_workflow_cache_hit`，递增 wasted_read_count | tool_use_runtime.py:_read_file (line 661) + 新模块 | v1 改进（Plan: BLOCKING） |
| 3 | per-iteration 注入 "已读 ranges" 到 tool-result message，**不是** user_prompt | execution_openai_tool_use.py:880-932 | v1 修正（Plan+Explore 一致） |
| 4 | 第一次 read 自动给 full file，阈值 **400 行**（≤ 24K bytes budget） | tool_use_runtime.py:_read_file | v1 修正 |
| 5 | tool_use_stall.py read_signatures key 改为 **range-overlap matching**（50% 重叠映射同 bucket） | tool_use_stall.py:67 | Plan 新建议 |
| 6 | `wasted_read_count > 0` 时把 no_write_threshold 从 4 降到 **2** | tool_use_stall.py + tool_use_runtime.py | Plan 新建议 |

---

## 改动 1：compaction instruction 改为正向描述

**位置**：`tool_use_prompt.py:141`（在 `compact_tool_result_content` 函数里）

**改前**：
```python
"instruction": "This older read/search result was compacted to keep the tool loop small. Call search_file or read_file_partial again if exact text is needed."
```

**改后**：
```python
"instruction": (
    f"Compacted result for {path} lines {start}-{end}. "
    "You have already read this range; cite line numbers from your prior context. "
    "To read DIFFERENT lines, call read_file_partial with a non-overlapping offset."
),
```

依据：Plan agent 指出 DeepSeek/GPT 对负向指令（"do NOT re-read"）跟从弱；正向描述（"You have already read"）跟从强。同时把 path/start/end 注入 stamp 里，让模型有 anchor 能引用。

`start/end` 从 `payload.get("offset")` / `payload.get("offset")+payload.get("limit")` 计算（已经存在于 compaction 输入 dict 里，见 line 143-144）。

---

## 改动 2：ReadCache + cache hit 返回真实内容（修复 BLOCKING）

**新文件**：`kodawari/autopilot/execution/tool_use_read_cache.py`

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class CacheDecision:
    """What the cache says about a (path, offset, limit) query."""
    is_hit: bool             # 完全被已读 ranges 覆盖
    cached_start: int = 0    # 已读到的最小 offset
    cached_end: int = 0      # 已读到的最大 offset+limit
    overlap_ratio: float = 0.0  # 新查询和已有的最大重叠比例 (0..1)

@dataclass
class ReadCache:
    """Per-session read-range tracker.

    Records what (path, [start, end)) ranges have been served. Returns
    CacheDecision so the runtime can decide whether to short-circuit.

    Critical contract: the runtime ALWAYS returns real content to the model
    (re-reading from disk is cheap), but tags cache hits internally with
    `_workflow_cache_hit: true`. The stall tracker uses that tag to
    increment wasted_read_count and trigger Change 6 earlier.
    """
    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def check(self, path: str, offset: int, limit: int) -> CacheDecision:
        end = offset + max(limit, 1)
        existing = self.ranges.get(path, [])
        if not existing:
            return CacheDecision(is_hit=False)
        best_overlap = 0.0
        cached_start, cached_end = 0, 0
        new_size = end - offset
        for s, e in existing:
            inter_s = max(s, offset)
            inter_e = min(e, end)
            if inter_e <= inter_s:
                continue
            overlap = (inter_e - inter_s) / new_size
            if overlap > best_overlap:
                best_overlap = overlap
                cached_start, cached_end = s, e
        return CacheDecision(
            is_hit=best_overlap >= 0.95,  # ≥95% covered counts as full hit
            cached_start=cached_start,
            cached_end=cached_end,
            overlap_ratio=best_overlap,
        )

    def record(self, path: str, offset: int, limit: int) -> None:
        end = offset + max(limit, 1)
        existing = self.ranges.setdefault(path, [])
        existing.append((offset, end))
        existing.sort()
        # merge overlapping ranges
        merged: list[tuple[int, int]] = []
        for s, e in existing:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        self.ranges[path] = merged

    def invalidate(self, path: str) -> None:
        """Drop all known ranges for path. Called on any write/delete attempt."""
        self.ranges.pop(path, None)

    def summary_for_prompt(self, max_entries: int = 30, max_chars: int = 1500) -> list[str]:
        """Return ['file.py: lines 1-100, 150-300', ...] capped at limits."""
        out: list[str] = []
        total_chars = 0
        for path, rngs in self.ranges.items():
            rng_str = ", ".join(f"lines {s}-{e}" for s, e in rngs)
            line = f"{path}: {rng_str}"
            if total_chars + len(line) > max_chars or len(out) >= max_entries:
                out.append(f"… plus {len(self.ranges) - len(out)} more files")
                break
            out.append(line)
            total_chars += len(line)
        return out
```

**接入点**（修正 v1 错误）：`tool_use_runtime.py` 里的 `_read_file()` 共享 handler（约 line 661），因为 `_tool_dispatch` (line 434-481) 把 `read_file` 和 `read_file_partial` 都路由到这个函数。

```python
# Pseudocode for _read_file
def _read_file(self, args, *, partial: bool):
    path = args["path"]
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 0)

    decision = self.read_cache.check(path, offset, limit)
    actual_result = self._read_file_from_disk(path, offset, limit)  # 原逻辑不变

    if decision.is_hit:
        # 仍返回真实 content，但内部标记。stall tracker 会消费这个 flag。
        actual_result["_workflow_cache_hit"] = True
        actual_result["_cache_hit_overlap"] = decision.overlap_ratio
        actual_result["instruction"] = (
            f"You have already read {path} lines {decision.cached_start}-{decision.cached_end}. "
            "This result is served from disk again only because you re-requested it. "
            "Refer to your prior context — do not re-read this range."
        )

    self.read_cache.record(path, offset, limit)
    return actual_result
```

**stall tracker 接入**（`tool_use_stall.py` 的 `record_tool_result`）：

```python
def record_tool_result(self, name: str, result: dict[str, Any]) -> None:
    # 原有 patch-failure 计数逻辑保留
    ...
    if name in {"read_file", "read_file_partial"} and result.get("_workflow_cache_hit"):
        self.wasted_read_count += 1
        if self.wasted_read_count > _cap(self.config, "max_wasted_reads", 3):
            self._raise(
                "EXECUTOR_STALLED_REDUNDANT_READS",
                f"executor served {self.wasted_read_count} cache-hit reads without writing",
            )
```

**cache 失效**（覆盖所有 mutation handler）：

修改 `_str_replace()`（line 795-923）、`_write_file()`（line 961-982）、`_delete_file()`（line 984-993）、`apply_patch_plan_item`（line 65 in tool_use_patch_plan.py），**在 attempt 开始时**（不是成功后）调用：

```python
self.read_cache.invalidate(path)
```

理由：失败的 str_replace 也可能改了磁盘内容（partial write）；attempt 时 invalidate 比 success 后 invalidate 更安全。

---

## 改动 3：per-iteration 注入 already-read（修复 BLOCKING）

v1 错误：把 already-read 注入到 `user_prompt()`，但 `user_prompt()` 只在 session 启动时跑一次（见 `execution_openai_tool_use.py:830-833`）。

**v2 修复**：注入点改到 `messages_for_payload`（每次 LLM 调用前都会跑），作为一个合成的 system reminder 追加在最后。

**位置**：`tool_use_prompt.py` 的 `messages_for_payload` 函数（line 57+）

```python
def messages_for_payload(messages, runtime, ...):
    payload_messages = []
    # ...原有压缩逻辑...

    # NEW: append a fresh "already-read" reminder before the last assistant turn
    already_read = runtime.read_cache.summary_for_prompt()
    if already_read:
        reminder = (
            "Already-read file ranges (DO NOT re-read these):\n"
            + "\n".join(f"  - {line}" for line in already_read)
        )
        payload_messages.append({
            "role": "system",
            "content": reminder,
        })
    return payload_messages
```

成本：≤ 1500 chars / call，约 400 tokens，可接受。

---

## 改动 4：第一次 read full file（阈值 400 行）

**位置**：`tool_use_runtime.py:_read_file()`

```python
def _read_file(self, args, *, partial: bool):
    path = args["path"]
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 0)

    # First read of this path, no offset/limit specified, file small → give full file
    if (
        path not in self.read_cache.ranges
        and offset == 0
        and limit == 0
    ):
        line_count = self._file_line_count(path)
        if line_count <= 400:  # 400 行 × ~60 bytes/line ≈ 24K bytes budget
            args = {**args, "offset": 0, "limit": line_count}
            partial = False

    # ...原有 read 逻辑...
```

**阈值 400 行的依据**：
- Plan agent 验证 `max_full_read_tool_result_bytes = 24_000` (tool_use_prompt.py:65)
- 400 行 × 平均 60 bytes/line = 24K bytes，刚好不被 compactor 压缩
- newsapp 60% 的源文件 < 400 行；剩下 40% 走 partial read 没影响

**比 v1 的 1500 行更保守**——因为 v1 的 1500 行会被 24K compactor 立刻打回，自相矛盾。

---

## 改动 5：stall key 用 range-overlap matching

**位置**：`tool_use_stall.py:64-73`

**改前**：
```python
def record_tool_call(self, name, arguments):
    normalized = str(arguments.get("path") or "").replace("\\", "/")
    if name in {"read_file", "read_file_partial", "get_file_hash"}:
        signature = f"{name}:{normalized}:{int(arguments.get('offset') or 0)}:{int(arguments.get('limit') or 0)}"
        count = self._increment(self.read_signatures, signature)
        if count > _cap(self.config, "max_redundant_read_count", 8):
            self._raise("EXECUTOR_STALLED_REDUNDANT_READS", ...)
```

**改后**：
```python
def record_tool_call(self, name, arguments):
    normalized = str(arguments.get("path") or "").replace("\\", "/")
    if name in {"read_file", "read_file_partial", "get_file_hash"}:
        offset = int(arguments.get("offset") or 0)
        limit = int(arguments.get("limit") or 0)
        # Bucket reads by 200-line windows — adjacent offsets map to same bucket
        bucket = (name, normalized, offset // 200)
        count = self._increment(self.read_signatures, str(bucket))
        if count > _cap(self.config, "max_redundant_read_count", 8):
            self._raise("EXECUTOR_STALLED_REDUNDANT_READS", ...)
```

依据：200 行的桶宽足够区分"读不同段落"vs"对同一段反复 partial read"。Plan agent 指出 +1 byte offset 即可绕过 exact match — bucket 化是最低开销的修复。

---

## 改动 6：wasted_read_count > 0 时收紧 no-write 阈值

**位置**：`tool_use_stall.py.enforce_no_write_progress`（搜 `last_write_iteration` 找具体位置）

```python
def enforce_no_write_progress(self, iteration: int) -> None:
    # 原阈值
    threshold = _cap(self.config, "max_no_write_iterations", 4)
    # 一旦发现有 cache-hit reads，给更紧的窗口
    if self.wasted_read_count > 0:
        threshold = min(threshold, 2)
    if iteration - self.last_write_iteration > threshold:
        self._raise("EXECUTOR_STALLED_NO_WRITE_PROGRESS", ...)
```

效果：一旦执行器开始"白读"，立刻进入紧迫窗口，让 deterministic recovery 更早接管，不需要等满 4 个空窗。

---

## 不动的事

- `max_redundant_read_count = 8` 默认值不变（作为兜底；range-bucket 已经更容易触发它）
- `max_read_windows_per_path = 8`（fragmented_reads 兜底）
- tool_manifest 与 tool_schemas 关系：Explore agent 验证两者本来就一致（manifest 由 active_tools() 决定），v1 误判为 bug，**v2 不动**
- 不删 read_file_partial 工具（codex_cli / claude_code 路径还要用）

---

## 风险（v2 修订）

| 风险 | 缓解 |
|------|------|
| 失败的 str_replace 仍可能部分写盘 → cache 服旧内容 | invalidate 在 **attempt** 开始时，非成功后 |
| `apply_patch_plan_item`、`delete_file` 等其他 mutation handler 没 invalidate | v2 显式覆盖这 4 个 handler |
| user_prompt 静态 → already-read 注入 user_prompt 会过期 | v2 改注入到 `messages_for_payload` 每次都新建 |
| 400 行阈值仍过大某些情况 | 400 × 60 bytes ≈ 24K，刚好等于 compactor 上限；如果实测发现某文件每行特别长（如 SQL 长字符串）再降到 300 |
| `bucket = offset // 200` 在 401 行文件上把整个文件归 3 个桶，可能误伤 | max_redundant_read_count=8 给足容错；同桶 8 次才触发，正常 read 不会撞 |
| 第一次 read 返回 400 行后 partial 仍可调 | self.read_cache 已 record [0, 400)，下次 partial(0, 100) 落在 cache 内，正常服务但 wasted_read_count +=1 |

---

## 验证计划

1. **单元测试**（≥ 6 个）：
   - `test_read_cache_full_hit` — 完全覆盖
   - `test_read_cache_partial_overlap` — 50% 重叠不算 hit (overlap=0.5 < 0.95)
   - `test_read_cache_disjoint` — 完全不重叠
   - `test_read_cache_invalidate_on_str_replace_attempt`
   - `test_stall_bucket_adjacent_offsets` — offset=0 / 100 / 200 撞同桶
   - `test_first_read_full_file_under_400_lines`

2. **集成测试**：用 deepseek-v4-flash 重跑 T5，预期：
   - read_file_partial 调用数 ≤ 30（基线 459，降幅 93%）
   - wasted_read_count 触发的 STALL_REDUNDANT_READS ≤ 1
   - T5 单 cycle 完成或最多 2 个 attempt

3. **回归**：跑 T2 user_redesign 路径，确保 `_workflow_cache_hit` 标记不破坏 user_redesign preamble 注入顺序

---

## 实施顺序

1. Change 1（1 行字面改） — 5 分钟
2. 新建 `tool_use_read_cache.py` + 单元测试 — 90 分钟
3. Change 2（_read_file 接入 + invalidate 4 个 handler） — 60 分钟
4. Change 3（messages_for_payload 注入） — 30 分钟
5. Change 4（first-read full file） — 30 分钟
6. Change 5（bucket key） — 15 分钟
7. Change 6（threshold gating） — 15 分钟
8. 集成实战 T5 — 30 分钟

总计 ~5 小时含测试。

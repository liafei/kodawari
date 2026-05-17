# 修复方案 v3.1：减少 read 循环导致的 EXECUTOR_STALLED_NO_WRITE_PROGRESS

> **v3.1 changelog** — 从第 3 轮 Explore agent fact-check 修补 2 项 CRITICAL：
> - **[CRITICAL fix]** `messages_for_payload` 真实签名是 `(messages, config, *, compact_all=False, cap_fn)`，v3 写错了（漏 `cap_fn`）。S3 改为：保留 `cap_fn` 必填、新增 `runtime=None` 可选；wrapper `_messages_for_payload` 也加 `runtime` 转发
> - **[CRITICAL fix]** v3 主体未明确 `runtime.read_cache` 在哪里实例化；新增 **S4** 支持修复：在 `ToolUseRuntime.__post_init__` (line 209-218) 添加 `self.read_cache = ReadCache()`
> - **[MINOR fix]** `_file_line_count` 早停 401 行避免大文件浪费 I/O（Plan agent 建议）
>
> v3.1 主体改动数从 6+3 升到 6+4（S1-S4）。
>
> **v3 changelog** — 从 v2 修复以下 BLOCKING / HIGH：
> - **[BLOCKING]** `wasted_read_count` 字段未在 `StallDetector` dataclass 中定义（Explore agent 第 6 项）
> - **[BLOCKING]** `_file_line_count(path)` 方法在 runtime 中不存在；v2 假设它存在但没说明（Explore agent hazard 5）
> - **[BLOCKING]** `messages_for_payload(messages, config, compact_all)` 不接收 runtime 参数；v2 直接调用 `runtime.read_cache.summary_for_prompt()` 会 NameError（Explore agent hazard 3）
> - **[MEDIUM]** 外部 mutation 防御缺失 — 用 mtime check（Plan agent 新增）
> - **[MEDIUM]** 第一次 auto-expand 给 full file 后，紧跟的 partial 会被算 wasted 但模型不知道为什么（Plan agent 新增）
>
> v2 review 中 Explore agent 关于 `user_prompt` 调用频率的说法（"per-iteration"）经现场核实是它 v2 自己 review 时的笔误；执行点 `execution_openai_tool_use.py:830-833` 显示 `_user_prompt(runtime)` 仅在 for-loop **之前**调用一次。v2 改动 3 把注入点改到 `messages_for_payload`（for-loop 内每轮调用）是正确的。

---

## 改动总览（6 项 + 3 项支持修复）

| # | 改动 | 文件:line |
|---|------|----------|
| 1 | compaction stamp 改正向 + 含 line range | tool_use_prompt.py:141 |
| 2 | ReadCache：cache 命中返回真实 content + `_workflow_cache_hit` 内部 flag | tool_use_runtime.py:_read_file (~661) + 新 tool_use_read_cache.py |
| 3 | per-iteration 注入已读 ranges 到 messages_for_payload | tool_use_prompt.py:57-88 |
| 4 | 第一次 read 自动给 full file（≤ 400 行） | tool_use_runtime.py:_read_file |
| 5 | stall key 改 200-行 bucket | tool_use_stall.py:67 |
| 6 | `wasted_read_count > 0` 时把 no-write threshold 收紧到 2 | tool_use_stall.py:enforce_no_write_progress |
| **S1** | **新增** `wasted_read_count: int = 0` 到 `StallDetector` dataclass | tool_use_stall.py:18-36 |
| **S2** | **新增** `_file_line_count(path)` helper（轻量行数，401 行早停） | tool_use_runtime.py |
| **S3** | **修签名** `messages_for_payload(messages, config, *, compact_all=False, cap_fn, runtime=None)`；调用点 line 854 加传 runtime；wrapper `_messages_for_payload` 同步加 `runtime` 转发 | tool_use_prompt.py:57 + execution_openai_tool_use.py:854,952 |
| **S4** | **新增** `self.read_cache = ReadCache()` 到 `ToolUseRuntime.__post_init__` (line 209-218) | tool_use_runtime.py:209-218 |

---

## 改动 1：compaction stamp（不变）

`tool_use_prompt.py:141`：

```python
"instruction": (
    f"Compacted result for {path} lines {start}-{end}. "
    "You have already read this range; cite line numbers from your prior context. "
    "To read DIFFERENT lines, call read_file_partial with a non-overlapping offset."
),
```

---

## 改动 2：ReadCache + cache-hit 返回真实 content（修订：加 mtime）

**新文件**：`kodawari/autopilot/execution/tool_use_read_cache.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class CacheDecision:
    is_hit: bool
    cached_start: int = 0
    cached_end: int = 0
    overlap_ratio: float = 0.0
    stale_mtime: bool = False  # NEW: file changed on disk since recorded

@dataclass
class ReadCache:
    """Per-session range tracker. Records what (path, [start, end)) has been
    served. Returns real content always (re-read from disk is cheap); the
    runtime tags cache hits with `_workflow_cache_hit` for the stall counter."""
    ranges: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    mtimes: dict[str, float] = field(default_factory=dict)  # NEW: external-mutation defense

    def check(self, project_root: Path, path: str, offset: int, limit: int) -> CacheDecision:
        end = offset + max(limit, 1)
        existing = self.ranges.get(path, [])
        # NEW: if file mtime changed since we last recorded, treat as fresh
        try:
            cur_mtime = (project_root / path).stat().st_mtime
        except OSError:
            cur_mtime = 0.0
        prev_mtime = self.mtimes.get(path)
        if prev_mtime is not None and cur_mtime != prev_mtime:
            self.invalidate(path)
            return CacheDecision(is_hit=False, stale_mtime=True)
        if not existing:
            return CacheDecision(is_hit=False)
        best_overlap = 0.0
        cs, ce = 0, 0
        new_size = end - offset
        for s, e in existing:
            inter_s, inter_e = max(s, offset), min(e, end)
            if inter_e <= inter_s:
                continue
            r = (inter_e - inter_s) / new_size
            if r > best_overlap:
                best_overlap, cs, ce = r, s, e
        return CacheDecision(is_hit=best_overlap >= 0.95, cached_start=cs, cached_end=ce, overlap_ratio=best_overlap)

    def record(self, project_root: Path, path: str, offset: int, limit: int) -> None:
        end = offset + max(limit, 1)
        existing = self.ranges.setdefault(path, [])
        existing.append((offset, end))
        existing.sort()
        merged: list[tuple[int, int]] = []
        for s, e in existing:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        self.ranges[path] = merged
        try:
            self.mtimes[path] = (project_root / path).stat().st_mtime
        except OSError:
            pass

    def invalidate(self, path: str) -> None:
        self.ranges.pop(path, None)
        self.mtimes.pop(path, None)

    def summary_for_prompt(self, max_entries: int = 30, max_chars: int = 1500) -> list[str]:
        out: list[str] = []
        total = 0
        items = list(self.ranges.items())
        for i, (path, rngs) in enumerate(items):
            rng_str = ", ".join(f"lines {s}-{e}" for s, e in rngs)
            line = f"{path}: {rng_str}"
            if total + len(line) > max_chars or len(out) >= max_entries:
                out.append(f"… plus {len(items) - i} more file(s)")
                break
            out.append(line)
            total += len(line)
        return out
```

**接入 `_read_file()` (tool_use_runtime.py:~661)**：

```python
def _read_file(self, args, *, partial: bool):
    path = args["path"]
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 0)

    # Change 4 auto-expand for first read of small file (in-line, no separate function)
    auto_expanded = False
    if (
        path not in self.read_cache.ranges
        and offset == 0
        and limit == 0
    ):
        n = self._file_line_count(path)  # S2 helper
        if n and n <= 400:
            offset, limit = 0, n
            auto_expanded = True

    decision = self.read_cache.check(self.project_root, path, offset, limit)
    actual_result = self._read_file_from_disk(path, offset, limit)  # 原逻辑

    if decision.is_hit:
        actual_result["_workflow_cache_hit"] = True
        actual_result["_cache_hit_overlap"] = decision.overlap_ratio
        actual_result["instruction"] = (
            f"You already read {path} lines {decision.cached_start}-{decision.cached_end}. "
            "This range is re-served from disk only because you re-requested it. "
            "Refer to your prior context — do not re-read this range."
        )

    if auto_expanded:
        actual_result["instruction"] = (
            f"Full file returned ({limit} lines, ≤400 cap). "
            "DO NOT request partial reads of this file; the full content is above."
        )

    self.read_cache.record(self.project_root, path, offset, limit)
    return actual_result
```

**Cache invalidate 在 4 个 mutation handler 的 attempt 开始处**：

- `_str_replace` (tool_use_runtime.py:795+)
- `_write_file` (tool_use_runtime.py:961+)
- `_delete_file` (tool_use_runtime.py:984+)
- `apply_patch_plan_item` (tool_use_patch_plan.py:65)

每个开头加：
```python
if path:
    self.read_cache.invalidate(path)  # invalidate on attempt, not success
```

**StallDetector 接入** (`tool_use_stall.py:record_tool_result`)：

```python
def record_tool_result(self, name: str, result: dict[str, Any]) -> None:
    # 原 patch-failure 计数保留
    ...
    if name in {"read_file", "read_file_partial"} and result.get("_workflow_cache_hit"):
        self.wasted_read_count += 1
        if self.wasted_read_count > _cap(self.config, "max_wasted_reads", 3):
            self._raise(
                "EXECUTOR_STALLED_REDUNDANT_READS",
                f"executor served {self.wasted_read_count} cache-hit reads without writing",
            )
```

---

## 改动 3：per-iteration 注入到 messages_for_payload（修订：改签名）

**支持修复 S3** —— `messages_for_payload` 当前签名不接收 runtime，必须改：

`tool_use_prompt.py:57`（当前）：
```python
def messages_for_payload(messages, config, *, compact_all=False):
    ...
```

改为：
```python
def messages_for_payload(messages, config, *, compact_all=False, runtime=None):
    payload_messages = []
    # ...原有压缩逻辑不变...

    # NEW: append fresh "already-read" reminder as a system message
    if runtime is not None:
        already_read = runtime.read_cache.summary_for_prompt()
        if already_read:
            payload_messages.append({
                "role": "system",
                "content": (
                    "Already-read file ranges (DO NOT re-read these):\n"
                    + "\n".join(f"  - {ln}" for ln in already_read)
                ),
            })
    return payload_messages
```

**调用点修改** `execution_openai_tool_use.py:854`：

```python
"messages": _messages_for_payload(messages, runtime.config, compact_all=waf_compact_mode, runtime=runtime),
```

---

## 改动 4：第一次 read 给 full file（≤ 400 行）

逻辑已合并到 Change 2 的 `_read_file` 改动里。阈值 400 行 × ~60 bytes/line ≈ 24K bytes（compactor 上限）。

---

## 改动 5：stall key bucket（不变）

`tool_use_stall.py:67`：

```python
if name in {"read_file", "read_file_partial", "get_file_hash"}:
    offset = int(arguments.get("offset") or 0)
    bucket = (name, normalized, offset // 200)
    count = self._increment(self.read_signatures, str(bucket))
    if count > _cap(self.config, "max_redundant_read_count", 8):
        self._raise("EXECUTOR_STALLED_REDUNDANT_READS", ...)
```

---

## 改动 6：wasted_read_count > 0 时收紧 no-write threshold

`tool_use_stall.py.enforce_no_write_progress` (line 134-145)：

```python
def enforce_no_write_progress(self, iteration: int) -> None:
    threshold = _cap(self.config, "max_no_write_iterations", 4)
    if self.wasted_read_count > 0:
        threshold = min(threshold, 2)
    if iteration - self.last_write_iteration > threshold:
        self._raise("EXECUTOR_STALLED_NO_WRITE_PROGRESS", ...)
```

---

## 支持修复

### S1 — `wasted_read_count` 字段定义

`tool_use_stall.py:18-36`（在 `@dataclass StallDetector` 内）：

```python
@dataclass
class StallDetector:
    config: Any
    # ...existing fields...
    wasted_read_count: int = 0  # NEW: tracks cache-hit reads (set by record_tool_result)
```

### S2 — `_file_line_count(path)` helper（含 401 行早停）

新增到 `tool_use_runtime.py`（紧邻 `_read_file_from_disk`）：

```python
def _file_line_count(self, path: str) -> int:
    """Cheap line count via wc-style read. Returns 0 if missing/binary.

    Early-terminates at 401 lines: we only need to know "≤400 or not"
    for the Change 4 auto-expand decision. For a 100K-line file this
    saves O(n) → O(401) work.
    """
    try:
        full = self.project_root / path
        with full.open("rb") as f:
            count = 0
            for _ in f:
                count += 1
                if count >= 401:
                    return 401  # caller only needs the "≤400" bit
            return count
    except (OSError, UnicodeDecodeError):
        return 0
```

### S3 — `messages_for_payload` 签名 + 调用点（v3.1 修正）

**修正**：v3 写的当前签名 `(messages, config, *, compact_all)` 漏了 `cap_fn`。真实签名（Explore agent 核实 tool_use_prompt.py:57）：

```python
def messages_for_payload(messages, config, *, compact_all=False, cap_fn):
    ...
```

**v3.1 改为**（保留 `cap_fn` 必填、新增 `runtime=None` 可选）：

```python
def messages_for_payload(messages, config, *, compact_all=False, cap_fn, runtime=None):
    payload_messages = []
    # ...原有压缩逻辑（含 cap_fn 使用）不变...

    # NEW: append fresh "already-read" reminder as a system message
    if runtime is not None and getattr(runtime, "read_cache", None) is not None:
        already_read = runtime.read_cache.summary_for_prompt()
        if already_read:
            payload_messages.append({
                "role": "system",
                "content": (
                    "Already-read file ranges (DO NOT re-read these):\n"
                    + "\n".join(f"  - {ln}" for ln in already_read)
                ),
            })
    return payload_messages
```

**Wrapper 修改** `execution_openai_tool_use.py:952-953`：

```python
def _messages_for_payload(messages, config, *, compact_all=False, runtime=None):
    return messages_for_payload(messages, config, compact_all=compact_all, cap_fn=_cap, runtime=runtime)
```

**调用点** `execution_openai_tool_use.py:854`：

```python
"messages": _messages_for_payload(messages, runtime.config, compact_all=waf_compact_mode, runtime=runtime),
```

### S4 — `ToolUseRuntime.__post_init__` 实例化 read_cache（新增）

`tool_use_runtime.py:209-218`（在 `stall_detector` 实例化附近）：

```python
def __post_init__(self) -> None:
    # ...existing initialization (stall_detector etc.)...
    from kodawari.autopilot.execution.tool_use_read_cache import ReadCache
    self.read_cache = ReadCache()
```

`from` 在函数内 lazy import 避免循环依赖（ReadCache 模块本身不依赖 runtime）。

---

## 不动的事（同 v2）

- `max_redundant_read_count = 8` 作为兜底
- `max_read_windows_per_path = 8` (FRAGMENTED_READS 兜底)
- tool_manifest / tool_schemas 一致性（v1 误判，v2 确认不动）
- 不删 read_file_partial 工具

---

## 风险（v3 修订）

| 风险 | 缓解 |
|------|------|
| 失败 str_replace 写盘 → 服旧 | invalidate 在 attempt 开始时，非 success 后 |
| 外部 / git / shell 修改文件 → 服旧 | **mtime check in ReadCache.check** (S2 新增) |
| 95% 阈值过严 | 留作可调；先看实测，必要时改 0.5 加 softer counter |
| 400 行文件 partial read 立刻 wasted | auto-expanded result 已带显式 instruction "DO NOT request partial reads" |
| 200-行 bucket 对小文件粒度太粗 | max_redundant_read_count=8 给足缓冲；同桶 8 次才触发 |
| `read_cache` 模块未挂载到 runtime → AttributeError | runtime `__init__` 必须实例化 `self.read_cache = ReadCache()`；写在 S2 紧邻位置 |

---

## 实施顺序

1. S1 + S2 + S3 支持修复（dataclass 字段 / helper / 签名改） — 30 分钟
2. Change 1（compaction stamp 改写） — 5 分钟
3. 新建 `tool_use_read_cache.py` + 单元测试 — 60 分钟
4. Change 2 接入 `_read_file()` + 4 个 mutation handler invalidate — 45 分钟
5. Change 3 注入 messages_for_payload — 20 分钟
6. Change 4 已合并到 Change 2，无独立工作
7. Change 5 bucket key — 10 分钟
8. Change 6 threshold gating — 10 分钟
9. 跑集成测试（deepseek-v4-flash + T5） — 30 分钟

总计 ~3.5 小时含测试。

---

## 验证（同 v2 标准）

1. **单元测试** ≥ 8 个：
   - `test_read_cache_full_hit_with_mtime_match`
   - `test_read_cache_mtime_changed_invalidates`
   - `test_read_cache_partial_overlap_below_95_is_miss`
   - `test_read_cache_invalidate_on_str_replace_attempt`
   - `test_read_cache_invalidate_on_failed_str_replace`（关键）
   - `test_first_read_full_file_under_400_lines_returns_full`
   - `test_first_read_over_400_lines_uses_partial`
   - `test_stall_bucket_adjacent_offsets_collide`
   - `test_wasted_read_count_tightens_no_write_threshold`

2. **集成测试**：deepseek-v4-flash 重跑 T5
   - read_file_partial 调用数：基线 459 → 目标 ≤ 30
   - wasted_read_count 触发 STALL_REDUNDANT_READS ≤ 1 次
   - T5 单 cycle 通过

3. **回归**：T2 user_redesign 路径不破坏 preamble 注入顺序

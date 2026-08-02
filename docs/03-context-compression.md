# Context Compression — 上下文压缩机制

> 五阶段压缩管线 + Anti-thrashing | `context/compressor.py`

---

## 0. 上下文工程背景

### 为什么需要上下文压缩

2024-2026 年间，LLM 上下文窗口从 128K 迅速扩展到 1M tokens（Gemini 2.5 Pro、Claude 4 等）。更大的窗口并不意味着无需压缩：

1. **注意力稀释**（Lost-in-the-Middle）：长上下文中，LLM 对中间信息的检索准确率从 90%+ 降到不足 60%。压缩对话历史实际上是**提升信息密度**，而非单纯的削减。
2. **成本非线性增长**：prompt token 的计费与长度成正比，100 轮对话可能累积数十万 tokens，每次 LLM 调用都携带完整历史。
3. **延迟与缓存失效**：Anthropic 的 prompt caching 要求前缀完全匹配，长对话中前缀缓存命中率随历史增长急剧下降。

### 业界方案对比

| 方案 | 代表产品 | 核心思路 | 信息损失 | 实现复杂度 |
|------|----------|----------|----------|------------|
| **滑动窗口** | ChatGPT Web | 保留最近 N 轮，丢弃旧消息 | 高（丢失全局上下文） | 低 |
| **LLM 摘要** | Claude Code, Cursor | 调用 LLM 将历史压缩为结构化摘要 | 中（LLM 理解后重述） | 中 |
| **层次化记忆** | Mem0, Letta | 工作记忆 → 短期记忆 → 长期记忆三层 | 低（多粒度保留） | 高 |
| **增量摘要** | LangChain ConversationSummaryBufferMemory | 保留最近 K 轮 + 旧轮次摘要 | 中 | 中 |
| **结构化压缩** | **MiniHermes** | 5 阶段管线：边界 → 裁剪 → 摘要 → 修复 → 分裂 | 中低（保护 tool pair 原子性） | 中高 |

**MiniHermes 选择结构化压缩的原因**：AI 编程助手的对话包含大量工具调用（bash 执行结果、文件内容、diff 输出），这些内容的**结构化语义**不能简单丢弃——需要保留"做了什么、结果是什么、文件改了什么"等关键信息。

### 核心设计权衡

```
保真度（fidelity） vs. 压缩率（compression ratio）

滑动窗口 ──→ 保真度低，压缩率固定
LLM 摘要 ──→ 保真度中等，压缩率高（可达 90%+）
结构化压缩 ──→ 保真度较高，压缩率中等（50-70%）
```

MiniHermes 选择在"摘要"基础上增加边界保护和工具对修复，牺牲一定压缩率换取保真度。

---

## 1. 触发机制：双检查点模型

```
检查点 1（调 LLM 前）：
  estimate_tokens > threshold_tokens
  threshold_tokens = context_window × threshold_percent（默认 50%）

检查点 2（LLM 响应后）：
  用 API 返回的真实 prompt_tokens 更新 _last_prompt_tokens
  下次检查点 1 使用更精确的增量估算
```

**为什么双检查点：**
- 检查点 1 用估算值决策（此时消息已经准备好，必须决定是否压缩）
- 检查点 2 用真实值校正（提高下次估算精度）

---

## 2. 五阶段压缩流程

```
compress(history, db, session_id) → (compressed_messages, new_session_id)

Phase 1: 边界确定    → head + middle + tail
Phase 2: 工具输出裁剪 → 大型输出压缩为一行
Phase 3: LLM 摘要生成 → 结构化摘要替换 middle
Phase 4: Tool pair sanitization → 修复孤立/缺失的 tool pair
Phase 5: 组装 + 分裂  → 新消息列表 + 新 session
```

### Phase 1: 边界确定

```
[msg0][msg1] | [msg2]...[msg_n-5] | [msg_n-4]...[msg_n]
   ← HEAD →         ← MIDDLE →         ← TAIL →

HEAD:  前 protect_first_n 条（默认 2）— 不做压缩
TAIL:  尾 tail_ratio（默认 20%）— 保留最近上下文
MIDDLE: 剩余 → 待压缩
```

**边界对齐：**
- `_align_boundary_backward()`: 如果切在 tool pair 中间 → 向上调整
- `_ensure_last_user_in_tail()`: 确保最后一条 user 消息在 tail（API 要求）

### Phase 2: 工具输出裁剪

```
for each tool result in MIDDLE:
    if len(content) > 500:
        content = "[tool_name] preview... (N chars omitted)"
```

### Phase 3: LLM 摘要生成

**预算计算：**
```python
middle_tokens = _estimate_tokens(middle)
summary_budget = min(middle_tokens × 20%, 12000)  # 下限 1000
```

**摘要模式：**
- **首次压缩**: 12-section 结构化模板（Primary Request / Goal / Constraints / Completed Actions / Active State / In Progress / Key Decisions / Relevant Files / Remaining Work / Important User Messages / Critical Context / Next Steps）
- **迭代压缩**: 保留旧摘要 + 追加新 turns（RECOMPRESS_SYSTEM_PROMPT）

**调用参数：**
```python
temperature=0.3, stream=False  # 低温度、非流式
```

### Phase 4: Tool pair sanitization

- Case 1: 移除孤立 tool results（call_id 不在任何 assistant 中）
- Case 2: 为缺失 result 的 tool_call 插入 stub

### Phase 5: 组装 + session 分裂

```python
# 确定 summary role（避免两个同 role 相邻）
new_messages = head + [summary_msg] + tail

# Session 分裂
new_session_id = db.create_child_session(session_id)
# parent_session_id=旧ID, end_reason="compression"
```

---

## 3. Anti-thrashing 防抖动

```python
should_compress(token_estimate) -> bool:
    if token_estimate <= self.threshold_tokens:
        return False         # 没到阈值
    if self._cooldown_until and time.time() < self._cooldown_until:
        return False         # 在冷却期（60s）
    if self._ineffective_count >= 2:
        return False         # 连续 2 次节省 < 10%
    return True
```

**无效压缩判定：**
```python
saving_ratio = (before - after) / before
if saving_ratio < 0.10:
    self._ineffective_count += 1
else:
    self._ineffective_count = 0
```

---

## 4. 迭代式摘要（RECOMPRESS）

```
Round 1: 压缩 50 轮 → Summary A
Round 2: 检测到 Summary A + 新增 50 轮 → 保留 + 追加 → Summary A+B
Round 3: 检测到 Summary A+B + 新增 50 轮 → 保留 + 追加 → Summary A+B+C
```

避免多次压缩导致信息逐层丢失。

---

## 5. compress() 伪代码

```python
def compress(self, history, db, session_id):
    # Phase 1: 边界
    tail_budget = max(1, int(len(history) * _TAIL_RATIO))
    split_idx = self._align_boundary_backward(history, len(history) - tail_budget)
    split_idx = self._ensure_last_user_in_tail(history, split_idx)
    head = history[:_PROTECT_FIRST_N]
    middle = history[_PROTECT_FIRST_N:split_idx]
    tail = history[split_idx:]

    # Phase 2: 裁剪
    middle = [prune_or_truncate(msg) for msg in middle]

    # Phase 3: 摘要
    summary = self._generate_summary(middle, head, tail)
    before = self._estimate_tokens(history)
    after = self._estimate_tokens([summary])

    # Anti-thrashing
    if (before - after) / before < 0.10:
        self._ineffective_count += 1
    else:
        self._ineffective_count = 0

    # Phase 4: sanitize
    middle = self._sanitize_tool_pairs(middle)

    # Phase 5: 组装
    role = self._determine_summary_role(head, tail)
    new_messages = head + [{"role": role, "content": summary}] + tail
    new_session_id = db.create_child_session(session_id)

    return new_messages, new_session_id
```

---

## 附录：关键常量

| 常量 | 默认值 | 含义 |
|------|--------|------|
| CONTEXT_WINDOW | 1,000,000 | 上下文窗口 token 数 |
| _THRESHOLD_PERCENT | 0.50 | 触发阈值（窗口的 50%） |
| _TAIL_RATIO | 0.20 | 尾部保留比例 |
| _PROTECT_FIRST_N | 2 | 头部保护条数 |
| _SUMMARY_RATIO | 0.20 | 摘要预算（middle 的 20%） |
| _SUMMARY_TOKENS_CEILING | 12,000 | 摘要上限 |
| _PRUNE_THRESHOLD | 500 | 工具输出裁剪阈值（chars） |
| cooldown | 60s | 压缩失败冷却 |
| ineffective_limit | 2 | 连续无效上限 |
| temperature | 0.3 | 压缩 LLM 温度 |

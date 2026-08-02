# Memory System — 双轨道持久记忆

> 冻结快照 + Live 操作 | `tools/memory.py`

---

## 0. 记忆工程背景

### AI Agent 的记忆需求分层

人类认知中的记忆是多层次的。AI Agent 同样需要不同粒度的记忆系统：

| 层次 | 人类类比 | Agent 实现 | 生命周期 | 容量 |
|------|----------|-----------|----------|------|
| **工作记忆** | 当前思考 | 对话历史（messages） | 单次 session | 上下文窗口 |
| **短期记忆** | 最近经历 | 压缩后的摘要 | 跨 session（会话恢复） | 摘要预算 |
| **长期记忆** | 知识/习惯 | 持久化记忆文件 | 跨 session（永久） | 文件系统 |

### 业界记忆方案对比

| 方案 | 代表产品 | 存储方式 | 检索方式 | 优点 | 缺点 |
|------|----------|----------|----------|------|------|
| **文件记忆** | **MiniHermes**, Claude Code | Markdown 文件 | 注入 system prompt | 简单、可读、可编辑 | 容量受限，无法语义搜索 |
| **向量记忆** | Mem0, Letta, LangChain | 向量数据库 | 语义相似度检索 | 容量大，语义理解 | 引入向量 DB 依赖，检索噪声 |
| **图谱记忆** | Microsoft GraphRAG | 知识图谱 | 实体关系遍历 | 结构化推理 | 构建成本高，延迟大 |
| **Rules + Memory** | Cursor Rules | `.cursorrules` 文件 | 注入 system prompt | 确定性、可版本控制 | 静态，无自动更新 |
| **Hybrid** | OpenAI Deep Research | 向量 + 文件 + 摘要 | 多路召回 | 全面 | 复杂度高 |

**MiniHermes 选择文件记忆的原因**：
1. **零依赖**：纯文本文件，不需要向量数据库
2. **透明可控**：用户可以直接用编辑器查看/修改 `~/.minihermes/memory/MEMORY.md`
3. **容量即约束**：字符限制迫使 LLM 做优先级选择，避免记忆膨胀
4. **持续命中前缀缓存**：snapshot 机制确保同一 session 内 system prompt 不变

### 与 RAG 的关系

```
RAG（Retrieval-Augmented Generation）：
  Query → Embed → Retrieve top-K → Inject into prompt

MiniHermes Memory：
  Session Start → Load all memory → Inject into system prompt → Session End

区别：
- RAG: 按需检索，适合海量文档
- MiniHermes Memory: 全量注入，适合少量高价值事实（偏好、约定）
```

---

## 1. 双轨道架构

```
~/.minihermes/memory/
├── MEMORY.md   — Agent 笔记（上限 2200 chars）
└── USER.md     — 用户画像（上限 1375 chars）
```

## 2. MemoryStore 类设计

### 初始化

```python
@dataclass
class MemoryStore:
    memory_entries: list[str]   # MEMORY.md 的条目
    user_entries: list[str]     # USER.md 的条目
    _snapshot: dict             # 冻结副本（session 内不变）
```

### load_from_disk()

```python
def load_from_disk(self):
    self.memory_entries = self._read_file(MEMORY_PATH)  # 按 --- 分隔
    self.user_entries = self._read_file(USER_PATH)
    self._snapshot = {
        "memory": self.memory_entries.copy(),  # 浅拷贝
        "user": self.user_entries.copy(),
    }
```

## 3. 冻结快照的核心设计

```
Session 启动
  ├── MemoryStore.load_from_disk()
  │     ├── 读 MEMORY.md / USER.md
  │     └── 复制到 _snapshot（session 内永不修改）
  ├── format_for_system_prompt() → 注入 system prompt
  │     使用 _snapshot 中的数据（冻结副本）
  └── 工具调用 memory 操作 → 修改 live entries → 写盘
        但 _snapshot 不变 → system prompt 不变 → 前缀缓存继续命中
        下次 session 启动时重新 load → 新快照生效
```

**设计理由：**
1. system prompt 整个 session 不变 → 前缀缓存持续命中
2. 写盘即持久，下次 session 自动生效
3. 容量控制防止上下文膨胀
4. 简洁可读的文本格式

## 4. 容量控制

```python
MEMORY_CHAR_LIMIT = 2200   # MEMORY.md
USER_CHAR_LIMIT = 1375     # USER.md
```

操作前检查：
```python
def _check_capacity(self, target, new_entry):
    current = self._char_count(target)
    new_total = current + len(new_entry) + len(ENTRY_DELIMITER)
    if new_total > self._char_limit(target):
        return False
    return True
```

超过限制时返回错误并提示当前使用量。

## 5. 模块级单例

```python
_store = None

def get_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
        _store.load_from_disk()
    return _store
```

第一次调用时从磁盘加载，之后返回同一实例。

## 6. 工具注册

```python
@register({
    "type": "function",
    "function": {
        "name": "memory",
        "parameters": {
            "action": {"enum": ["add", "update", "delete", "view"]},
            "target": {"enum": ["memory", "user"]},
            "entry": "string (for add/update)",
            "index": "int (for update/delete)",
        }
    }
})
def memory_fn(action, target, entry=None, index=None):
    store = get_store()
    if action == "add":    return store.add(target, entry)
    if action == "update": return store.update(target, index, entry)
    if action == "delete": return store.delete(target, index)
    if action == "view":   return store.view(target)
```

Schema 描述中明确告知 LLM：修改在下个 session 的 system prompt 中生效。

## 7. 完整数据流

```
Session Start:
  MemoryStore.load_from_disk()
    → _snapshot = {"memory": [...], "user": [...]}
    → format_for_system_prompt() → system prompt

Agent 调用 memory 工具:
  → add/update/delete → live entries 变化 → _save() 写盘
  → _snapshot 不变 → system prompt 不变

Session End (exit/clear):
  → 不影响（已写盘）

Next Session Start:
  → load_from_disk() 重新读盘 → 新快照包含上次修改
```

# session_search — 历史会话搜索

代码: `tools/session_search.py` (80行)

---

## Schema 设计

```python
{
    "name": "session_search",
    "parameters": {
        "query": {"type": "string", "description": "Search query (omit to browse recent sessions)."},
        "limit": {"type": "integer", "default": 5, "description": "Max results (1-10)."}
    },
    "required": []
}
```

## 实现

```python
def session_search(query: str = None, limit: int = 5) -> str:
    limit = min(max(limit, 1), 10)  # clamp 1-10

    db = SessionDB()  # 新建连接（避免锁竞争）
    if query:
        return _search(db, query, limit)
    else:
        return _list_recent(db, limit)
```

## 关键设计点

### 双模式单函数

- **query 为空** → 浏览最近的会话
- **query 非空** → FTS5 全文搜索

一个函数两种行为，减少 tool schema 复杂度。

### FTS5 snippet 高亮

```python
# FTS5 snippet() 返回带标记的匹配片段
SELECT snippet(messages_fts, 0, '>>>', '<<<', '...', 40)
FROM messages_fts WHERE messages_fts MATCH ?
```

匹配词用 `>>>` 和 `<<<` 包裹。

### 结果分组

每个 session 最多 3 条匹配消息，按 session 分组展示。

### 重连 DB

每次调用创建新的 `SessionDB` 连接，避免与主 Agent 的写入连接冲突。

### limit 范围

1-10，默认 5。防止一次性返回过多结果占用上下文。

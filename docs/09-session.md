# 09 — 会话持久化与恢复

> SQLite WAL + FTS5 + session 分裂链 | `session/db.py`

---

## 1. SessionDB 初始化

```python
class SessionDB:
    def __init__(self):
        db_path = Path.home() / ".minihermes" / "state.db"
        self.conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,   # 多线程安全访问
            isolation_level=None,      # autocommit 模式
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        self._migrate()
        self._backfill_fts()
```

WAL 模式：写入不阻塞读取，支持多线程并发访问。

---

## 2. 增量迁移

`_migrate()` 按需添加列，而非全量重建：

```python
def _migrate(self):
    existing = {row[1] for row in self.conn.execute("PRAGMA table_info(sessions)")}
    if "parent_session_id" not in existing:
        self.conn.execute("ALTER TABLE sessions ADD COLUMN parent_session_id TEXT")
    if "end_reason" not in existing:
        self.conn.execute("ALTER TABLE sessions ADD COLUMN end_reason TEXT")
    # ... 更多列的增量添加
```

---

## 3. 数据库 Schema

### sessions 表
```sql
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT,
    model TEXT,
    model_config TEXT,       -- JSON
    system_prompt TEXT,
    started_at TEXT,
    ended_at TEXT,
    end_reason TEXT,          -- user_exit/clear/compression/resumed
    parent_session_id TEXT,   -- 压缩链
    title TEXT
)
```

### messages 表
```sql
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES sessions(id),
    role TEXT,                -- user/assistant/tool/system
    content TEXT,
    tool_calls TEXT,          -- JSON
    tool_call_id TEXT,
    tool_name TEXT,
    token_count INTEGER,
    finish_reason TEXT,
    msg_type TEXT             -- message/summary/system
)
```

### FTS 全文索引
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, content=messages, content_rowid=id
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
```

---

## 4. FTS 回填

```python
def backfill_fts(self):
    # 检查 messages_fts 是否为空
    count = self.conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    if count == 0:
        # 从 messages 表全量回填
        self.conn.execute(
            "INSERT INTO messages_fts(rowid, content) "
            "SELECT id, content FROM messages"
        )
```

---

## 5. 核心方法

### create_session

```python
def create_session(self, session_id, source="cli", model="", ...):
    self.conn.execute(
        "INSERT OR IGNORE INTO sessions (id, source, model, ...) VALUES (?, ?, ?, ...)",
        (session_id, source, model, ...)
    )
```

`INSERT OR IGNORE` 保证幂等：重复创建同一 session ID 不会失败。

### end_session

```python
def end_session(self, session_id, end_reason="user_exit"):
    self.conn.execute(
        "UPDATE sessions SET ended_at = ?, end_reason = ? "
        "WHERE id = ? AND ended_at IS NULL",
        (datetime.now().isoformat(), end_reason, session_id)
    )
```

`ended_at IS NULL` 防止重复关闭。

### append_message

```python
def append_message(self, session_id, msg: dict):
    self.conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, "
        "tool_call_id, tool_name, token_count, finish_reason, msg_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ...
    )
```

实时写入（非批量），保证崩溃后数据不丢失。

### get_messages_for_llm

```python
def get_messages_for_llm(self, session_id):
    # 1. 找到最新的 summary 消息（如果有）
    summary_row = self.conn.execute(
        "SELECT id FROM messages WHERE session_id = ? AND msg_type = 'summary' "
        "ORDER BY id DESC LIMIT 1", (session_id,)
    ).fetchone()

    # 2. 取 summary 之后的所有消息 + summary 本身
    if summary_row:
        return [summary_msg] + [msgs after summary]
    else:
        return all_msgs
```

---

## 6. Session 分裂（压缩链路）

```python
def create_child_session(self, parent_session_id):
    # 1. 结束父 session
    self.end_session(parent_session_id, end_reason="compression")

    # 2. 创建子 session
    child_id = generate_session_id()
    self.conn.execute(
        "INSERT INTO sessions (id, parent_session_id, ...) VALUES (?, ?, ...)",
        (child_id, parent_session_id, ...)
    )
    return child_id
```

**分裂链：**
```
session_A → 压缩 → end_reason="compression"
session_B → parent_session_id=session_A
  → 再次压缩
session_C → parent_session_id=session_B
```

### resolve_resume_session_id

```python
def resolve_resume_session_id(self, session_id):
    # 沿 parent_session_id 链走到最新子 session
    current = session_id
    while True:
        child = self.conn.execute(
            "SELECT id FROM sessions WHERE parent_session_id = ? "
            "AND end_reason = 'compression' LIMIT 1",
            (current,)
        ).fetchone()
        if not child:
            break
        current = child[0]
    return current
```

恢复 `/resume <id>` 时自动定位到最新的子 session。

---

## 7. 消息实时写入

- 每条消息（user/assistant/tool）立即写入 DB
- 写入不需要 WAL checkpoint（autocommit 模式）
- 崩溃恢复：DB 中已有所有已写入的消息

---

## 8. end_reason 枚举

| 值 | 含义 |
|----|------|
| user_exit | 用户正常退出 |
| clear | /clear 命令 |
| compression | 上下文压缩导致分裂 |
| resumed | 从其他 session 恢复 |

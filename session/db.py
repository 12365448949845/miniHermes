"""
SQLite 会话持久化。

对齐 hermes 的 hermes_state.py 设计（精简版）：
  - 创建/结束会话（含 model_config、system_prompt、end_reason）
  - 追加/读取消息（含 token_count、finish_reason）
  - Token 统计、工具调用计数
  - 列出/删除历史会话
"""

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

SESSION_DB_PATH = "~/.minihermes/state.db"
SESSION_LIST_LIMIT = 20
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'cli',
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    title TEXT,
    parent_session_id TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_calls TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    reasoning TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    msg_type TEXT NOT NULL DEFAULT 'normal'
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF content ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class _LockedCursor:
    """让 execute 返回的 cursor 也遵守同一把数据库锁。"""

    def __init__(self, cursor, lock: threading.RLock):
        self._cursor = cursor
        self._lock = lock

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def fetchmany(self, size=None):
        with self._lock:
            return self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)

    def __iter__(self):
        with self._lock:
            rows = list(self._cursor)
        return iter(rows)

    def close(self):
        with self._lock:
            return self._cursor.close()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _LockedConnection:
    """SQLite 单连接代理；短事务由 SessionDB._transaction 持有 RLock。"""

    def __init__(self, conn, lock: threading.RLock):
        self._conn = conn
        self._lock = lock

    def execute(self, *args, **kwargs):
        with self._lock:
            return _LockedCursor(self._conn.execute(*args, **kwargs), self._lock)

    def executemany(self, *args, **kwargs):
        with self._lock:
            return _LockedCursor(self._conn.executemany(*args, **kwargs), self._lock)

    def executescript(self, *args, **kwargs):
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self):
        with self._lock:
            return self._conn.commit()

    def rollback(self):
        with self._lock:
            return self._conn.rollback()

    def close(self):
        with self._lock:
            return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


class SessionDB:
    def __init__(self, db_path: str | Path | None = None):
        self._list_limit = SESSION_LIST_LIMIT
        self._db_lock = threading.RLock()

        raw_path = str(db_path or SESSION_DB_PATH)
        if raw_path != ":memory:":
            p = Path(raw_path).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            raw_path = str(p)
        raw_conn = sqlite3.connect(raw_path, isolation_level=None, check_same_thread=False)
        self._conn = _LockedConnection(raw_conn, self._db_lock)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self.backfill_fts()

    def _migrate(self):
        """按 PRAGMA user_version 执行可回滚的增量迁移。"""
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"state.db schema version {current} is newer than supported {SCHEMA_VERSION}"
            )

        while current < SCHEMA_VERSION:
            target = current + 1
            migration = getattr(self, f"_migrate_to_v{target}")
            with self._transaction():
                migration()
                self._conn.execute(f"PRAGMA user_version={target}")
            current = target

        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"state.db foreign key check failed: {violations[:3]}")

    def _migrate_to_v1(self):
        """增加多 Agent Task/Run/Event 状态表和消息 Run 归属。"""
        session_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "parent_session_id" not in session_cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT"
            )

        message_cols = {
            r[1] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "agent_run_id" not in message_cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN agent_run_id TEXT NULL"
            )

        statements = [
            """
            CREATE TABLE IF NOT EXISTS agent_tasks (
                task_id TEXT PRIMARY KEY,
                conversation_id TEXT,
                session_id TEXT,
                parent_task_id TEXT REFERENCES agent_tasks(task_id) ON DELETE SET NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                request_preview TEXT NOT NULL DEFAULT '',
                context_preview TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                last_run_id TEXT,
                created_at REAL NOT NULL,
                finished_at REAL,
                result_preview TEXT,
                error_code TEXT,
                error_message TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                parent_run_id TEXT REFERENCES agent_runs(run_id) ON DELETE SET NULL,
                conversation_id TEXT,
                start_session_id TEXT,
                end_session_id TEXT,
                attempt INTEGER NOT NULL,
                agent_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                tool_policy_json TEXT NOT NULL DEFAULT '{}',
                approval_mode TEXT NOT NULL DEFAULT 'interactive',
                max_iterations INTEGER NOT NULL DEFAULT 0,
                timeout_seconds REAL,
                iterations_used INTEGER NOT NULL DEFAULT 0,
                provider_attempts INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                completion_reason TEXT,
                error_code TEXT,
                error_message TEXT,
                UNIQUE(task_id, attempt)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                run_id TEXT REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_messages_agent_run ON messages(agent_run_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_tasks_session ON agent_tasks(session_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_agent_tasks_conversation ON agent_tasks(conversation_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_agent_tasks_parent ON agent_tasks(parent_task_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_conversation ON agent_runs(conversation_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_parent ON agent_runs(parent_run_id)",
            "CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_agent_events_task ON agent_events(task_id, created_at)",
            """
            CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE OF content ON messages BEGIN
                INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_active_task
            ON agent_runs(task_id)
            WHERE status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_main_runs_active_conversation
            ON agent_runs(conversation_id)
            WHERE agent_kind = 'main_turn'
              AND status IN ('RUNNING', 'CANCEL_REQUESTED')
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

    def _migrate_to_v2(self):
        """增加每个 tool_call 的结构化执行记录。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS tool_executions (
                execution_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                retryable INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                error_code TEXT,
                error_message TEXT,
                output_preview TEXT
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tool_executions_run_started
            ON tool_executions(run_id, started_at)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_executions_run_call
            ON tool_executions(run_id, tool_call_id)
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

    @contextmanager
    def _transaction(self):
        """Runtime 多语句状态迁移的短事务边界。"""
        with self._db_lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def create_session(
        self,
        session_id: str,
        model: str,
        model_config: str = None,
        system_prompt: str = None,
    ) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO sessions (id, source, model, model_config, system_prompt, started_at)
               VALUES (?, 'cli', ?, ?, ?, ?)""",
            (session_id, model, model_config, system_prompt, time.time()),
        )

    def end_session(self, session_id: str, end_reason: str = "user_exit") -> None:
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ? AND ended_at IS NULL",
            (time.time(), end_reason, session_id),
        )

    def create_child_session(
        self,
        parent_id: str,
        child_id: str,
        model: str,
        model_config: str = None,
        system_prompt: str = None,
    ) -> None:
        """压缩后创建子 session，同时结束 parent session。"""
        self.end_session(parent_id, end_reason="compression")
        self._conn.execute(
            """INSERT INTO sessions (id, source, model, model_config, system_prompt,
               started_at, parent_session_id)
               VALUES (?, 'cli', ?, ?, ?, ?, ?)""",
            (child_id, model, model_config, system_prompt, time.time(), parent_id),
        )

    def resolve_resume_session_id(self, session_id: str) -> str:
        """沿压缩链路走到最新的有消息的 session。"""
        current = session_id
        visited = set()
        while current not in visited:
            visited.add(current)
            cur = self._conn.execute(
                """SELECT id FROM sessions
                   WHERE parent_session_id = ? ORDER BY started_at DESC LIMIT 1""",
                (current,),
            )
            child = cur.fetchone()
            if not child:
                return current
            cur2 = self._conn.execute(
                "SELECT end_reason FROM sessions WHERE id = ?", (current,)
            )
            row = cur2.fetchone()
            if row and row[0] == "compression":
                current = child[0]
            else:
                return current
        return current

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_calls: list = None,
        tool_call_id: str = None,
        tool_name: str = None,
        reasoning: str = None,
        token_count: int = None,
        finish_reason: str = None,
        msg_type: str = "normal",
        agent_run_id: str = None,
    ) -> None:
        tc_json = json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None
        self._conn.execute(
            """INSERT INTO messages
               (session_id, role, content, tool_calls, tool_call_id, tool_name, reasoning,
                timestamp, token_count, finish_reason, msg_type, agent_run_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_id, role, content, tc_json, tool_call_id, tool_name, reasoning,
             time.time(), token_count, finish_reason, msg_type, agent_run_id),
        )
        self._conn.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )

    def increment_tool_calls(self, session_id: str, count: int = 1) -> None:
        self._conn.execute(
            "UPDATE sessions SET tool_call_count = tool_call_count + ? WHERE id = ?",
            (count, session_id),
        )

    def update_tokens(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> None:
        self._conn.execute(
            """UPDATE sessions SET
               input_tokens = input_tokens + ?,
               output_tokens = output_tokens + ?,
               reasoning_tokens = reasoning_tokens + ?
               WHERE id = ?""",
            (input_tokens, output_tokens, reasoning_tokens, session_id),
        )

    def get_messages(self, session_id: str) -> list[dict]:
        cur = self._conn.execute(
            """SELECT role, content, tool_calls, tool_call_id, tool_name, reasoning,
                      token_count, finish_reason, msg_type, agent_run_id
               FROM messages WHERE session_id = ? ORDER BY id""",
            (session_id,),
        )
        messages = []
        for row in cur.fetchall():
            (role, content, tc_json, tool_call_id, tool_name, reasoning,
             token_count, finish_reason, msg_type, agent_run_id) = row
            msg: dict = {"role": role}
            if content is not None:
                msg["content"] = content
            if tc_json:
                msg["tool_calls"] = json.loads(tc_json)
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            if tool_name:
                msg["tool_name"] = tool_name
            if reasoning:
                msg["_reasoning"] = reasoning
            if token_count is not None:
                msg["_token_count"] = token_count
            if finish_reason:
                msg["finish_reason"] = finish_reason
            if msg_type != "normal":
                msg["_msg_type"] = msg_type
            if agent_run_id:
                msg["_agent_run_id"] = agent_run_id
            messages.append(msg)
        return messages

    def get_messages_for_llm(self, session_id: str) -> list[dict]:
        """加载用于 LLM 的消息：反向遍历，遇到最近的 summary 停止。"""
        all_msgs = self.get_messages(session_id)

        summary_idx = None
        for i in range(len(all_msgs) - 1, -1, -1):
            if all_msgs[i].get("_msg_type") == "summary":
                summary_idx = i
                break

        if summary_idx is None:
            return all_msgs

        return [all_msgs[summary_idx]] + all_msgs[summary_idx + 1:]

    def list_sessions(self, limit: int = None) -> list[dict]:
        if limit is None:
            limit = self._list_limit
        cur = self._conn.execute(
            """SELECT id, source, model, model_config, system_prompt, started_at, ended_at,
                      end_reason, message_count, tool_call_count, input_tokens, output_tokens,
                      reasoning_tokens, title, parent_session_id
               FROM sessions ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        )
        return [
            {
                "id": row[0],
                "source": row[1],
                "model": row[2],
                "model_config": row[3],
                "system_prompt": row[4],
                "started_at": row[5],
                "ended_at": row[6],
                "end_reason": row[7],
                "message_count": row[8],
                "tool_call_count": row[9],
                "input_tokens": row[10],
                "output_tokens": row[11],
                "reasoning_tokens": row[12],
                "title": row[13],
                "parent_session_id": row[14],
            }
            for row in cur.fetchall()
        ]

    def get_last_session_id(self) -> str | None:
        cur = self._conn.execute(
            "SELECT id FROM sessions ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        return row[0] if row else None

    def resolve_conversation_id(self, session_id: str) -> str:
        """沿 parent_session_id 向上解析稳定的逻辑会话根 ID。"""
        current = session_id
        visited = set()
        while current and current not in visited:
            visited.add(current)
            row = self._conn.execute(
                "SELECT parent_session_id FROM sessions WHERE id = ?", (current,)
            ).fetchone()
            if not row or not row[0]:
                return current
            current = row[0]
        return session_id

    # ── Agent Runtime 状态存储 ──────────────────────────────────────

    def _append_agent_event(self, task_id: str, run_id: str | None,
                            event_type: str, payload: dict | None = None):
        self._conn.execute(
            """INSERT INTO agent_events
               (task_id, run_id, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, run_id, event_type,
             json.dumps(payload or {}, ensure_ascii=False), time.time()),
        )

    def create_agent_task(self, *, task_id: str, conversation_id: str | None,
                          session_id: str | None, parent_task_id: str | None,
                          kind: str, title: str, request_preview: str,
                          context_preview: str = "") -> dict:
        now = time.time()
        with self._transaction():
            self._conn.execute(
                """INSERT INTO agent_tasks
                   (task_id, conversation_id, session_id, parent_task_id, kind,
                    title, request_preview, context_preview, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                (task_id, conversation_id, session_id, parent_task_id, kind,
                 title, request_preview, context_preview, now),
            )
            self._append_agent_event(task_id, None, "task_created")
        return self.get_agent_task(task_id)

    def create_agent_run(self, *, run_id: str, task_id: str,
                         parent_run_id: str | None, conversation_id: str | None,
                         start_session_id: str | None, agent_kind: str,
                         model: str, tool_policy_json: str,
                         approval_mode: str, max_iterations: int,
                         timeout_seconds: float | None) -> dict:
        now = time.time()
        with self._transaction():
            row = self._conn.execute(
                "SELECT COALESCE(MAX(attempt), 0) FROM agent_runs WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            attempt = int(row[0]) + 1
            self._conn.execute(
                """INSERT INTO agent_runs
                   (run_id, task_id, parent_run_id, conversation_id,
                    start_session_id, end_session_id, attempt, agent_kind,
                    status, model, tool_policy_json, approval_mode,
                    max_iterations, timeout_seconds, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?)""",
                (run_id, task_id, parent_run_id, conversation_id,
                 start_session_id, start_session_id, attempt, agent_kind,
                 model, tool_policy_json, approval_mode, max_iterations,
                 timeout_seconds, now),
            )
            self._conn.execute(
                "UPDATE agent_tasks SET last_run_id = ? WHERE task_id = ?",
                (run_id, task_id),
            )
            self._append_agent_event(task_id, run_id, "run_queued")
        return self.get_agent_run(run_id)

    def start_agent_run(self, run_id: str, task_id: str):
        now = time.time()
        with self._transaction():
            run_update = self._conn.execute(
                """UPDATE agent_runs SET status = 'RUNNING', started_at = ?
                   WHERE run_id = ? AND task_id = ? AND status = 'QUEUED'""",
                (now, run_id, task_id),
            )
            task_update = self._conn.execute(
                """UPDATE agent_tasks SET status = 'RUNNING'
                   WHERE task_id = ? AND status = 'PENDING'""",
                (task_id,),
            )
            if run_update.rowcount != 1 or task_update.rowcount != 1:
                raise RuntimeError(f"illegal start transition for run {run_id}")
            self._append_agent_event(task_id, run_id, "run_started")

    def request_agent_run_cancel(self, run_id: str,
                                 completion_reason: str = "user_interrupt") -> str:
        now = time.time()
        with self._transaction():
            row = self._conn.execute(
                "SELECT task_id, status FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"unknown agent run: {run_id}")
            task_id, status = row
            if status == "QUEUED":
                self._conn.execute(
                    """UPDATE agent_runs SET status = 'CANCELLED', finished_at = ?,
                              completion_reason = ?
                       WHERE run_id = ? AND status = 'QUEUED'""",
                    (now, completion_reason, run_id),
                )
                self._conn.execute(
                    """UPDATE agent_tasks SET status = 'CANCELLED', finished_at = ?
                       WHERE task_id = ? AND status = 'PENDING'""",
                    (now, task_id),
                )
                self._append_agent_event(
                    task_id, run_id, "run_cancelled",
                    {"completion_reason": completion_reason},
                )
                self._append_agent_event(task_id, run_id, "task_cancelled")
                return "CANCELLED"
            if status == "RUNNING":
                update = self._conn.execute(
                    """UPDATE agent_runs SET status = 'CANCEL_REQUESTED',
                              completion_reason = ?
                       WHERE run_id = ? AND status = 'RUNNING'""",
                    (completion_reason, run_id),
                )
                if update.rowcount != 1:
                    raise RuntimeError(f"cancel transition lost for run {run_id}")
                self._append_agent_event(
                    task_id, run_id, "cancel_requested",
                    {"reason": completion_reason},
                )
                return "CANCEL_REQUESTED"
            return status

    def finish_agent_run(self, *, run_id: str, task_id: str, status: str,
                         completion_reason: str, end_session_id: str | None,
                         result_preview: str = "", error_code: str | None = None,
                         error_message: str | None = None,
                         iterations_used: int = 0, provider_attempts: int = 0,
                         prompt_tokens: int = 0, completion_tokens: int = 0,
                         reasoning_tokens: int = 0):
        terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"}
        if status not in terminal:
            raise ValueError(f"not a terminal run status: {status}")
        allowed_from = {
            "SUCCEEDED": ("RUNNING",),
            "FAILED": ("RUNNING", "CANCEL_REQUESTED"),
            "CANCELLED": ("CANCEL_REQUESTED",),
            "TIMED_OUT": ("CANCEL_REQUESTED",),
            "INTERRUPTED": ("RUNNING", "CANCEL_REQUESTED"),
        }[status]
        placeholders = ",".join("?" for _ in allowed_from)
        now = time.time()
        task_status = "SUCCEEDED" if status == "SUCCEEDED" else (
            "CANCELLED" if status == "CANCELLED" else "FAILED"
        )
        run_event = {
            "SUCCEEDED": "run_succeeded",
            "FAILED": "run_failed",
            "CANCELLED": "run_cancelled",
            "TIMED_OUT": "run_timed_out",
            "INTERRUPTED": "run_interrupted",
        }[status]
        task_event = {
            "SUCCEEDED": "task_succeeded",
            "CANCELLED": "task_cancelled",
        }.get(status, "task_failed")

        with self._transaction():
            update = self._conn.execute(
                f"""UPDATE agent_runs SET status = ?, end_session_id = ?,
                           finished_at = ?, completion_reason = ?, error_code = ?,
                           error_message = ?, iterations_used = ?, provider_attempts = ?,
                           prompt_tokens = ?, completion_tokens = ?, reasoning_tokens = ?
                       WHERE run_id = ? AND task_id = ?
                         AND status IN ({placeholders})""",
                (status, end_session_id, now, completion_reason, error_code,
                 error_message, iterations_used, provider_attempts, prompt_tokens,
                 completion_tokens, reasoning_tokens, run_id, task_id, *allowed_from),
            )
            if update.rowcount != 1:
                raise RuntimeError(f"illegal terminal transition for run {run_id} -> {status}")
            task_update = self._conn.execute(
                """UPDATE agent_tasks SET status = ?, finished_at = ?,
                          result_preview = ?, error_code = ?, error_message = ?
                   WHERE task_id = ? AND status = 'RUNNING'""",
                (task_status, now, result_preview, error_code, error_message, task_id),
            )
            if task_update.rowcount != 1:
                raise RuntimeError(f"illegal terminal transition for task {task_id}")
            self._append_agent_event(
                task_id, run_id, run_event,
                {"completion_reason": completion_reason},
            )
            self._append_agent_event(task_id, run_id, task_event)

    def reconcile_agent_runs(self) -> dict[str, int]:
        """启动时关闭上次进程遗留的非终态 Run，不自动重放任务。"""
        now = time.time()
        counts = {"interrupted": 0, "cancelled": 0}
        with self._transaction():
            rows = self._conn.execute(
                """SELECT run_id, task_id, status FROM agent_runs
                   WHERE status IN ('QUEUED', 'RUNNING', 'CANCEL_REQUESTED')"""
            ).fetchall()
            for run_id, task_id, status in rows:
                if status == "QUEUED":
                    terminal = "CANCELLED"
                    task_status = "CANCELLED"
                    reason = "process_restarted_before_start"
                    event = "run_cancelled"
                    counts["cancelled"] += 1
                else:
                    terminal = "INTERRUPTED"
                    task_status = "FAILED"
                    reason = "process_restarted"
                    event = "run_interrupted"
                    counts["interrupted"] += 1
                self._conn.execute(
                    """UPDATE agent_runs SET status = ?, finished_at = ?,
                              completion_reason = ? WHERE run_id = ?""",
                    (terminal, now, reason, run_id),
                )
                self._conn.execute(
                    """UPDATE agent_tasks SET status = ?, finished_at = ?,
                              error_code = ?, error_message = ?
                       WHERE task_id = ? AND status IN ('PENDING', 'RUNNING')""",
                    (task_status, now, reason, reason, task_id),
                )
                self._append_agent_event(
                    task_id, run_id, event, {"completion_reason": reason}
                )
                self._append_agent_event(
                    task_id,
                    run_id,
                    "task_cancelled" if task_status == "CANCELLED" else "task_failed",
                )
        return counts

    def get_agent_task(self, task_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._row_to_dict("agent_tasks", row)

    def get_agent_run(self, run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_dict("agent_runs", row)

    def list_agent_runs(self, conversation_id: str | None = None,
                        limit: int = 20) -> list[dict]:
        if conversation_id is None:
            rows = self._conn.execute(
                "SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM agent_runs WHERE conversation_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (conversation_id, limit),
            ).fetchall()
        return [self._row_to_dict("agent_runs", row) for row in rows]

    def list_agent_events(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM agent_events WHERE run_id = ? ORDER BY event_id", (run_id,)
        ).fetchall()
        events = [self._row_to_dict("agent_events", row) for row in rows]
        for event in events:
            event["payload"] = json.loads(event.pop("payload_json"))
        return events

    def append_agent_event(self, task_id: str, run_id: str | None,
                           event_type: str, payload: dict | None = None):
        """追加不包含完整参数或输出的 Runtime 审计事件。"""
        self._append_agent_event(task_id, run_id, event_type, payload)

    def create_tool_execution(self, *, execution_id: str, run_id: str,
                              tool_call_id: str, tool_name: str) -> dict:
        now = time.time()
        self._conn.execute(
            """INSERT INTO tool_executions
               (execution_id, run_id, tool_call_id, tool_name, status,
                attempts, retryable, created_at, started_at)
               VALUES (?, ?, ?, ?, 'RUNNING', 0, 0, ?, ?)""",
            (execution_id, run_id, tool_call_id, tool_name, now, now),
        )
        return self.get_tool_execution(execution_id)

    def finish_tool_execution(self, *, execution_id: str, status: str,
                              attempts: int, retryable: bool,
                              error_code: str | None,
                              error_message: str | None,
                              output_preview: str | None):
        terminal = {"SUCCEEDED", "FAILED", "DENIED", "CANCELLED"}
        if status not in terminal:
            raise ValueError(f"not a terminal tool status: {status}")
        update = self._conn.execute(
            """UPDATE tool_executions
               SET status = ?, attempts = ?, retryable = ?, finished_at = ?,
                   error_code = ?, error_message = ?, output_preview = ?
               WHERE execution_id = ? AND status = 'RUNNING'""",
            (
                status,
                max(0, int(attempts)),
                1 if retryable else 0,
                time.time(),
                error_code,
                error_message,
                output_preview,
                execution_id,
            ),
        )
        if update.rowcount != 1:
            raise RuntimeError(
                f"illegal terminal transition for tool execution {execution_id}"
            )

    def get_tool_execution(self, execution_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tool_executions WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        result = self._row_to_dict("tool_executions", row)
        if result:
            result["retryable"] = bool(result["retryable"])
        return result

    def list_tool_executions(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM tool_executions
               WHERE run_id = ? ORDER BY created_at, execution_id""",
            (run_id,),
        ).fetchall()
        results = [self._row_to_dict("tool_executions", row) for row in rows]
        for result in results:
            result["retryable"] = bool(result["retryable"])
        return results

    def _row_to_dict(self, table: str, row) -> dict | None:
        if row is None:
            return None
        columns = [
            item[1] for item in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        return dict(zip(columns, row))

    def set_title(self, session_id: str, title: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (title.strip()[:100], session_id),
        )

    def delete_session(self, session_id: str) -> None:
        """删除整个逻辑会话压缩链及其 Runtime 审计记录。"""
        conversation_id = self.resolve_conversation_id(session_id)
        rows = self._conn.execute(
            """WITH RECURSIVE session_chain(id) AS (
                   SELECT id FROM sessions WHERE id = ?
                   UNION ALL
                   SELECT s.id FROM sessions s
                   JOIN session_chain c ON s.parent_session_id = c.id
               )
               SELECT id FROM session_chain""",
            (conversation_id,),
        ).fetchall()
        session_ids = [row[0] for row in rows]
        if not session_ids:
            return
        placeholders = ",".join("?" for _ in session_ids)
        with self._transaction():
            # messages 的 DELETE trigger 同步移除 FTS5 索引。
            self._conn.execute(
                f"DELETE FROM messages WHERE session_id IN ({placeholders})",
                session_ids,
            )
            self._conn.execute(
                f"""DELETE FROM agent_tasks
                    WHERE conversation_id = ? OR session_id IN ({placeholders})""",
                (conversation_id, *session_ids),
            )
            self._conn.execute(
                f"DELETE FROM sessions WHERE id IN ({placeholders})",
                session_ids,
            )

    def search_messages(self, query: str, limit: int = 5) -> list[dict]:
        """FTS5 搜索消息内容，按 session 分组返回匹配片段。"""
        try:
            cur = self._conn.execute(
                """SELECT m.session_id, m.role, m.content, s.title, s.started_at,
                          snippet(messages_fts, 0, '>>>', '<<<', '...', 40) as snippet
                   FROM messages_fts
                   JOIN messages m ON m.id = messages_fts.rowid
                   JOIN sessions s ON s.id = m.session_id
                   WHERE messages_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit * 3),
            )
            rows = cur.fetchall()
        except Exception:
            return []

        # 按 session 分组，每个 session 最多保留 3 条匹配
        sessions: dict[str, dict] = {}
        for session_id, role, content, title, started_at, snippet in rows:
            if session_id not in sessions:
                if len(sessions) >= limit:
                    break
                sessions[session_id] = {
                    "session_id": session_id,
                    "title": title,
                    "started_at": started_at,
                    "matches": [],
                }
            if len(sessions[session_id]["matches"]) < 3:
                sessions[session_id]["matches"].append({
                    "role": role,
                    "snippet": snippet or (content[:200] if content else ""),
                })

        return list(sessions.values())

    def backfill_fts(self):
        """首次启动时回填已有消息到 FTS 表。"""
        content_count = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE content IS NOT NULL"
        ).fetchone()[0]
        indexed_count = self._conn.execute(
            "SELECT COUNT(*) FROM messages_fts_docsize"
        ).fetchone()[0]
        if indexed_count != content_count:
            self._conn.execute(
                "INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')"
            )

    def close(self):
        self._conn.close()

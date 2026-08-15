"""
SQLite 会话持久化。

对齐 hermes 的 hermes_state.py 设计（精简版）：
  - 创建/结束会话（含 model_config、system_prompt、end_reason）
  - 追加/读取消息（含 token_count、finish_reason）
  - Token 统计、工具调用计数
  - 列出/删除历史会话
"""

import json
import math
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

SESSION_DB_PATH = "~/.minihermes/state.db"
SESSION_LIST_LIMIT = 20
SCHEMA_VERSION = 13

_SNAPSHOT_CAPTURE_STATUSES = frozenset({
    "REPLAYABLE", "PARTIAL", "UNAVAILABLE",
})
_SNAPSHOT_ARTIFACT_STATUSES = frozenset({"AVAILABLE", "PURGED"})
_REPRODUCIBILITY_STATUSES = _SNAPSHOT_CAPTURE_STATUSES
_ARTIFACT_STATUSES = frozenset({"AVAILABLE", "INCOMPLETE", "PURGED"})
_LOG_STATUSES = frozenset({"COMPLETE", "TRUNCATED", "REDACTED", "UNAVAILABLE"})
_REPLAY_STATUSES = frozenset({
    "NOT_REQUESTED",
    "REPLAY_SUCCEEDED",
    "REPLAY_COMMAND_FAILED",
    "REPLAY_SETUP_FAILED",
    "REPLAY_DENIED",
    "REPLAY_UNAVAILABLE",
    "REPLAY_CANCELLED",
    "REPLAY_TIMED_OUT",
})
_REPRODUCIBLE_TOOL_NAMES = frozenset({"bash"})
_ARTIFACT_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WORKFLOW_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_WORKFLOW_RUN_ACTIVE_STATUSES = frozenset({"QUEUED", "RUNNING", "WAITING_HUMAN"})
_WORKFLOW_RUN_TERMINAL_STATUSES = frozenset({
    "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED",
})
_WORKFLOW_NODE_ACTIVE_STATUSES = frozenset({
    "PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_CHILDREN",
})
_WORKFLOW_NODE_TERMINAL_STATUSES = frozenset({
    "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED", "SKIPPED",
})
_WORKFLOW_NODE_KINDS = frozenset({"AGENT", "FUNCTION", "HUMAN_GATE", "JOIN"})
_WORKFLOW_GATE_STATUSES = frozenset({
    "WAITING", "APPROVED", "DENIED", "CANCELLED", "EXPIRED",
})
_WORKFLOW_STATE_MAX_BYTES = 64 * 1024
_WORKFLOW_SPECIAL_EDGE_IDS = frozenset({"__start__", "__end__"})
_WORKTREE_LEASE_STATUSES = frozenset({
    "PROVISIONING", "READY", "RUNNING", "PRESERVED", "INTEGRATING",
    "MERGED", "FAILED", "REJECTED",
})
_WORKTREE_CLEANUP_STATUSES = frozenset({"PENDING", "SUCCEEDED", "FAILED"})
_WORKTREE_INTEGRATION_STATUSES = frozenset({
    "PREPARING", "VERIFYING", "READY_TO_APPLY",
    "MERGED", "CONFLICT", "VERIFICATION_FAILED", "DENIED", "CANCELLED",
    "PRECONDITION_FAILED", "FAILED", "INTERRUPTED",
})
_WORKTREE_INTEGRATION_ACTIVE_STATUSES = frozenset({
    "PREPARING", "VERIFYING", "READY_TO_APPLY",
})
_WORKTREE_INTEGRATION_TRANSITIONS = {
    "PREPARING": frozenset({
        "VERIFYING", "CONFLICT", "DENIED", "CANCELLED",
        "PRECONDITION_FAILED", "FAILED", "INTERRUPTED",
    }),
    "VERIFYING": frozenset({
        "READY_TO_APPLY", "CONFLICT", "VERIFICATION_FAILED", "CANCELLED",
        "PRECONDITION_FAILED", "FAILED", "INTERRUPTED",
    }),
    "READY_TO_APPLY": frozenset({
        "MERGED", "CONFLICT", "DENIED", "CANCELLED",
        "PRECONDITION_FAILED", "FAILED", "INTERRUPTED",
    }),
    "MERGED": frozenset(),
    "CONFLICT": frozenset(),
    "VERIFICATION_FAILED": frozenset(),
    "DENIED": frozenset(),
    "CANCELLED": frozenset(),
    "PRECONDITION_FAILED": frozenset(),
    "FAILED": frozenset(),
    "INTERRUPTED": frozenset(),
}
_AGENT_RUN_TERMINAL_STATUSES = frozenset({
    "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED",
})
_WORKTREE_LEASE_TRANSITIONS = {
    "PROVISIONING": frozenset({"READY", "FAILED"}),
    "READY": frozenset({"RUNNING", "REJECTED", "FAILED"}),
    "RUNNING": frozenset({"PRESERVED", "FAILED"}),
    "PRESERVED": frozenset({"INTEGRATING", "REJECTED"}),
    "INTEGRATING": frozenset({"PRESERVED", "MERGED", "FAILED"}),
    "FAILED": frozenset({"REJECTED"}),
    "MERGED": frozenset(),
    "REJECTED": frozenset(),
}
_RECOVERY_SOURCE_KINDS = frozenset({"TOOL_EXECUTION", "RUN"})
_RECOVERY_FAILURE_CLASSES = frozenset({
    "CONTROL", "SECURITY", "INPUT", "CONFIGURATION", "PRECONDITION",
    "CODE_EXECUTION", "TRANSIENT", "RESOURCE", "PARTIAL_WRITE",
    "INTERNAL_UNKNOWN",
})
_RECOVERY_ACTIONS = frozenset({
    "RETRY", "REPAIR_REQUIRED", "ROLLBACK", "STOP", "NOT_APPLICABLE",
})
_RECOVERY_STATUSES = frozenset({
    "PENDING", "RETRYING", "RETRY_SUCCEEDED", "RETRY_EXHAUSTED",
    "REPAIR_REQUIRED", "RESOLVED", "ABANDONED", "MANUAL_REQUIRED",
    "ROLLBACK_RUNNING", "ROLLED_BACK", "ROLLBACK_SKIPPED",
    "ROLLBACK_CONFLICT", "NOT_APPLICABLE",
})
_RECOVERY_TRANSITIONS = {
    "PENDING": frozenset({
        "RETRYING", "REPAIR_REQUIRED", "ROLLBACK_RUNNING", "NOT_APPLICABLE",
    }),
    "RETRYING": frozenset({"RETRY_SUCCEEDED", "RETRY_EXHAUSTED"}),
    "REPAIR_REQUIRED": frozenset({"RESOLVED", "ABANDONED", "MANUAL_REQUIRED"}),
    "ROLLBACK_RUNNING": frozenset({
        "ROLLED_BACK", "ROLLBACK_SKIPPED", "ROLLBACK_CONFLICT",
    }),
    "RETRY_SUCCEEDED": frozenset(),
    "RETRY_EXHAUSTED": frozenset(),
    "RESOLVED": frozenset(),
    "ABANDONED": frozenset(),
    "MANUAL_REQUIRED": frozenset(),
    "ROLLED_BACK": frozenset(),
    "ROLLBACK_SKIPPED": frozenset(),
    "ROLLBACK_CONFLICT": frozenset(),
    "NOT_APPLICABLE": frozenset(),
}
_RECOVERY_TERMINAL_STATUSES = frozenset({
    status for status, targets in _RECOVERY_TRANSITIONS.items() if not targets
})
_RECOVERY_REASON_MAX_BYTES = 8 * 1024
_RECOVERY_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RECOVERY_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*[^\s,;]+"
    ),
)
_RECOVERY_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "password", "secret", "token",
})
_TOOL_ATTEMPT_TERMINAL_STATUSES = frozenset({
    "SUCCEEDED", "FAILED", "CANCELLED",
})
_TOOL_ATTEMPT_PREVIEW_MAX_CHARS = 1000


def _validate_status(value: str, allowed: frozenset[str], field: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {field}: {value!r}")
    return value


def _validate_artifact_relpath(value: str | None, field: str) -> str | None:
    """只接受制品根目录下的 POSIX 相对路径。"""
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"invalid {field}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"invalid {field}: {value!r}")
    if ":" in value:
        raise ValueError(f"invalid {field}: {value!r}")
    return path.as_posix()


def _validate_artifact_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _validate_git_object_id(value: str | None, field: str, *, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value
    ):
        raise ValueError(f"invalid {field}")
    return value.lower()


def _validate_sha256(value: str | None, field: str, *, optional: bool = False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"invalid {field}")
    return value


def _validate_bundle_relpath(
    value: str | None,
    *,
    run_id: str,
    category: str,
    item_id: str,
    field: str,
) -> str | None:
    """强制执行记录使用稳定制品布局，避免跨 Run 指向任意文件。"""
    relative_path = _validate_artifact_relpath(value, field)
    if relative_path is None:
        return None
    expected = (run_id, category, item_id)
    parts = PurePosixPath(relative_path).parts
    if len(parts) != 4 or parts[:3] != expected:
        raise ValueError(f"invalid {field}: does not belong to its artifact bundle")
    return relative_path


def _validate_workflow_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _WORKFLOW_IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _validate_workflow_edge_id(value: str) -> str:
    if value in _WORKFLOW_SPECIAL_EDGE_IDS:
        return value
    return _validate_workflow_identifier(value, "edge_id")


def _normalize_workflow_json(value, field: str, *, limit: int) -> tuple[str, object]:
    """规范化图定义、状态和摘要，禁止把不可解析的大内容写入 SQLite。"""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {field}: JSON is required") from exc
    else:
        parsed = value
    try:
        encoded = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: JSON serializable value is required") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError(f"invalid {field}: exceeds size limit")
    return encoded, parsed


def _normalize_workflow_state(value, *, root_task_id: str) -> str:
    """复用图模型的受限状态校验，避免 DB 入口绕过状态安全边界。"""
    encoded, parsed = _normalize_workflow_json(
        value, "state_json", limit=_WORKFLOW_STATE_MAX_BYTES
    )
    try:
        # 延迟导入避免 SessionDB 与 Agent 包在模块初始化阶段互相依赖。
        from agent.graph import validate_workflow_state

        normalized = validate_workflow_state(parsed, task_id=root_task_id)
    except (ImportError, ValueError) as exc:
        raise ValueError(f"invalid state_json: {exc}") from exc
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sanitize_recovery_reason(value, *, depth: int = 0):
    if depth > 4:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        text = value.replace("\x00", "")
        for pattern in _RECOVERY_SECRET_PATTERNS:
            text = pattern.sub("[REDACTED]", text)
        return text[:500]
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_recovery_reason(item, depth=depth + 1)
            for item in value[:20]
        ]
    if isinstance(value, dict):
        result = {}
        for raw_key, item in list(value.items())[:30]:
            key = str(raw_key).replace("\x00", "")[:64]
            if key.lower() in _RECOVERY_SENSITIVE_KEYS:
                result[key] = "[REDACTED]"
            else:
                result[key] = _sanitize_recovery_reason(
                    item, depth=depth + 1
                )
        return result
    return f"[{type(value).__name__}]"


def _normalize_recovery_reason(value) -> str:
    sanitized = _sanitize_recovery_reason(value)
    if not isinstance(sanitized, dict):
        raise ValueError("recovery reason must be an object")
    encoded, _ = _normalize_workflow_json(
        sanitized, "reason_json", limit=_RECOVERY_REASON_MAX_BYTES
    )
    return encoded


def _sanitize_tool_attempt_text(value) -> str:
    text = str(value or "").replace("\x00", "")
    for pattern in _RECOVERY_SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text[:_TOOL_ATTEMPT_PREVIEW_MAX_CHARS]

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

    def _migrate_to_v3(self):
        """增加可复现执行的快照与制品元数据，不保存大文件本体。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS workspace_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                workspace_root TEXT NOT NULL,
                git_root TEXT NOT NULL,
                base_commit TEXT,
                state_hash TEXT,
                capture_status TEXT NOT NULL,
                artifact_status TEXT NOT NULL DEFAULT 'AVAILABLE',
                reason_code TEXT,
                manifest_relpath TEXT,
                base_tree_relpath TEXT,
                patch_relpath TEXT,
                untracked_relpath TEXT,
                capture_fingerprint TEXT,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS execution_records (
                record_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                tool_execution_id TEXT NOT NULL
                    REFERENCES tool_executions(execution_id) ON DELETE CASCADE,
                snapshot_id TEXT REFERENCES workspace_snapshots(snapshot_id) ON DELETE SET NULL,
                tool_name TEXT NOT NULL,
                command_preview TEXT NOT NULL DEFAULT '',
                command_relpath TEXT,
                working_directory_rel TEXT,
                environment_relpath TEXT,
                stdout_relpath TEXT,
                stderr_relpath TEXT,
                exit_code INTEGER,
                termination_reason TEXT,
                log_status TEXT NOT NULL DEFAULT 'UNAVAILABLE',
                reproducibility_status TEXT NOT NULL DEFAULT 'UNAVAILABLE',
                artifact_status TEXT NOT NULL DEFAULT 'INCOMPLETE',
                replay_status TEXT NOT NULL DEFAULT 'NOT_REQUESTED',
                replayed_from_record_id TEXT
                    REFERENCES execution_records(record_id) ON DELETE SET NULL,
                created_at REAL NOT NULL,
                finished_at REAL,
                UNIQUE(tool_execution_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workspace_snapshots_run
            ON workspace_snapshots(run_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_records_run
            ON execution_records(run_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_records_snapshot
            ON execution_records(snapshot_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_records_reproducibility
            ON execution_records(reproducibility_status, artifact_status, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_execution_records_replay
            ON execution_records(replay_status, created_at)
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

    def _migrate_to_v4(self):
        """增加 Graph Engineering 的工作流、节点、边和人工 Gate 记录。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS workflow_runs (
                workflow_run_id TEXT PRIMARY KEY,
                root_task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
                root_agent_run_id TEXT REFERENCES agent_runs(run_id),
                workflow_id TEXT NOT NULL,
                workflow_version INTEGER NOT NULL,
                definition_snapshot_json TEXT NOT NULL,
                conversation_id TEXT,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                state_version INTEGER NOT NULL DEFAULT 0,
                pause_reason TEXT,
                completion_reason TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workflow_node_runs (
                node_run_id TEXT PRIMARY KEY,
                workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(workflow_run_id)
                    ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                branch_key TEXT NOT NULL DEFAULT 'main',
                attempt INTEGER NOT NULL DEFAULT 1,
                node_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                input_state_version INTEGER NOT NULL,
                output_state_version INTEGER,
                agent_task_id TEXT REFERENCES agent_tasks(task_id),
                agent_run_id TEXT REFERENCES agent_runs(run_id),
                output_summary_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT,
                error_message TEXT,
                created_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                UNIQUE(workflow_run_id, node_id, branch_key, attempt),
                UNIQUE(agent_run_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workflow_transitions (
                transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(workflow_run_id)
                    ON DELETE CASCADE,
                from_node_run_id TEXT REFERENCES workflow_node_runs(node_run_id)
                    ON DELETE SET NULL,
                to_node_run_id TEXT REFERENCES workflow_node_runs(node_run_id)
                    ON DELETE SET NULL,
                edge_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                state_version INTEGER NOT NULL,
                created_at REAL NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workflow_gates (
                gate_id TEXT PRIMARY KEY,
                workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(workflow_run_id)
                    ON DELETE CASCADE,
                node_run_id TEXT NOT NULL UNIQUE REFERENCES workflow_node_runs(node_run_id)
                    ON DELETE CASCADE,
                gate_kind TEXT NOT NULL,
                status TEXT NOT NULL,
                request_summary TEXT NOT NULL,
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                response_summary TEXT,
                requested_at REAL NOT NULL,
                responded_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_runs_conversation_created
            ON workflow_runs(conversation_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_runs_status_created
            ON workflow_runs(status, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_runs_workflow_status
            ON workflow_node_runs(workflow_run_id, status, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_node_runs_agent_run
            ON workflow_node_runs(agent_run_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_transitions_workflow
            ON workflow_transitions(workflow_run_id, transition_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_workflow_gates_status_requested
            ON workflow_gates(status, requested_at)
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

    def _migrate_to_v5(self):
        """把 bash 证据记录可选地关联到执行它的图节点。"""
        columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(execution_records)"
            ).fetchall()
        }
        if "node_run_id" not in columns:
            self._conn.execute(
                "ALTER TABLE execution_records ADD COLUMN node_run_id TEXT "
                "REFERENCES workflow_node_runs(node_run_id) ON DELETE SET NULL"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_records_node_run "
            "ON execution_records(node_run_id, created_at)"
        )

    def _migrate_to_v6(self):
        """命令制品需要独立完整性哈希，不能只信任其存在。"""
        columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(execution_records)"
            ).fetchall()
        }
        if "command_sha256" not in columns:
            self._conn.execute(
                "ALTER TABLE execution_records ADD COLUMN command_sha256 TEXT"
            )

    def _migrate_to_v7(self):
        """快照也需要记录当前制品可用性，防止已清理目录再次成为候选。"""
        columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(workspace_snapshots)"
            ).fetchall()
        }
        if "artifact_status" not in columns:
            self._conn.execute(
                "ALTER TABLE workspace_snapshots ADD COLUMN artifact_status TEXT "
                "NOT NULL DEFAULT 'AVAILABLE'"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_snapshots_artifact "
            "ON workspace_snapshots(artifact_status, created_at)"
        )

    def _migrate_to_v8(self):
        """增加 Worktree lease，并让执行证据可选地关联独占工作区。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS worktree_leases (
                workspace_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL UNIQUE REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                parent_run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
                git_root TEXT NOT NULL,
                worktree_path TEXT NOT NULL UNIQUE,
                branch_name TEXT NOT NULL UNIQUE,
                base_commit TEXT NOT NULL,
                write_scope_json TEXT NOT NULL,
                runner_backend TEXT NOT NULL,
                runner_image_digest TEXT,
                lease_status TEXT NOT NULL,
                cleanup_status TEXT NOT NULL DEFAULT 'PENDING',
                diff_relpath TEXT,
                diff_hash TEXT,
                change_manifest_relpath TEXT,
                change_manifest_hash TEXT,
                failure_code TEXT,
                failure_message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                preserve_until REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_worktree_leases_status_updated
            ON worktree_leases(lease_status, updated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_worktree_leases_parent
            ON worktree_leases(parent_run_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_worktree_leases_task
            ON worktree_leases(task_id, created_at)
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

        snapshot_columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(workspace_snapshots)"
            ).fetchall()
        }
        if "workspace_id" not in snapshot_columns:
            self._conn.execute(
                "ALTER TABLE workspace_snapshots ADD COLUMN workspace_id TEXT "
                "REFERENCES worktree_leases(workspace_id) ON DELETE SET NULL"
            )

        record_columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(execution_records)"
            ).fetchall()
        }
        if "workspace_id" not in record_columns:
            self._conn.execute(
                "ALTER TABLE execution_records ADD COLUMN workspace_id TEXT "
                "REFERENCES worktree_leases(workspace_id) ON DELETE SET NULL"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workspace_snapshots_workspace "
            "ON workspace_snapshots(workspace_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_records_workspace "
            "ON execution_records(workspace_id, created_at)"
        )

    def _migrate_to_v9(self):
        """增加失败分类与恢复决策审计，不改变现有工具执行行为。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS failure_recovery_records (
                recovery_id TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                node_run_id TEXT REFERENCES workflow_node_runs(node_run_id)
                    ON DELETE SET NULL,
                tool_execution_id TEXT REFERENCES tool_executions(execution_id)
                    ON DELETE CASCADE,
                parent_recovery_id TEXT REFERENCES failure_recovery_records(recovery_id)
                    ON DELETE SET NULL,
                failure_class TEXT NOT NULL,
                error_code TEXT NOT NULL,
                selected_action TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_number INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 0,
                workspace_id TEXT REFERENCES worktree_leases(workspace_id)
                    ON DELETE SET NULL,
                checkpoint_id TEXT,
                reason_json TEXT NOT NULL DEFAULT '{}',
                result_record_id TEXT REFERENCES execution_records(record_id)
                    ON DELETE SET NULL,
                version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL,
                CHECK (
                    (source_kind = 'TOOL_EXECUTION' AND tool_execution_id IS NOT NULL)
                    OR (source_kind = 'RUN' AND tool_execution_id IS NULL)
                )
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_initial_tool_source
            ON failure_recovery_records(tool_execution_id)
            WHERE tool_execution_id IS NOT NULL AND parent_recovery_id IS NULL
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_initial_run_source
            ON failure_recovery_records(run_id)
            WHERE source_kind = 'RUN' AND parent_recovery_id IS NULL
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_recovery_run_created
            ON failure_recovery_records(run_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_recovery_status_updated
            ON failure_recovery_records(status, updated_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_recovery_parent
            ON failure_recovery_records(parent_recovery_id, created_at)
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

    def _migrate_to_v10(self):
        """增加每次真实工具尝试与重试等待的独立审计流水。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS tool_retry_attempts (
                attempt_id TEXT PRIMARY KEY,
                tool_execution_id TEXT NOT NULL
                    REFERENCES tool_executions(execution_id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                status TEXT NOT NULL,
                retryable INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0, 1)),
                error_code TEXT,
                error_message TEXT,
                output_preview TEXT,
                duration_seconds REAL,
                retry_delay_seconds REAL,
                wait_status TEXT NOT NULL DEFAULT 'NOT_SCHEDULED',
                started_at REAL NOT NULL,
                finished_at REAL,
                wait_started_at REAL,
                wait_finished_at REAL,
                UNIQUE (tool_execution_id, attempt_number)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tool_retry_attempts_execution
            ON tool_retry_attempts(tool_execution_id, attempt_number)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tool_retry_attempts_waiting
            ON tool_retry_attempts(wait_status, wait_started_at)
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

    def _migrate_to_v11(self):
        """增加修复后复验的稳定命令标识和工具来源唯一约束。"""
        columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(execution_records)"
            ).fetchall()
        }
        if "verification_key" not in columns:
            self._conn.execute(
                "ALTER TABLE execution_records ADD COLUMN verification_key TEXT"
            )
        statements = [
            """
            CREATE INDEX IF NOT EXISTS idx_execution_records_verification
            ON execution_records(
                run_id, workspace_id, verification_key, created_at
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_recovery_tool_source
            ON failure_recovery_records(tool_execution_id)
            WHERE tool_execution_id IS NOT NULL
            """,
        ]
        for statement in statements:
            self._conn.execute(statement)

    def _migrate_to_v12(self):
        """为 Worktree 回滚增加独立制品引用和稳定结果原因。"""
        columns = {
            row[1] for row in self._conn.execute(
                "PRAGMA table_info(failure_recovery_records)"
            ).fetchall()
        }
        additions = {
            "result_artifact_relpath": "TEXT",
            "result_artifact_hash": "TEXT",
            "result_reason_code": "TEXT",
        }
        for name, column_type in additions.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE failure_recovery_records ADD COLUMN {name} {column_type}"
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recovery_workspace_action "
            "ON failure_recovery_records(workspace_id, selected_action, created_at)"
        )

    def _migrate_to_v13(self):
        """增加 Worktree 显式集成事务、验证证据和清理状态。"""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS worktree_integration_records (
                integration_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES worktree_leases(workspace_id),
                integration_run_id TEXT NOT NULL UNIQUE
                    REFERENCES agent_runs(run_id) ON DELETE CASCADE,
                source_main_commit TEXT NOT NULL,
                candidate_commit TEXT,
                candidate_tree_hash TEXT,
                expected_merge_tree_hash TEXT,
                final_merge_commit TEXT,
                final_merge_tree_hash TEXT,
                verification_command_hash TEXT NOT NULL,
                verification_record_id TEXT
                    REFERENCES execution_records(record_id) ON DELETE SET NULL,
                status TEXT NOT NULL,
                failure_code TEXT,
                failure_message TEXT,
                result_artifact_relpath TEXT,
                result_artifact_hash TEXT,
                temp_cleanup_status TEXT NOT NULL DEFAULT 'PENDING',
                version INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                finished_at REAL
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_worktree_integrations_workspace
            ON worktree_integration_records(workspace_id, created_at)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_worktree_integrations_status
            ON worktree_integration_records(status, updated_at)
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_worktree_integrations_active_workspace
            ON worktree_integration_records(workspace_id)
            WHERE status IN ('PREPARING', 'VERIFYING', 'READY_TO_APPLY')
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_worktree_integrations_single_active
            ON worktree_integration_records((1))
            WHERE status IN ('PREPARING', 'VERIFYING', 'READY_TO_APPLY')
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
                terminal_status = (
                    "TIMED_OUT"
                    if completion_reason == "deadline_exceeded"
                    else "CANCELLED"
                )
                task_status = (
                    "FAILED" if terminal_status == "TIMED_OUT" else "CANCELLED"
                )
                run_event = (
                    "run_timed_out"
                    if terminal_status == "TIMED_OUT"
                    else "run_cancelled"
                )
                task_event = (
                    "task_failed"
                    if terminal_status == "TIMED_OUT"
                    else "task_cancelled"
                )
                self._conn.execute(
                    """UPDATE agent_runs SET status = ?, finished_at = ?,
                              completion_reason = ?
                       WHERE run_id = ? AND status = 'QUEUED'""",
                    (terminal_status, now, completion_reason, run_id),
                )
                self._conn.execute(
                    """UPDATE agent_tasks SET status = ?, finished_at = ?
                       WHERE task_id = ? AND status = 'PENDING'""",
                    (task_status, now, task_id),
                )
                self._append_agent_event(
                    task_id, run_id, run_event,
                    {"completion_reason": completion_reason},
                )
                self._append_agent_event(task_id, run_id, task_event)
                return terminal_status
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

            self._finalize_repair_recoveries_locked(
                run_id=run_id,
                task_id=task_id,
                run_status=status,
                completion_reason=completion_reason,
                now=now,
            )

    def _finalize_repair_recoveries_locked(
        self,
        *,
        run_id: str,
        task_id: str,
        run_status: str,
        completion_reason: str,
        now: float,
    ) -> int:
        """Run 终态时关闭未验证的修复项；调用方必须持有事务锁。"""
        target = (
            "ABANDONED"
            if run_status in {"CANCELLED", "TIMED_OUT", "INTERRUPTED"}
            else "MANUAL_REQUIRED"
        )
        rows = self._conn.execute(
            """SELECT recovery_id, version
               FROM failure_recovery_records
               WHERE run_id = ? AND status = 'REPAIR_REQUIRED'
               ORDER BY created_at, recovery_id""",
            (run_id,),
        ).fetchall()
        count = 0
        for recovery_id, version in rows:
            update = self._conn.execute(
                """UPDATE failure_recovery_records
                   SET status = ?, version = version + 1,
                       updated_at = ?, finished_at = ?
                   WHERE recovery_id = ? AND status = 'REPAIR_REQUIRED'
                     AND version = ?""",
                (target, now, now, recovery_id, version),
            )
            if update.rowcount != 1:
                continue
            count += 1
            self._append_agent_event(
                task_id,
                run_id,
                "repair_closed",
                {
                    "recovery_id": recovery_id,
                    "status": target,
                    "run_status": run_status,
                    "completion_reason": completion_reason,
                },
            )
        return count

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
                self._finalize_repair_recoveries_locked(
                    run_id=run_id,
                    task_id=task_id,
                    run_status=terminal,
                    completion_reason=reason,
                    now=now,
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

    # ── Tool attempt and retry-wait audit storage ─────────────────

    def start_tool_retry_attempt(
        self,
        *,
        attempt_id: str,
        tool_execution_id: str,
        attempt_number: int,
    ) -> dict:
        attempt_id = _validate_artifact_identifier(attempt_id, "attempt_id")
        tool_execution_id = _validate_artifact_identifier(
            tool_execution_id, "tool_execution_id"
        )
        attempt_number = int(attempt_number)
        if attempt_number < 1:
            raise ValueError("attempt_number must be positive")
        now = time.time()
        with self._transaction():
            execution = self._conn.execute(
                "SELECT status FROM tool_executions WHERE execution_id = ?",
                (tool_execution_id,),
            ).fetchone()
            if not execution:
                raise KeyError(f"unknown tool execution: {tool_execution_id}")
            if execution[0] != "RUNNING":
                raise RuntimeError("tool execution is not running")
            previous = self._conn.execute(
                """SELECT COALESCE(MAX(attempt_number), 0)
                   FROM tool_retry_attempts WHERE tool_execution_id = ?""",
                (tool_execution_id,),
            ).fetchone()[0]
            if attempt_number != previous + 1:
                raise RuntimeError(
                    f"non-sequential tool attempt: expected {previous + 1}"
                )
            self._conn.execute(
                """INSERT INTO tool_retry_attempts
                   (attempt_id, tool_execution_id, attempt_number, status,
                    retryable, wait_status, started_at)
                   VALUES (?, ?, ?, 'RUNNING', 0, 'NOT_SCHEDULED', ?)""",
                (attempt_id, tool_execution_id, attempt_number, now),
            )
        return self.get_tool_retry_attempt(attempt_id)

    def finish_tool_retry_attempt(
        self,
        *,
        attempt_id: str,
        status: str,
        retryable: bool,
        error_code: str | None,
        error_message: str | None,
        output_preview: str | None,
        duration_seconds: float,
    ) -> dict:
        attempt_id = _validate_artifact_identifier(attempt_id, "attempt_id")
        status = _validate_status(
            status, _TOOL_ATTEMPT_TERMINAL_STATUSES, "tool attempt status"
        )
        if status == "SUCCEEDED":
            error_code = None
            error_message = None
            retryable = False
        elif not isinstance(error_code, str) or not _RECOVERY_ERROR_CODE.fullmatch(
            error_code
        ):
            raise ValueError("failed tool attempt requires a stable error_code")
        duration_seconds = float(duration_seconds)
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("invalid tool attempt duration")
        now = time.time()
        update = self._conn.execute(
            """UPDATE tool_retry_attempts
               SET status = ?, retryable = ?, error_code = ?, error_message = ?,
                   output_preview = ?, duration_seconds = ?, finished_at = ?
               WHERE attempt_id = ? AND status = 'RUNNING'""",
            (
                status,
                1 if retryable else 0,
                error_code,
                _sanitize_tool_attempt_text(error_message) or None,
                _sanitize_tool_attempt_text(output_preview),
                duration_seconds,
                now,
                attempt_id,
            ),
        )
        if update.rowcount != 1:
            raise RuntimeError(
                f"illegal terminal transition for tool attempt {attempt_id}"
            )
        return self.get_tool_retry_attempt(attempt_id)

    def schedule_tool_retry_wait(
        self,
        *,
        attempt_id: str,
        retry_delay_seconds: float,
    ) -> dict:
        attempt_id = _validate_artifact_identifier(attempt_id, "attempt_id")
        retry_delay_seconds = float(retry_delay_seconds)
        if (
            not math.isfinite(retry_delay_seconds)
            or retry_delay_seconds < 0
            or retry_delay_seconds > 60.0
        ):
            raise ValueError("invalid retry delay")
        now = time.time()
        update = self._conn.execute(
            """UPDATE tool_retry_attempts
               SET retry_delay_seconds = ?, wait_status = 'WAITING',
                   wait_started_at = ?
               WHERE attempt_id = ? AND status = 'FAILED'
                 AND retryable = 1 AND wait_status = 'NOT_SCHEDULED'""",
            (retry_delay_seconds, now, attempt_id),
        )
        if update.rowcount != 1:
            raise RuntimeError(
                f"tool attempt is not eligible for retry wait: {attempt_id}"
            )
        return self.get_tool_retry_attempt(attempt_id)

    def finish_tool_retry_wait(
        self,
        *,
        attempt_id: str,
        status: str,
    ) -> dict:
        attempt_id = _validate_artifact_identifier(attempt_id, "attempt_id")
        if status not in {"COMPLETED", "CANCELLED"}:
            raise ValueError(f"invalid retry wait status: {status!r}")
        update = self._conn.execute(
            """UPDATE tool_retry_attempts
               SET wait_status = ?, wait_finished_at = ?
               WHERE attempt_id = ? AND wait_status = 'WAITING'""",
            (status, time.time(), attempt_id),
        )
        if update.rowcount != 1:
            raise RuntimeError(
                f"illegal retry wait transition for tool attempt {attempt_id}"
            )
        return self.get_tool_retry_attempt(attempt_id)

    def get_tool_retry_attempt(self, attempt_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM tool_retry_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        return self._decode_tool_retry_attempt(row)

    def list_tool_retry_attempts(self, tool_execution_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM tool_retry_attempts
               WHERE tool_execution_id = ? ORDER BY attempt_number""",
            (tool_execution_id,),
        ).fetchall()
        return [self._decode_tool_retry_attempt(row) for row in rows]

    def _decode_tool_retry_attempt(self, row) -> dict | None:
        result = self._row_to_dict("tool_retry_attempts", row)
        if result:
            result["retryable"] = bool(result["retryable"])
        return result

    # ── Failure recovery audit storage ─────────────────────────────

    def create_worktree_rollback_recovery(
        self,
        *,
        recovery_id: str,
        workspace_id: str,
        reason=None,
    ) -> tuple[dict, bool]:
        """为已停止的独占 Worktree 创建一次显式回滚尝试。"""
        recovery_id = _validate_artifact_identifier(recovery_id, "recovery_id")
        workspace_id = _validate_artifact_identifier(workspace_id, "workspace_id")
        reason_json = _normalize_recovery_reason(reason or {})
        now = time.time()
        with self._transaction():
            lease = self._conn.execute(
                """SELECT task_id, run_id, lease_status, base_commit, cleanup_status
                   FROM worktree_leases WHERE workspace_id = ?""",
                (workspace_id,),
            ).fetchone()
            if not lease:
                raise KeyError(f"unknown worktree lease: {workspace_id}")
            task_id, run_id, lease_status, base_commit, _cleanup_status = lease
            run = self._conn.execute(
                "SELECT status FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not run or run[0] not in _AGENT_RUN_TERMINAL_STATUSES:
                raise RuntimeError("worktree rollback requires a terminal source run")

            latest_row = self._conn.execute(
                """SELECT * FROM failure_recovery_records
                   WHERE source_kind = 'RUN' AND run_id = ?
                   ORDER BY created_at DESC, recovery_id DESC LIMIT 1""",
                (run_id,),
            ).fetchone()
            latest = self._decode_failure_recovery(latest_row)
            if latest and latest["selected_action"] == "ROLLBACK" and latest["status"] in {
                "PENDING", "ROLLBACK_RUNNING", "ROLLED_BACK",
            }:
                if lease_status == "REJECTED" and latest["status"] != "ROLLED_BACK":
                    raise RuntimeError("rejected worktree has no completed rollback")
                return latest, False
            if lease_status not in {"PRESERVED", "FAILED"}:
                raise RuntimeError(
                    f"worktree lease in {lease_status} state cannot be rolled back"
                )

            parent_recovery_id = latest["recovery_id"] if latest else None
            self._conn.execute(
                """INSERT INTO failure_recovery_records
                   (recovery_id, source_kind, run_id, node_run_id,
                    tool_execution_id, parent_recovery_id, failure_class,
                    error_code, selected_action, status, attempt_number,
                    max_attempts, workspace_id, checkpoint_id, reason_json,
                    version, created_at, updated_at, finished_at)
                   VALUES (?, 'RUN', ?, NULL, NULL, ?, 'CONTROL',
                           'discard_requested', 'ROLLBACK', 'PENDING', 0, 0,
                           ?, ?, ?, 1, ?, ?, NULL)""",
                (
                    recovery_id, run_id, parent_recovery_id, workspace_id,
                    base_commit, reason_json, now, now,
                ),
            )
            self._append_agent_event(
                task_id,
                run_id,
                "rollback_requested",
                {
                    "recovery_id": recovery_id,
                    "workspace_id": workspace_id,
                    "parent_recovery_id": parent_recovery_id,
                },
            )
        return self.get_failure_recovery(recovery_id), True

    def create_initial_failure_recovery(
        self,
        *,
        recovery_id: str,
        run_id: str,
        node_run_id: str | None,
        tool_execution_id: str,
        failure_class: str,
        error_code: str,
        selected_action: str,
        status: str,
        attempt_number: int = 0,
        max_attempts: int = 0,
        workspace_id: str | None = None,
        checkpoint_id: str | None = None,
        reason=None,
    ) -> tuple[dict, bool]:
        """Idempotently create one initial record for a failed tool execution."""
        recovery_id = _validate_artifact_identifier(recovery_id, "recovery_id")
        run_id = _validate_artifact_identifier(run_id, "run_id")
        tool_execution_id = _validate_artifact_identifier(
            tool_execution_id, "tool_execution_id"
        )
        if node_run_id is not None:
            node_run_id = _validate_artifact_identifier(
                node_run_id, "node_run_id"
            )
        if workspace_id is not None:
            workspace_id = _validate_artifact_identifier(
                workspace_id, "workspace_id"
            )
        if checkpoint_id is not None:
            checkpoint_id = _validate_artifact_identifier(
                checkpoint_id, "checkpoint_id"
            )
        failure_class = _validate_status(
            failure_class, _RECOVERY_FAILURE_CLASSES, "failure_class"
        )
        selected_action = _validate_status(
            selected_action, _RECOVERY_ACTIONS, "selected_action"
        )
        status = _validate_status(status, _RECOVERY_STATUSES, "recovery status")
        if not isinstance(error_code, str) or not _RECOVERY_ERROR_CODE.fullmatch(
            error_code
        ):
            raise ValueError("invalid recovery error_code")
        initial_statuses = {
            "RETRY": {"PENDING", "RETRY_EXHAUSTED"},
            "REPAIR_REQUIRED": {"REPAIR_REQUIRED"},
            "ROLLBACK": {"PENDING"},
            "STOP": {"NOT_APPLICABLE"},
            "NOT_APPLICABLE": {"NOT_APPLICABLE"},
        }
        if status not in initial_statuses[selected_action]:
            raise ValueError(
                f"invalid initial status {status} for action {selected_action}"
            )
        attempt_number = max(0, int(attempt_number))
        max_attempts = max(0, int(max_attempts))
        if attempt_number > max_attempts:
            raise ValueError("attempt_number exceeds max_attempts")
        reason_json = _normalize_recovery_reason(reason or {})
        now = time.time()
        finished_at = now if status in _RECOVERY_TERMINAL_STATUSES else None

        with self._transaction():
            source = self._conn.execute(
                """SELECT run_id, status, error_code FROM tool_executions
                   WHERE execution_id = ?""",
                (tool_execution_id,),
            ).fetchone()
            if not source:
                raise KeyError(f"unknown tool execution: {tool_execution_id}")
            if source[0] != run_id:
                raise ValueError("tool execution does not belong to recovery run")
            if source[1] not in {"FAILED", "DENIED", "CANCELLED"}:
                raise ValueError("recovery source must be a failed tool execution")
            from agent.recovery import ERROR_CODE_REGISTRY

            source_error_code = source[2]
            definition = (
                ERROR_CODE_REGISTRY.get(source_error_code)
                if isinstance(source_error_code, str)
                else None
            )
            expected_error_code = (
                source_error_code if definition is not None else "unknown_failure"
            )
            expected_failure_class = (
                definition.failure_class.value
                if definition is not None else "INTERNAL_UNKNOWN"
            )
            if (
                error_code != expected_error_code
                or failure_class != expected_failure_class
            ):
                raise ValueError(
                    "recovery classification does not match tool execution"
                )
            if node_run_id is not None:
                node = self._conn.execute(
                    """SELECT agent_run_id FROM workflow_node_runs
                       WHERE node_run_id = ?""",
                    (node_run_id,),
                ).fetchone()
                if not node or node[0] != run_id:
                    raise ValueError("node run does not belong to recovery run")
            if workspace_id is not None:
                workspace = self._conn.execute(
                    "SELECT run_id FROM worktree_leases WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if not workspace or workspace[0] != run_id:
                    raise ValueError("workspace does not belong to recovery run")

            existing = self._conn.execute(
                """SELECT * FROM failure_recovery_records
                   WHERE tool_execution_id = ?""",
                (tool_execution_id,),
            ).fetchone()
            if existing:
                return self._decode_failure_recovery(existing), False

            inserted = self._conn.execute(
                """INSERT OR IGNORE INTO failure_recovery_records
                   (recovery_id, source_kind, run_id, node_run_id,
                    tool_execution_id, parent_recovery_id, failure_class,
                    error_code, selected_action, status, attempt_number,
                    max_attempts, workspace_id, checkpoint_id, reason_json,
                    version, created_at, updated_at, finished_at)
                   VALUES (?, 'TOOL_EXECUTION', ?, ?, ?, NULL, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    recovery_id, run_id, node_run_id, tool_execution_id,
                    failure_class, error_code, selected_action, status,
                    attempt_number, max_attempts, workspace_id, checkpoint_id,
                    reason_json, now, now, finished_at,
                ),
            )
            row = self._conn.execute(
                """SELECT * FROM failure_recovery_records
                   WHERE tool_execution_id = ?""",
                (tool_execution_id,),
            ).fetchone()
            if not row:
                raise RuntimeError("failed to create recovery record")
            return self._decode_failure_recovery(row), inserted.rowcount == 1

    def transition_failure_recovery(
        self,
        recovery_id: str,
        *,
        status: str,
        expected_version: int,
        result_record_id: str | None = None,
        result_artifact_relpath: str | None = None,
        result_artifact_hash: str | None = None,
        result_reason_code: str | None = None,
    ) -> dict:
        """Apply one legal optimistic recovery status transition."""
        recovery_id = _validate_artifact_identifier(recovery_id, "recovery_id")
        status = _validate_status(status, _RECOVERY_STATUSES, "recovery status")
        expected_version = int(expected_version)
        if result_record_id is not None:
            result_record_id = _validate_artifact_identifier(
                result_record_id, "result_record_id"
            )
        result_artifact_relpath = _validate_artifact_relpath(
            result_artifact_relpath, "result_artifact_relpath"
        )
        if result_artifact_hash is not None and not re.fullmatch(
            r"[0-9a-f]{64}", result_artifact_hash
        ):
            raise ValueError("invalid result_artifact_hash")
        if (result_artifact_relpath is None) != (result_artifact_hash is None):
            raise ValueError("recovery artifact path and hash must be provided together")
        if result_reason_code is not None:
            if not _RECOVERY_ERROR_CODE.fullmatch(result_reason_code):
                raise ValueError("invalid recovery result_reason_code")
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM failure_recovery_records WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown recovery record: {recovery_id}")
            current = self._decode_failure_recovery(row)
            if current["version"] != expected_version:
                raise RuntimeError("stale recovery record version")
            if status not in _RECOVERY_TRANSITIONS[current["status"]]:
                raise RuntimeError(
                    f"illegal recovery transition {current['status']} -> {status}"
                )
            required_action = {
                "RETRYING": "RETRY",
                "REPAIR_REQUIRED": "REPAIR_REQUIRED",
                "ROLLBACK_RUNNING": "ROLLBACK",
            }.get(status)
            if required_action and current["selected_action"] != required_action:
                raise RuntimeError(
                    f"recovery action {current['selected_action']} cannot enter {status}"
                )
            if status == "RETRYING":
                run = self._conn.execute(
                    "SELECT status FROM agent_runs WHERE run_id = ?",
                    (current["run_id"],),
                ).fetchone()
                if not run or run[0] != "RUNNING":
                    raise RuntimeError(
                        "cannot start recovery work after the source run ended"
                    )
            if status == "ROLLBACK_RUNNING":
                run = self._conn.execute(
                    "SELECT status FROM agent_runs WHERE run_id = ?",
                    (current["run_id"],),
                ).fetchone()
                workspace = self._conn.execute(
                    """SELECT run_id, lease_status FROM worktree_leases
                       WHERE workspace_id = ?""",
                    (current["workspace_id"],),
                ).fetchone()
                if (
                    current["source_kind"] != "RUN"
                    or not run
                    or run[0] not in _AGENT_RUN_TERMINAL_STATUSES
                    or not workspace
                    or workspace[0] != current["run_id"]
                    or workspace[1] not in {"PRESERVED", "FAILED"}
                ):
                    raise RuntimeError(
                        "worktree rollback requires a terminal run and preserved candidate"
                    )
            if result_record_id is not None:
                result = self._conn.execute(
                    """SELECT run_id, workspace_id FROM execution_records
                       WHERE record_id = ?""",
                    (result_record_id,),
                ).fetchone()
                if not result:
                    raise KeyError(
                        f"unknown recovery result record: {result_record_id}"
                    )
                if (
                    result[0] != current["run_id"]
                    or result[1] != current["workspace_id"]
                ):
                    raise ValueError(
                        "recovery result record belongs to another run or workspace"
                    )
            effective_artifact_relpath = (
                result_artifact_relpath or current.get("result_artifact_relpath")
            )
            effective_artifact_hash = (
                result_artifact_hash or current.get("result_artifact_hash")
            )
            effective_reason_code = (
                result_reason_code or current.get("result_reason_code")
            )
            if effective_artifact_relpath and current["selected_action"] == "ROLLBACK":
                expected_relpath = (
                    f"{current['run_id']}/worktrees/{current['workspace_id']}/"
                    f"rollback-{current['recovery_id']}.json"
                )
                if effective_artifact_relpath != expected_relpath:
                    raise ValueError("rollback artifact path does not match recovery ownership")
            if status == "ROLLED_BACK" and (
                not effective_artifact_relpath or not effective_artifact_hash
            ):
                raise ValueError("ROLLED_BACK requires a verified result artifact")
            if status in {"ROLLBACK_SKIPPED", "ROLLBACK_CONFLICT"} and not effective_reason_code:
                raise ValueError(f"{status} requires a result_reason_code")

            now = time.time()
            finished_at = now if status in _RECOVERY_TERMINAL_STATUSES else None
            update = self._conn.execute(
                """UPDATE failure_recovery_records
                   SET status = ?, result_record_id = COALESCE(?, result_record_id),
                       result_artifact_relpath = COALESCE(?, result_artifact_relpath),
                       result_artifact_hash = COALESCE(?, result_artifact_hash),
                       result_reason_code = COALESCE(?, result_reason_code),
                       version = version + 1, updated_at = ?, finished_at = ?
                   WHERE recovery_id = ? AND status = ? AND version = ?""",
                (
                    status, result_record_id, result_artifact_relpath,
                    result_artifact_hash, result_reason_code, now, finished_at,
                    recovery_id, current["status"], expected_version,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("concurrent recovery transition rejected")
        return self.get_failure_recovery(recovery_id)

    def link_repair_verification_failure(
        self, recovery_id: str
    ) -> tuple[dict, dict | None]:
        """把同一验证命令的再次失败接到当前活动修复链。"""
        recovery_id = _validate_artifact_identifier(recovery_id, "recovery_id")
        with self._transaction():
            row = self._conn.execute(
                "SELECT * FROM failure_recovery_records WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown recovery record: {recovery_id}")
            current = self._decode_failure_recovery(row)
            if (
                current["selected_action"] != "REPAIR_REQUIRED"
                or current["status"] != "REPAIR_REQUIRED"
            ):
                return current, None
            if current["parent_recovery_id"] is not None:
                return current, None

            evidence = self._conn.execute(
                """SELECT record_id, run_id, workspace_id, verification_key,
                          finished_at
                   FROM execution_records WHERE tool_execution_id = ?""",
                (current["tool_execution_id"],),
            ).fetchone()
            if (
                not evidence
                or not evidence[3]
                or evidence[4] is None
                or evidence[1] != current["run_id"]
                or evidence[2] != current["workspace_id"]
            ):
                return current, None

            previous_row = self._conn.execute(
                """SELECT f.*
                   FROM failure_recovery_records f
                   JOIN execution_records e
                     ON e.tool_execution_id = f.tool_execution_id
                   WHERE f.run_id = ?
                     AND f.recovery_id != ?
                     AND f.selected_action = 'REPAIR_REQUIRED'
                     AND f.status = 'REPAIR_REQUIRED'
                     AND e.verification_key = ?
                     AND COALESCE(f.workspace_id, '') = COALESCE(?, '')
                     AND COALESCE(e.workspace_id, '') = COALESCE(f.workspace_id, '')
                   ORDER BY f.updated_at DESC, f.created_at DESC
                   LIMIT 1""",
                (
                    current["run_id"], recovery_id, evidence[3],
                    current["workspace_id"],
                ),
            ).fetchone()
            if not previous_row:
                return current, None
            previous = self._decode_failure_recovery(previous_row)
            now = time.time()
            previous_update = self._conn.execute(
                """UPDATE failure_recovery_records
                   SET status = 'ABANDONED', result_record_id = ?,
                       version = version + 1, updated_at = ?, finished_at = ?
                   WHERE recovery_id = ? AND status = 'REPAIR_REQUIRED'
                     AND version = ?""",
                (
                    evidence[0], now, now, previous["recovery_id"],
                    previous["version"],
                ),
            )
            if previous_update.rowcount != 1:
                raise RuntimeError("concurrent repair chain transition rejected")
            current_update = self._conn.execute(
                """UPDATE failure_recovery_records
                   SET parent_recovery_id = ?, version = version + 1,
                       updated_at = ?
                   WHERE recovery_id = ? AND status = 'REPAIR_REQUIRED'
                     AND parent_recovery_id IS NULL AND version = ?""",
                (
                    previous["recovery_id"], now, recovery_id,
                    current["version"],
                ),
            )
            if current_update.rowcount != 1:
                raise RuntimeError("concurrent repair chain link rejected")

            linked_row = self._conn.execute(
                "SELECT * FROM failure_recovery_records WHERE recovery_id = ?",
                (recovery_id,),
            ).fetchone()
            previous_row = self._conn.execute(
                "SELECT * FROM failure_recovery_records WHERE recovery_id = ?",
                (previous["recovery_id"],),
            ).fetchone()
            return (
                self._decode_failure_recovery(linked_row),
                self._decode_failure_recovery(previous_row),
            )

    def resolve_repair_verifications(
        self, tool_execution_id: str
    ) -> list[dict]:
        """用同一 Run/Worktree 内的新成功证据闭合活动修复项。"""
        tool_execution_id = _validate_artifact_identifier(
            tool_execution_id, "tool_execution_id"
        )
        with self._transaction():
            source = self._conn.execute(
                """SELECT t.run_id, t.status, e.record_id, e.workspace_id,
                          e.verification_key, e.finished_at
                   FROM tool_executions t
                   JOIN execution_records e
                     ON e.tool_execution_id = t.execution_id
                   WHERE t.execution_id = ?""",
                (tool_execution_id,),
            ).fetchone()
            if (
                not source
                or source[1] != "SUCCEEDED"
                or not source[4]
                or source[5] is None
            ):
                return []
            rows = self._conn.execute(
                """SELECT f.*
                   FROM failure_recovery_records f
                   JOIN execution_records e
                     ON e.tool_execution_id = f.tool_execution_id
                   WHERE f.run_id = ?
                     AND f.selected_action = 'REPAIR_REQUIRED'
                     AND f.status = 'REPAIR_REQUIRED'
                     AND e.verification_key = ?
                     AND COALESCE(f.workspace_id, '') = COALESCE(?, '')
                     AND COALESCE(e.workspace_id, '') = COALESCE(f.workspace_id, '')
                   ORDER BY f.created_at, f.recovery_id""",
                (source[0], source[4], source[3]),
            ).fetchall()
            now = time.time()
            resolved = []
            for row in rows:
                record = self._decode_failure_recovery(row)
                update = self._conn.execute(
                    """UPDATE failure_recovery_records
                       SET status = 'RESOLVED', result_record_id = ?,
                           version = version + 1, updated_at = ?, finished_at = ?
                       WHERE recovery_id = ? AND status = 'REPAIR_REQUIRED'
                         AND version = ?""",
                    (
                        source[2], now, now, record["recovery_id"],
                        record["version"],
                    ),
                )
                if update.rowcount != 1:
                    raise RuntimeError(
                        "concurrent repair verification transition rejected"
                    )
                updated = self._conn.execute(
                    """SELECT * FROM failure_recovery_records
                       WHERE recovery_id = ?""",
                    (record["recovery_id"],),
                ).fetchone()
                resolved.append(self._decode_failure_recovery(updated))
            return resolved

    def get_failure_recovery(self, recovery_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM failure_recovery_records WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        return self._decode_failure_recovery(row)

    def get_failure_recovery_for_tool_execution(
        self, tool_execution_id: str
    ) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM failure_recovery_records
               WHERE tool_execution_id = ?""",
            (tool_execution_id,),
        ).fetchone()
        return self._decode_failure_recovery(row)

    def find_failure_recoveries_by_prefix(
        self, recovery_id_prefix: str, limit: int = 3
    ) -> list[dict]:
        if not isinstance(recovery_id_prefix, str) or not recovery_id_prefix:
            return []
        rows = self._conn.execute(
            """SELECT * FROM failure_recovery_records
               WHERE recovery_id LIKE ? ORDER BY created_at DESC LIMIT ?""",
            (recovery_id_prefix + "%", min(max(int(limit), 1), 20)),
        ).fetchall()
        return [self._decode_failure_recovery(row) for row in rows]

    def list_failure_recoveries(
        self, run_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        limit = min(max(int(limit), 1), 200)
        if run_id is None:
            rows = self._conn.execute(
                """SELECT * FROM failure_recovery_records
                   ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM failure_recovery_records
                   WHERE run_id = ? ORDER BY created_at DESC LIMIT ?""",
                (run_id, limit),
            ).fetchall()
        return [self._decode_failure_recovery(row) for row in rows]

    def reconcile_failure_recoveries(self) -> dict[str, int]:
        """Close interrupted active recovery work without executing any action."""
        targets = {
            "PENDING": "NOT_APPLICABLE",
            "RETRYING": "RETRY_EXHAUSTED",
            "ROLLBACK_RUNNING": "ROLLBACK_SKIPPED",
        }
        counts = {target: 0 for target in targets.values()}
        counts.update({"ABANDONED": 0, "MANUAL_REQUIRED": 0})
        now = time.time()
        with self._transaction():
            rows = self._conn.execute(
                """SELECT f.recovery_id, f.status, f.run_id, r.task_id,
                          f.selected_action
                   FROM failure_recovery_records f
                   JOIN agent_runs r ON r.run_id = f.run_id
                   WHERE f.status IN ('PENDING', 'RETRYING', 'ROLLBACK_RUNNING')
                     AND r.status IN (
                         'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT',
                         'INTERRUPTED'
                     )"""
            ).fetchall()
            for recovery_id, current_status, run_id, task_id, selected_action in rows:
                terminal = (
                    "ROLLBACK_SKIPPED"
                    if current_status == "PENDING" and selected_action == "ROLLBACK"
                    else targets[current_status]
                )
                update = self._conn.execute(
                    """UPDATE failure_recovery_records
                       SET status = ?, version = version + 1,
                           result_reason_code = CASE
                               WHEN selected_action = 'ROLLBACK'
                               THEN 'process_restarted'
                               ELSE result_reason_code
                           END,
                           updated_at = ?, finished_at = ?
                       WHERE recovery_id = ? AND status = ?""",
                    (
                        terminal, now, now, recovery_id, current_status,
                    ),
                )
                if update.rowcount != 1:
                    continue
                counts[terminal] += 1
                self._append_agent_event(
                    task_id,
                    run_id,
                    "recovery_reconciled",
                    {
                        "recovery_id": recovery_id,
                        "from_status": current_status,
                        "status": terminal,
                        "reason": "source_run_terminal",
                    },
                )
            repair_rows = self._conn.execute(
                """SELECT f.recovery_id, f.run_id, r.task_id, r.status,
                          r.completion_reason
                   FROM failure_recovery_records f
                   JOIN agent_runs r ON r.run_id = f.run_id
                   WHERE f.status = 'REPAIR_REQUIRED'
                     AND r.status IN (
                         'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT',
                         'INTERRUPTED'
                     )"""
            ).fetchall()
            for recovery_id, run_id, task_id, run_status, completion_reason in repair_rows:
                terminal = (
                    "ABANDONED"
                    if run_status in {"CANCELLED", "TIMED_OUT", "INTERRUPTED"}
                    else "MANUAL_REQUIRED"
                )
                update = self._conn.execute(
                    """UPDATE failure_recovery_records
                       SET status = ?, version = version + 1,
                           updated_at = ?, finished_at = ?
                       WHERE recovery_id = ? AND status = 'REPAIR_REQUIRED'""",
                    (terminal, now, now, recovery_id),
                )
                if update.rowcount != 1:
                    continue
                counts[terminal] += 1
                self._append_agent_event(
                    task_id,
                    run_id,
                    "recovery_reconciled",
                    {
                        "recovery_id": recovery_id,
                        "from_status": "REPAIR_REQUIRED",
                        "status": terminal,
                        "reason": completion_reason or "source_run_terminal",
                    },
                )
        return counts

    def _decode_failure_recovery(self, row) -> dict | None:
        result = self._row_to_dict("failure_recovery_records", row)
        if result:
            result["reason"] = json.loads(result.pop("reason_json"))
        return result

    # ── Worktree lease 状态存储 ──────────────────────────────────────

    def create_worktree_lease(
        self,
        *,
        workspace_id: str,
        task_id: str,
        run_id: str,
        parent_run_id: str,
        git_root: str,
        worktree_path: str,
        branch_name: str,
        base_commit: str,
        write_scope,
        runner_backend: str,
        runner_image_digest: str | None = None,
        preserve_until: float | None = None,
    ) -> dict:
        """登记一个尚未创建真实目录的 Worktree lease。"""
        workspace_id = _validate_artifact_identifier(workspace_id, "workspace_id")
        task_id = _validate_artifact_identifier(task_id, "task_id")
        run_id = _validate_artifact_identifier(run_id, "run_id")
        parent_run_id = _validate_artifact_identifier(parent_run_id, "parent_run_id")
        expected_branch = f"minihermes/worktree/{workspace_id}"
        if branch_name != expected_branch:
            raise ValueError("worktree branch_name must be derived from workspace_id")
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base_commit or ""):
            raise ValueError("invalid worktree base_commit")
        if runner_backend != "docker":
            raise ValueError("only the strict docker runner backend is supported")
        if runner_image_digest is not None and (
            not isinstance(runner_image_digest, str)
            or not runner_image_digest
            or len(runner_image_digest) > 255
        ):
            raise ValueError("invalid runner_image_digest")

        root = Path(git_root).expanduser().resolve(strict=False)
        managed = Path(worktree_path).expanduser().resolve(strict=False)
        if not Path(git_root).expanduser().is_absolute() or not root.is_dir():
            raise ValueError("git_root must be an existing absolute directory")
        if not Path(worktree_path).expanduser().is_absolute():
            raise ValueError("worktree_path must be absolute")
        try:
            managed.relative_to(root)
        except ValueError:
            pass
        else:
            raise ValueError("managed worktree_path must be outside git_root")

        from agent.worktree import normalize_write_scope

        frozen_scope = normalize_write_scope(write_scope, workspace_root=root)
        scope_json = json.dumps(
            list(frozen_scope), ensure_ascii=False, separators=(",", ":")
        )
        now = time.time()
        with self._transaction():
            run = self._conn.execute(
                "SELECT task_id, parent_run_id FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if not run:
                raise KeyError(f"unknown agent run: {run_id}")
            if run[0] != task_id:
                raise ValueError("worktree lease task does not own run")
            if run[1] != parent_run_id:
                raise ValueError("worktree lease parent_run_id does not match run")
            self._conn.execute(
                """INSERT INTO worktree_leases
                   (workspace_id, task_id, run_id, parent_run_id, git_root,
                    worktree_path, branch_name, base_commit, write_scope_json,
                    runner_backend, runner_image_digest, lease_status,
                    cleanup_status, created_at, updated_at, preserve_until)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PROVISIONING',
                           'PENDING', ?, ?, ?)""",
                (
                    workspace_id, task_id, run_id, parent_run_id, str(root),
                    str(managed), branch_name, base_commit.lower(), scope_json,
                    runner_backend, runner_image_digest, now, now, preserve_until,
                ),
            )
            self._append_agent_event(
                task_id,
                run_id,
                "worktree_lease_provisioning",
                {"workspace_id": workspace_id},
            )
        return self.get_worktree_lease(workspace_id)

    def transition_worktree_lease(
        self,
        workspace_id: str,
        *,
        status: str,
        failure_code: str | None = None,
        failure_message: str | None = None,
        diff_relpath: str | None = None,
        diff_hash: str | None = None,
        change_manifest_relpath: str | None = None,
        change_manifest_hash: str | None = None,
        preserve_until: float | None = None,
    ) -> dict:
        """执行受控 lease 状态迁移；Git 副作用由 WorkspaceManager 负责。"""
        workspace_id = _validate_artifact_identifier(workspace_id, "workspace_id")
        _validate_status(status, _WORKTREE_LEASE_STATUSES, "lease_status")
        paths = {
            "diff_relpath": _validate_artifact_relpath(diff_relpath, "diff_relpath"),
            "change_manifest_relpath": _validate_artifact_relpath(
                change_manifest_relpath, "change_manifest_relpath"
            ),
        }
        for value, field in (
            (diff_hash, "diff_hash"),
            (change_manifest_hash, "change_manifest_hash"),
        ):
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"invalid {field}")
        if status == "FAILED" and not failure_code:
            raise ValueError("FAILED worktree lease requires failure_code")
        if failure_code is not None:
            failure_code = _validate_artifact_identifier(failure_code, "failure_code")
        if failure_message is not None:
            failure_message = str(failure_message)[:500]

        now = time.time()
        with self._transaction():
            row = self._conn.execute(
                """SELECT task_id, run_id, lease_status FROM worktree_leases
                   WHERE workspace_id = ?""",
                (workspace_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown worktree lease: {workspace_id}")
            task_id, run_id, current = row
            if status not in _WORKTREE_LEASE_TRANSITIONS[current]:
                raise RuntimeError(
                    f"illegal worktree lease transition: {current} -> {status}"
                )
            update = self._conn.execute(
                """UPDATE worktree_leases
                   SET lease_status = ?, failure_code = ?, failure_message = ?,
                       diff_relpath = COALESCE(?, diff_relpath),
                       diff_hash = COALESCE(?, diff_hash),
                       change_manifest_relpath = COALESCE(?, change_manifest_relpath),
                       change_manifest_hash = COALESCE(?, change_manifest_hash),
                       preserve_until = COALESCE(?, preserve_until), updated_at = ?
                   WHERE workspace_id = ? AND lease_status = ?""",
                (
                    status, failure_code, failure_message, paths["diff_relpath"],
                    diff_hash, paths["change_manifest_relpath"],
                    change_manifest_hash, preserve_until, now, workspace_id, current,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("worktree lease transition lost")
            self._append_agent_event(
                task_id,
                run_id,
                "worktree_lease_transition",
                {
                    "workspace_id": workspace_id,
                    "from_status": current,
                    "to_status": status,
                    "failure_code": failure_code,
                },
            )
        return self.get_worktree_lease(workspace_id)

    def set_worktree_cleanup_status(
        self,
        workspace_id: str,
        *,
        cleanup_status: str,
        failure_message: str | None = None,
    ) -> dict:
        """独立记录清理结果，绝不改写候选是否已合并。"""
        workspace_id = _validate_artifact_identifier(workspace_id, "workspace_id")
        _validate_status(
            cleanup_status, _WORKTREE_CLEANUP_STATUSES, "cleanup_status"
        )
        now = time.time()
        with self._transaction():
            row = self._conn.execute(
                """SELECT task_id, run_id, lease_status, cleanup_status
                   FROM worktree_leases WHERE workspace_id = ?""",
                (workspace_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown worktree lease: {workspace_id}")
            task_id, run_id, lease_status, current_cleanup = row
            if lease_status in {"PROVISIONING", "READY", "RUNNING", "INTEGRATING"}:
                raise RuntimeError("cannot clean an active worktree lease")
            if current_cleanup == "SUCCEEDED" and cleanup_status != "SUCCEEDED":
                raise RuntimeError("successful worktree cleanup cannot be reopened")
            update = self._conn.execute(
                """UPDATE worktree_leases
                   SET cleanup_status = ?, failure_message = COALESCE(?, failure_message),
                       updated_at = ? WHERE workspace_id = ?""",
                (
                    cleanup_status,
                    str(failure_message)[:500] if failure_message is not None else None,
                    now,
                    workspace_id,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("worktree cleanup transition lost")
            self._append_agent_event(
                task_id,
                run_id,
                "worktree_cleanup_status",
                {
                    "workspace_id": workspace_id,
                    "cleanup_status": cleanup_status,
                },
            )
        return self.get_worktree_lease(workspace_id)

    def get_worktree_lease(self, workspace_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM worktree_leases WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        return self._worktree_lease_to_dict(row)

    def get_worktree_lease_for_run(self, run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM worktree_leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return self._worktree_lease_to_dict(row)

    def list_worktree_leases(
        self, *, parent_run_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        bounded_limit = max(1, min(int(limit), 200))
        if parent_run_id is None:
            rows = self._conn.execute(
                """SELECT * FROM worktree_leases
                   ORDER BY created_at DESC, workspace_id LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM worktree_leases WHERE parent_run_id = ?
                   ORDER BY created_at, workspace_id LIMIT ?""",
                (parent_run_id, bounded_limit),
            ).fetchall()
        return [self._worktree_lease_to_dict(row) for row in rows]

    def list_active_worktree_leases(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM worktree_leases
               WHERE lease_status IN ('PROVISIONING', 'READY', 'RUNNING')
               ORDER BY created_at, workspace_id"""
        ).fetchall()
        return [self._worktree_lease_to_dict(row) for row in rows]

    def _worktree_lease_to_dict(self, row) -> dict | None:
        result = self._row_to_dict("worktree_leases", row)
        if result is None:
            return None
        result["write_scope"] = json.loads(result.pop("write_scope_json"))
        return result

    # ── Worktree 显式集成状态存储 ──────────────────────────────────

    def start_worktree_integration(
        self,
        *,
        integration_id: str,
        workspace_id: str,
        integration_run_id: str,
        source_main_commit: str,
        verification_command_hash: str,
    ) -> dict:
        """原子占用一个 PRESERVED 候选并登记集成事务。"""
        integration_id = _validate_artifact_identifier(
            integration_id, "integration_id"
        )
        workspace_id = _validate_artifact_identifier(workspace_id, "workspace_id")
        integration_run_id = _validate_artifact_identifier(
            integration_run_id, "integration_run_id"
        )
        source_main_commit = _validate_git_object_id(
            source_main_commit, "source_main_commit"
        )
        verification_command_hash = _validate_sha256(
            verification_command_hash, "verification_command_hash"
        )
        now = time.time()
        with self._transaction():
            lease = self._conn.execute(
                """SELECT task_id, run_id, lease_status, cleanup_status
                   FROM worktree_leases WHERE workspace_id = ?""",
                (workspace_id,),
            ).fetchone()
            if not lease:
                raise KeyError(f"unknown worktree lease: {workspace_id}")
            source_task_id, source_run_id, lease_status, cleanup_status = lease
            if lease_status != "PRESERVED" or cleanup_status != "PENDING":
                raise RuntimeError("worktree candidate is not integration-ready")
            source_run = self._conn.execute(
                "SELECT status FROM agent_runs WHERE run_id = ?",
                (source_run_id,),
            ).fetchone()
            if not source_run or source_run[0] != "SUCCEEDED":
                raise RuntimeError("worktree source run must be SUCCEEDED")
            integration_run = self._conn.execute(
                """SELECT task_id, parent_run_id, status, agent_kind
                   FROM agent_runs WHERE run_id = ?""",
                (integration_run_id,),
            ).fetchone()
            if not integration_run:
                raise KeyError(f"unknown integration run: {integration_run_id}")
            integration_task_id, parent_run_id, run_status, agent_kind = integration_run
            if (
                parent_run_id != source_run_id
                or run_status != "RUNNING"
                or agent_kind != "worktree_integration"
            ):
                raise RuntimeError("invalid worktree integration run")
            lease_update = self._conn.execute(
                """UPDATE worktree_leases SET lease_status = 'INTEGRATING',
                          failure_code = NULL, failure_message = NULL, updated_at = ?
                   WHERE workspace_id = ? AND lease_status = 'PRESERVED'
                     AND cleanup_status = 'PENDING'""",
                (now, workspace_id),
            )
            if lease_update.rowcount != 1:
                raise RuntimeError("worktree integration lease acquisition lost")
            self._conn.execute(
                """INSERT INTO worktree_integration_records
                   (integration_id, workspace_id, integration_run_id,
                    source_main_commit, verification_command_hash, status,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'PREPARING', ?, ?)""",
                (
                    integration_id, workspace_id, integration_run_id,
                    source_main_commit, verification_command_hash, now, now,
                ),
            )
            self._append_agent_event(
                source_task_id,
                source_run_id,
                "worktree_integration_started",
                {
                    "workspace_id": workspace_id,
                    "integration_id": integration_id,
                    "integration_run_id": integration_run_id,
                },
            )
            self._append_agent_event(
                integration_task_id,
                integration_run_id,
                "worktree_integration_preparing",
                {"workspace_id": workspace_id, "integration_id": integration_id},
            )
        return self.get_worktree_integration(integration_id)

    def transition_worktree_integration(
        self,
        integration_id: str,
        *,
        status: str,
        candidate_commit: str | None = None,
        candidate_tree_hash: str | None = None,
        expected_merge_tree_hash: str | None = None,
        final_merge_commit: str | None = None,
        final_merge_tree_hash: str | None = None,
        verification_record_id: str | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
        result_artifact_relpath: str | None = None,
        result_artifact_hash: str | None = None,
    ) -> dict:
        """迁移集成事务；终态与候选 lease 在同一事务内关闭。"""
        integration_id = _validate_artifact_identifier(
            integration_id, "integration_id"
        )
        _validate_status(status, _WORKTREE_INTEGRATION_STATUSES, "integration status")
        candidate_commit = _validate_git_object_id(
            candidate_commit, "candidate_commit", optional=True
        )
        candidate_tree_hash = _validate_git_object_id(
            candidate_tree_hash, "candidate_tree_hash", optional=True
        )
        expected_merge_tree_hash = _validate_git_object_id(
            expected_merge_tree_hash, "expected_merge_tree_hash", optional=True
        )
        final_merge_commit = _validate_git_object_id(
            final_merge_commit, "final_merge_commit", optional=True
        )
        final_merge_tree_hash = _validate_git_object_id(
            final_merge_tree_hash, "final_merge_tree_hash", optional=True
        )
        if verification_record_id is not None:
            verification_record_id = _validate_artifact_identifier(
                verification_record_id, "verification_record_id"
            )
        if result_artifact_hash is not None:
            result_artifact_hash = _validate_sha256(
                result_artifact_hash, "result_artifact_hash"
            )
        if bool(result_artifact_relpath) != bool(result_artifact_hash):
            raise ValueError("result artifact path and hash must be supplied together")
        now = time.time()
        with self._transaction():
            row = self._conn.execute(
                """SELECT workspace_id, integration_run_id, status, version,
                          candidate_commit, candidate_tree_hash,
                          expected_merge_tree_hash, final_merge_commit,
                          final_merge_tree_hash, verification_record_id
                   FROM worktree_integration_records WHERE integration_id = ?""",
                (integration_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown worktree integration: {integration_id}")
            (
                workspace_id, integration_run_id, current, version,
                old_candidate_commit, old_candidate_tree, old_expected_tree,
                old_final_commit, old_final_tree, old_verification_record,
            ) = row
            if status not in _WORKTREE_INTEGRATION_TRANSITIONS[current]:
                raise RuntimeError(
                    f"illegal worktree integration transition: {current} -> {status}"
                )
            values = {
                "candidate_commit": candidate_commit or old_candidate_commit,
                "candidate_tree_hash": candidate_tree_hash or old_candidate_tree,
                "expected_merge_tree_hash": expected_merge_tree_hash or old_expected_tree,
                "final_merge_commit": final_merge_commit or old_final_commit,
                "final_merge_tree_hash": final_merge_tree_hash or old_final_tree,
                "verification_record_id": (
                    verification_record_id or old_verification_record
                ),
            }
            if status == "VERIFYING" and not all((
                values["candidate_commit"], values["candidate_tree_hash"]
            )):
                raise ValueError("VERIFYING requires an immutable candidate commit")
            if status == "READY_TO_APPLY" and not all((
                values["expected_merge_tree_hash"], values["verification_record_id"]
            )):
                raise ValueError("READY_TO_APPLY requires merge tree and verification evidence")
            if status == "MERGED" and not all((
                values["candidate_commit"], values["candidate_tree_hash"],
                values["expected_merge_tree_hash"], values["final_merge_commit"],
                values["final_merge_tree_hash"], values["verification_record_id"],
                result_artifact_relpath, result_artifact_hash,
            )):
                raise ValueError("MERGED requires complete integration evidence")
            if status == "MERGED" and (
                values["expected_merge_tree_hash"] != values["final_merge_tree_hash"]
            ):
                raise ValueError("final merge tree does not match verified merge tree")
            if status not in _WORKTREE_INTEGRATION_ACTIVE_STATUSES and (
                status != "MERGED" and not failure_code
            ):
                raise ValueError(f"{status} requires failure_code")
            if result_artifact_relpath is not None:
                result_artifact_relpath = _validate_artifact_relpath(
                    result_artifact_relpath, "result_artifact_relpath"
                )
                expected = (
                    f"{integration_run_id}/integrations/{integration_id}/result.json"
                )
                if result_artifact_relpath != expected:
                    raise ValueError("integration result artifact path is not canonical")
            finished_at = (
                now if status not in _WORKTREE_INTEGRATION_ACTIVE_STATUSES else None
            )
            update = self._conn.execute(
                """UPDATE worktree_integration_records
                   SET status = ?, candidate_commit = ?, candidate_tree_hash = ?,
                       expected_merge_tree_hash = ?, final_merge_commit = ?,
                       final_merge_tree_hash = ?, verification_record_id = ?,
                       failure_code = ?, failure_message = ?,
                       result_artifact_relpath = ?, result_artifact_hash = ?,
                       version = version + 1, updated_at = ?, finished_at = ?
                   WHERE integration_id = ? AND status = ? AND version = ?""",
                (
                    status, values["candidate_commit"], values["candidate_tree_hash"],
                    values["expected_merge_tree_hash"], values["final_merge_commit"],
                    values["final_merge_tree_hash"], values["verification_record_id"],
                    str(failure_code)[:64] if failure_code else None,
                    str(failure_message)[:500] if failure_message else None,
                    result_artifact_relpath, result_artifact_hash,
                    now, finished_at, integration_id, current, version,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("worktree integration transition lost")

            lease_target = None
            if status not in _WORKTREE_INTEGRATION_ACTIVE_STATUSES:
                lease_target = "MERGED" if status == "MERGED" else (
                    "FAILED" if status == "FAILED" else "PRESERVED"
                )
                lease_update = self._conn.execute(
                    """UPDATE worktree_leases
                       SET lease_status = ?, failure_code = ?, failure_message = ?,
                           updated_at = ?
                       WHERE workspace_id = ? AND lease_status = 'INTEGRATING'""",
                    (
                        lease_target,
                        str(failure_code)[:64] if lease_target == "FAILED" else None,
                        str(failure_message)[:500] if lease_target == "FAILED" else None,
                        now, workspace_id,
                    ),
                )
                if lease_update.rowcount != 1:
                    raise RuntimeError("worktree integration lease completion lost")

            lease_row = self._conn.execute(
                "SELECT task_id, run_id FROM worktree_leases WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            self._append_agent_event(
                lease_row[0],
                lease_row[1],
                "worktree_integration_transition",
                {
                    "workspace_id": workspace_id,
                    "integration_id": integration_id,
                    "from_status": current,
                    "to_status": status,
                    "lease_status": lease_target,
                    "failure_code": failure_code,
                },
            )
        return self.get_worktree_integration(integration_id)

    def finish_worktree_integration(self, integration_id: str, **fields) -> dict:
        status = fields.pop("status")
        if status in _WORKTREE_INTEGRATION_ACTIVE_STATUSES:
            raise ValueError("finish_worktree_integration requires a terminal status")
        return self.transition_worktree_integration(
            integration_id, status=status, **fields
        )

    def set_worktree_integration_cleanup_status(
        self,
        integration_id: str,
        *,
        cleanup_status: str,
        failure_message: str | None = None,
    ) -> dict:
        integration_id = _validate_artifact_identifier(
            integration_id, "integration_id"
        )
        _validate_status(
            cleanup_status, _WORKTREE_CLEANUP_STATUSES, "temp_cleanup_status"
        )
        with self._transaction():
            row = self._conn.execute(
                """SELECT temp_cleanup_status, version
                   FROM worktree_integration_records WHERE integration_id = ?""",
                (integration_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown worktree integration: {integration_id}")
            if row[0] == "SUCCEEDED" and cleanup_status != "SUCCEEDED":
                raise RuntimeError("successful integration cleanup cannot be reopened")
            update = self._conn.execute(
                """UPDATE worktree_integration_records
                   SET temp_cleanup_status = ?,
                       failure_message = COALESCE(?, failure_message),
                       version = version + 1, updated_at = ?
                   WHERE integration_id = ? AND version = ?""",
                (
                    cleanup_status,
                    str(failure_message)[:500] if failure_message else None,
                    time.time(), integration_id, row[1],
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("integration cleanup status update lost")
        return self.get_worktree_integration(integration_id)

    def get_worktree_integration(self, integration_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM worktree_integration_records WHERE integration_id = ?",
            (integration_id,),
        ).fetchone()
        return self._row_to_dict("worktree_integration_records", row)

    def get_latest_worktree_integration(self, workspace_id: str) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM worktree_integration_records
               WHERE workspace_id = ? ORDER BY created_at DESC, integration_id DESC
               LIMIT 1""",
            (workspace_id,),
        ).fetchone()
        return self._row_to_dict("worktree_integration_records", row)

    def list_worktree_integrations(
        self, *, workspace_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        bounded_limit = max(1, min(int(limit), 200))
        if workspace_id is None:
            rows = self._conn.execute(
                """SELECT * FROM worktree_integration_records
                   ORDER BY created_at DESC, integration_id LIMIT ?""",
                (bounded_limit,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM worktree_integration_records
                   WHERE workspace_id = ?
                   ORDER BY created_at DESC, integration_id LIMIT ?""",
                (workspace_id, bounded_limit),
            ).fetchall()
        return [
            self._row_to_dict("worktree_integration_records", row) for row in rows
        ]

    def reconcile_worktree_integrations(self) -> dict[str, int]:
        """启动时只关闭数据库中的遗留事务，不自动操作 Git 现场。"""
        now = time.time()
        count = 0
        with self._transaction():
            rows = self._conn.execute(
                """SELECT integration_id, workspace_id, status, version
                   FROM worktree_integration_records
                   WHERE status IN ('PREPARING', 'VERIFYING', 'READY_TO_APPLY')
                   ORDER BY created_at, integration_id"""
            ).fetchall()
            for integration_id, workspace_id, current, version in rows:
                update = self._conn.execute(
                    """UPDATE worktree_integration_records
                       SET status = 'INTERRUPTED', failure_code = 'runtime_restart',
                           failure_message = ?, version = version + 1,
                           updated_at = ?, finished_at = ?
                       WHERE integration_id = ? AND status = ? AND version = ?""",
                    (
                        "Runtime restarted during explicit Worktree integration; "
                        "Git state requires inspection",
                        now, now, integration_id, current, version,
                    ),
                )
                if update.rowcount != 1:
                    continue
                lease = self._conn.execute(
                    """SELECT task_id, run_id, lease_status FROM worktree_leases
                       WHERE workspace_id = ?""",
                    (workspace_id,),
                ).fetchone()
                if lease and lease[2] == "INTEGRATING":
                    self._conn.execute(
                        """UPDATE worktree_leases
                           SET lease_status = 'PRESERVED', updated_at = ?
                           WHERE workspace_id = ? AND lease_status = 'INTEGRATING'""",
                        (now, workspace_id),
                    )
                    self._append_agent_event(
                        lease[0], lease[1], "worktree_integration_interrupted",
                        {
                            "workspace_id": workspace_id,
                            "integration_id": integration_id,
                            "reason": "runtime_restart",
                        },
                    )
                count += 1
        return {"interrupted": count}

    # ── 可复现执行元数据 ─────────────────────────────────────────────

    def create_workspace_snapshot(
        self,
        *,
        snapshot_id: str,
        run_id: str,
        workspace_id: str | None = None,
        workspace_root: str,
        git_root: str,
        base_commit: str | None = None,
        state_hash: str | None = None,
        capture_status: str = "UNAVAILABLE",
        reason_code: str | None = None,
        manifest_relpath: str | None = None,
        base_tree_relpath: str | None = None,
        patch_relpath: str | None = None,
        untracked_relpath: str | None = None,
        capture_fingerprint: str | None = None,
    ) -> dict:
        """登记不可变快照元数据；源码归档由制品管理器写入磁盘。"""
        _validate_status(capture_status, _SNAPSHOT_CAPTURE_STATUSES, "capture_status")
        snapshot_id = _validate_artifact_identifier(snapshot_id, "snapshot_id")
        run_id = _validate_artifact_identifier(run_id, "run_id")
        if workspace_id is not None:
            workspace_id = _validate_artifact_identifier(workspace_id, "workspace_id")
        paths = {
            "manifest_relpath": _validate_bundle_relpath(
                manifest_relpath, run_id=run_id, category="snapshots", item_id=snapshot_id,
                field="manifest_relpath",
            ),
            "base_tree_relpath": _validate_bundle_relpath(
                base_tree_relpath, run_id=run_id, category="snapshots", item_id=snapshot_id,
                field="base_tree_relpath",
            ),
            "patch_relpath": _validate_bundle_relpath(
                patch_relpath, run_id=run_id, category="snapshots", item_id=snapshot_id,
                field="patch_relpath",
            ),
            "untracked_relpath": _validate_bundle_relpath(
                untracked_relpath, run_id=run_id, category="snapshots", item_id=snapshot_id,
                field="untracked_relpath",
            ),
        }
        if not snapshot_id or not run_id or not workspace_root or not git_root:
            raise ValueError("snapshot_id, run_id, workspace_root and git_root are required")
        now = time.time()
        with self._transaction():
            if not self._conn.execute(
                "SELECT 1 FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone():
                raise KeyError(f"unknown agent run: {run_id}")
            if workspace_id is not None:
                lease = self._conn.execute(
                    "SELECT run_id FROM worktree_leases WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if not lease:
                    raise KeyError(f"unknown worktree lease: {workspace_id}")
                if lease[0] != run_id:
                    raise ValueError("worktree lease does not belong to snapshot run")
            self._conn.execute(
                """INSERT INTO workspace_snapshots
                   (snapshot_id, run_id, workspace_id, workspace_root, git_root, base_commit,
                    state_hash, capture_status, artifact_status, reason_code, manifest_relpath,
                    base_tree_relpath, patch_relpath, untracked_relpath,
                    capture_fingerprint, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'AVAILABLE', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id, run_id, workspace_id, workspace_root, git_root, base_commit,
                    state_hash, capture_status, reason_code, paths["manifest_relpath"],
                    paths["base_tree_relpath"], paths["patch_relpath"],
                    paths["untracked_relpath"], capture_fingerprint, now,
                ),
            )
        return self.get_workspace_snapshot(snapshot_id)

    def get_workspace_snapshot(self, snapshot_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workspace_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        return self._row_to_dict("workspace_snapshots", row)

    def list_workspace_snapshots(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM workspace_snapshots
               WHERE run_id = ? ORDER BY created_at, snapshot_id""",
            (run_id,),
        ).fetchall()
        return [self._row_to_dict("workspace_snapshots", row) for row in rows]

    def find_replayable_workspace_snapshot(
        self, git_root: str, capture_fingerprint: str
    ) -> dict | None:
        """查找可安全复用的完整快照，制品哈希仍由调用方二次验证。"""
        row = self._conn.execute(
            """SELECT * FROM workspace_snapshots
               WHERE git_root = ? AND capture_fingerprint = ?
                 AND capture_status = 'REPLAYABLE' AND artifact_status = 'AVAILABLE'
               ORDER BY created_at DESC LIMIT 1""",
            (git_root, capture_fingerprint),
        ).fetchone()
        return self._row_to_dict("workspace_snapshots", row)

    def create_execution_record(
        self,
        *,
        record_id: str,
        run_id: str,
        workspace_id: str | None = None,
        tool_execution_id: str,
        tool_name: str,
        command_preview: str = "",
        command_sha256: str | None = None,
        verification_key: str | None = None,
        snapshot_id: str | None = None,
        command_relpath: str | None = None,
        working_directory_rel: str | None = None,
        environment_relpath: str | None = None,
        stdout_relpath: str | None = None,
        stderr_relpath: str | None = None,
        node_run_id: str | None = None,
        replayed_from_record_id: str | None = None,
    ) -> dict:
        """创建尚未完成的执行记录，所有制品路径只能是相对路径。"""
        if not record_id or not run_id or not tool_execution_id or not tool_name:
            raise ValueError("record_id, run_id, tool_execution_id and tool_name are required")
        record_id = _validate_artifact_identifier(record_id, "record_id")
        run_id = _validate_artifact_identifier(run_id, "run_id")
        tool_execution_id = _validate_artifact_identifier(tool_execution_id, "tool_execution_id")
        if workspace_id is not None:
            workspace_id = _validate_artifact_identifier(workspace_id, "workspace_id")
        if node_run_id is not None:
            node_run_id = _validate_artifact_identifier(node_run_id, "node_run_id")
        if command_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", command_sha256):
            raise ValueError("invalid command_sha256")
        if verification_key is not None and not re.fullmatch(
            r"[0-9a-f]{64}", verification_key
        ):
            raise ValueError("invalid verification_key")
        paths = {
            "command_relpath": _validate_bundle_relpath(
                command_relpath, run_id=run_id, category="executions", item_id=record_id,
                field="command_relpath",
            ),
            "environment_relpath": _validate_bundle_relpath(
                environment_relpath, run_id=run_id, category="executions", item_id=record_id,
                field="environment_relpath",
            ),
            "stdout_relpath": _validate_bundle_relpath(
                stdout_relpath, run_id=run_id, category="executions", item_id=record_id,
                field="stdout_relpath",
            ),
            "stderr_relpath": _validate_bundle_relpath(
                stderr_relpath, run_id=run_id, category="executions", item_id=record_id,
                field="stderr_relpath",
            ),
        }
        if working_directory_rel is not None:
            cwd_path = PurePosixPath(working_directory_rel)
            if (
                not isinstance(working_directory_rel, str)
                or not working_directory_rel
                or "\\" in working_directory_rel
                or "\x00" in working_directory_rel
                or cwd_path.is_absolute()
                or any(part in ("", "..") for part in cwd_path.parts)
            ):
                raise ValueError("invalid working_directory_rel")
            working_directory_rel = cwd_path.as_posix()
        now = time.time()
        with self._transaction():
            tool_row = self._conn.execute(
                "SELECT run_id, tool_name FROM tool_executions WHERE execution_id = ?",
                (tool_execution_id,),
            ).fetchone()
            if not tool_row:
                raise KeyError(f"unknown tool execution: {tool_execution_id}")
            if tool_row[0] != run_id:
                raise ValueError("tool execution does not belong to run")
            if tool_name not in _REPRODUCIBLE_TOOL_NAMES or tool_row[1] != tool_name:
                raise ValueError("execution records are only supported for matching bash executions")
            if workspace_id is not None:
                lease = self._conn.execute(
                    "SELECT run_id FROM worktree_leases WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if not lease:
                    raise KeyError(f"unknown worktree lease: {workspace_id}")
                if lease[0] != run_id:
                    integration = self._conn.execute(
                        """SELECT 1 FROM worktree_integration_records
                           WHERE workspace_id = ? AND integration_run_id = ?
                             AND status IN ('PREPARING', 'VERIFYING', 'READY_TO_APPLY')""",
                        (workspace_id, run_id),
                    ).fetchone()
                    if not integration:
                        raise ValueError(
                            "worktree lease does not belong to execution run"
                        )
            if node_run_id is not None:
                node_row = self._conn.execute(
                    "SELECT agent_run_id FROM workflow_node_runs WHERE node_run_id = ?",
                    (node_run_id,),
                ).fetchone()
                if not node_row:
                    raise KeyError(f"unknown workflow node run: {node_run_id}")
                if node_row[0] != run_id:
                    raise ValueError("workflow node run does not belong to execution run")
            if snapshot_id:
                snapshot = self._conn.execute(
                    """SELECT artifact_status, run_id, workspace_id FROM workspace_snapshots
                       WHERE snapshot_id = ?""",
                    (snapshot_id,),
                ).fetchone()
                if not snapshot:
                    raise KeyError(f"unknown workspace snapshot: {snapshot_id}")
                if snapshot[0] != "AVAILABLE":
                    raise RuntimeError("cannot attach an execution record to purged snapshot")
                if workspace_id is not None and snapshot[2] != workspace_id:
                    raise ValueError("workspace snapshot and execution workspace do not match")
            if replayed_from_record_id:
                _validate_artifact_identifier(replayed_from_record_id, "replayed_from_record_id")
            if replayed_from_record_id and not self._conn.execute(
                "SELECT 1 FROM execution_records WHERE record_id = ?",
                (replayed_from_record_id,),
            ).fetchone():
                raise KeyError(f"unknown source execution record: {replayed_from_record_id}")
            self._conn.execute(
                """INSERT INTO execution_records
                   (record_id, run_id, workspace_id, tool_execution_id, snapshot_id,
                    node_run_id, tool_name,
                    command_preview, command_sha256, verification_key,
                    command_relpath, working_directory_rel,
                    environment_relpath, stdout_relpath, stderr_relpath,
                    artifact_status, replayed_from_record_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           'INCOMPLETE', ?, ?)""",
                (
                    record_id, run_id, workspace_id, tool_execution_id, snapshot_id,
                    node_run_id, tool_name,
                    str(command_preview or ""), command_sha256, verification_key,
                    paths["command_relpath"],
                    working_directory_rel, paths["environment_relpath"],
                    paths["stdout_relpath"], paths["stderr_relpath"],
                    replayed_from_record_id, now,
                ),
            )
        return self.get_execution_record(record_id)

    def finish_execution_record(
        self,
        *,
        record_id: str,
        log_status: str,
        reproducibility_status: str,
        artifact_status: str = "AVAILABLE",
        exit_code: int | None = None,
        termination_reason: str | None = None,
        replay_status: str = "NOT_REQUESTED",
        finished_at: float | None = None,
    ) -> dict:
        _validate_status(log_status, _LOG_STATUSES, "log_status")
        _validate_status(reproducibility_status, _REPRODUCIBILITY_STATUSES, "reproducibility_status")
        _validate_status(artifact_status, _ARTIFACT_STATUSES, "artifact_status")
        _validate_status(replay_status, _REPLAY_STATUSES, "replay_status")
        with self._transaction():
            record = self._conn.execute(
                """SELECT snapshot_id, command_relpath, working_directory_rel,
                          environment_relpath, stdout_relpath, stderr_relpath,
                          artifact_status
                   FROM execution_records WHERE record_id = ? AND finished_at IS NULL""",
                (record_id,),
            ).fetchone()
            if not record:
                raise RuntimeError(f"execution record is already finished or unknown: {record_id}")
            if record[6] == "PURGED" and artifact_status != "PURGED":
                raise RuntimeError("purged execution record cannot become available again")
            if reproducibility_status == "REPLAYABLE":
                snapshot_id, command_path, cwd, environment_path, stdout_path, stderr_path, _ = record
                snapshot = self._conn.execute(
                    """SELECT capture_status, artifact_status FROM workspace_snapshots
                       WHERE snapshot_id = ?""",
                    (snapshot_id,),
                ).fetchone() if snapshot_id else None
                if (
                    artifact_status != "AVAILABLE"
                    or log_status != "COMPLETE"
                    or not snapshot
                    or snapshot[0] != "REPLAYABLE"
                    or snapshot[1] != "AVAILABLE"
                    or not all((command_path, cwd, environment_path, stdout_path, stderr_path))
                ):
                    raise ValueError(
                        "REPLAYABLE record requires a replayable snapshot and complete available artifacts"
                    )
            update = self._conn.execute(
                """UPDATE execution_records
                   SET log_status = ?, reproducibility_status = ?,
                       artifact_status = ?, exit_code = ?, termination_reason = ?,
                       replay_status = ?, finished_at = ?
                   WHERE record_id = ? AND finished_at IS NULL""",
                (
                    log_status, reproducibility_status, artifact_status, exit_code,
                    termination_reason, replay_status, finished_at or time.time(), record_id,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError(f"execution record completion lost: {record_id}")
        return self.get_execution_record(record_id)

    def mark_execution_artifact_status(self, record_id: str, artifact_status: str) -> dict:
        _validate_status(artifact_status, _ARTIFACT_STATUSES, "artifact_status")
        with self._transaction():
            update = self._conn.execute(
                """UPDATE execution_records SET artifact_status = ?
                   WHERE record_id = ?
                     AND NOT (artifact_status = 'PURGED' AND ? != 'PURGED')""",
                (artifact_status, record_id, artifact_status),
            )
            if update.rowcount != 1:
                existing = self._conn.execute(
                    "SELECT artifact_status FROM execution_records WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if existing and existing[0] == "PURGED":
                    raise RuntimeError("purged execution record cannot become available again")
                raise KeyError(f"unknown execution record: {record_id}")
        return self.get_execution_record(record_id)

    def update_execution_replay_status(self, record_id: str, replay_status: str) -> dict:
        """更新源记录的最近一次重放结果，不改写其原始执行终态。"""
        _validate_status(replay_status, _REPLAY_STATUSES, "replay_status")
        with self._transaction():
            update = self._conn.execute(
                """UPDATE execution_records SET replay_status = ?
                   WHERE record_id = ?""",
                (replay_status, record_id),
            )
            if update.rowcount != 1:
                raise KeyError(f"unknown execution record: {record_id}")
        return self.get_execution_record(record_id)

    def get_execution_record(self, record_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM execution_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        return self._row_to_dict("execution_records", row)

    def get_execution_record_for_tool_execution(
        self, tool_execution_id: str
    ) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM execution_records
               WHERE tool_execution_id = ?""",
            (tool_execution_id,),
        ).fetchone()
        return self._row_to_dict("execution_records", row)

    def list_execution_records(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM execution_records
               WHERE run_id = ? ORDER BY created_at, record_id""",
            (run_id,),
        ).fetchall()
        return [self._row_to_dict("execution_records", row) for row in rows]

    def find_execution_records_by_prefix(self, record_id_prefix: str, limit: int = 3) -> list[dict]:
        """按安全的固定前缀查找记录；最多返回少量结果供 CLI 判定歧义。"""
        if not isinstance(record_id_prefix, str) or not record_id_prefix:
            return []
        bounded_limit = max(1, min(int(limit), 20))
        rows = self._conn.execute(
            """SELECT * FROM execution_records
               WHERE substr(record_id, 1, ?) = ?
               ORDER BY created_at DESC, record_id LIMIT ?""",
            (len(record_id_prefix), record_id_prefix, bounded_limit),
        ).fetchall()
        return [self._row_to_dict("execution_records", row) for row in rows]

    def list_snapshot_references(self, snapshot_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM execution_records WHERE snapshot_id = ?
               ORDER BY created_at, record_id""",
            (snapshot_id,),
        ).fetchall()
        return [self._row_to_dict("execution_records", row) for row in rows]

    def list_artifact_bundle_owners(self) -> dict[str, set[tuple[str, str]]]:
        """返回仍受数据库管理的快照和执行制品目录归属。"""
        snapshots = self._conn.execute(
            "SELECT run_id, snapshot_id FROM workspace_snapshots"
        ).fetchall()
        executions = self._conn.execute(
            "SELECT run_id, record_id FROM execution_records"
        ).fetchall()
        return {
            "snapshots": {(row[0], row[1]) for row in snapshots},
            "executions": {(row[0], row[1]) for row in executions},
        }

    def purge_snapshot_references(self, snapshot_id: str) -> int:
        """制品被清理后，只更新引用记录的当前可用性，不改历史等级。"""
        with self._transaction():
            snapshot = self._conn.execute(
                """UPDATE workspace_snapshots SET artifact_status = 'PURGED'
                   WHERE snapshot_id = ? AND artifact_status != 'PURGED'""",
                (snapshot_id,),
            )
            if snapshot.rowcount != 1 and not self._conn.execute(
                "SELECT 1 FROM workspace_snapshots WHERE snapshot_id = ?", (snapshot_id,)
            ).fetchone():
                raise KeyError(f"unknown workspace snapshot: {snapshot_id}")
            cursor = self._conn.execute(
                """UPDATE execution_records SET artifact_status = 'PURGED'
                   WHERE snapshot_id = ? AND artifact_status != 'PURGED'""",
                (snapshot_id,),
            )
            return cursor.rowcount

    def inspect_artifact_retention_groups(
        self,
        *,
        normal_before: float,
        failed_before: float,
    ) -> list[dict]:
        """返回制品清理分组的当前判断，不修改记录。

        一个快照和所有引用它的执行记录必须作为整体处理；否则删除快照会让
        另一条仍保留的记录指向不完整材料。没有快照的执行记录各自独立成组。
        """
        with self._transaction():
            return self._artifact_retention_groups_locked(
                normal_before=normal_before,
                failed_before=failed_before,
            )

    def claim_expired_artifact_retention_groups(
        self,
        *,
        normal_before: float,
        failed_before: float,
    ) -> list[dict]:
        """原子认领可清理组，并先让数据库停止宣称制品可用。

        文件删除在事务外执行。即使进程在删除前崩溃，后续重放也会因
        ``PURGED`` 而被拒绝，避免把不完整材料当作可重放现场。
        """
        with self._transaction():
            groups = self._artifact_retention_groups_locked(
                normal_before=normal_before,
                failed_before=failed_before,
            )
            claimed: list[dict] = []
            for group in groups:
                if not group["eligible"]:
                    continue
                if group["snapshot_id"]:
                    self._conn.execute(
                        """UPDATE workspace_snapshots SET artifact_status = 'PURGED'
                           WHERE snapshot_id = ? AND artifact_status != 'PURGED'""",
                        (group["snapshot_id"],),
                    )
                record_ids = group["record_ids"]
                if record_ids:
                    placeholders = ",".join("?" for _ in record_ids)
                    self._conn.execute(
                        f"""UPDATE execution_records SET artifact_status = 'PURGED'
                            WHERE record_id IN ({placeholders})
                              AND artifact_status != 'PURGED'""",
                        record_ids,
                    )
                claimed.append(group)
            return claimed

    def claim_capacity_artifact_retention_groups(
        self,
        group_keys: set[tuple[str, str]],
    ) -> list[dict]:
        """在容量压力下认领已结束的成功制品组。

        调用方只能传入前一次只读检查返回的 ``(group_type, group_id)``。
        本方法会在同一事务内重新检查活动 Run、活动执行和失败标记，不能因
        旧列表或并发重放而删除仍受保护的现场。
        """
        if not group_keys:
            return []
        with self._transaction():
            groups = self._artifact_retention_groups_locked(
                normal_before=0.0,
                failed_before=0.0,
            )
            claimed: list[dict] = []
            for group in groups:
                key = (group["group_type"], group["group_id"])
                if key not in group_keys:
                    continue
                if (
                    group["failed"]
                    or group["already_purged"]
                    or group["blocked_reason"] not in {None, "retention"}
                ):
                    continue
                if group["snapshot_id"]:
                    self._conn.execute(
                        """UPDATE workspace_snapshots SET artifact_status = 'PURGED'
                           WHERE snapshot_id = ? AND artifact_status != 'PURGED'""",
                        (group["snapshot_id"],),
                    )
                record_ids = group["record_ids"]
                if record_ids:
                    placeholders = ",".join("?" for _ in record_ids)
                    self._conn.execute(
                        f"""UPDATE execution_records SET artifact_status = 'PURGED'
                            WHERE record_id IN ({placeholders})
                              AND artifact_status != 'PURGED'""",
                        record_ids,
                    )
                claimed.append(group)
            return claimed

    def _artifact_retention_groups_locked(
        self,
        *,
        normal_before: float,
        failed_before: float,
    ) -> list[dict]:
        snapshots = self._conn.execute(
            """SELECT snapshot_id, run_id, created_at, artifact_status
               FROM workspace_snapshots ORDER BY created_at, snapshot_id"""
        ).fetchall()
        groups: list[dict] = []
        covered_records: set[str] = set()
        for snapshot_id, owner_run_id, created_at, artifact_status in snapshots:
            rows = self._conn.execute(
                """SELECT er.record_id, er.run_id, er.artifact_status,
                          er.created_at, er.finished_at, er.exit_code,
                          er.termination_reason, ar.status AS run_status,
                          te.status AS tool_status
                   FROM execution_records er
                   LEFT JOIN agent_runs ar ON ar.run_id = er.run_id
                   LEFT JOIN tool_executions te
                     ON te.execution_id = er.tool_execution_id
                   WHERE er.snapshot_id = ?
                   ORDER BY er.created_at, er.record_id""",
                (snapshot_id,),
            ).fetchall()
            covered_records.update(row[0] for row in rows)
            groups.append(self._evaluate_artifact_group_locked(
                group_type="snapshot",
                group_id=snapshot_id,
                snapshot_id=snapshot_id,
                snapshot_run_id=owner_run_id,
                snapshot_created_at=float(created_at),
                snapshot_artifact_status=artifact_status,
                rows=rows,
                normal_before=normal_before,
                failed_before=failed_before,
            ))

        orphan_rows = self._conn.execute(
            """SELECT er.record_id, er.run_id, er.artifact_status,
                      er.created_at, er.finished_at, er.exit_code,
                      er.termination_reason, ar.status AS run_status,
                      te.status AS tool_status
               FROM execution_records er
               LEFT JOIN agent_runs ar ON ar.run_id = er.run_id
               LEFT JOIN tool_executions te ON te.execution_id = er.tool_execution_id
               WHERE er.snapshot_id IS NULL
               ORDER BY er.created_at, er.record_id"""
        ).fetchall()
        for row in orphan_rows:
            if row[0] in covered_records:
                continue
            groups.append(self._evaluate_artifact_group_locked(
                group_type="execution",
                group_id=row[0],
                snapshot_id=None,
                snapshot_run_id=None,
                snapshot_created_at=float(row[3]),
                snapshot_artifact_status="AVAILABLE",
                rows=[row],
                normal_before=normal_before,
                failed_before=failed_before,
            ))
        return groups

    def _evaluate_artifact_group_locked(
        self,
        *,
        group_type: str,
        group_id: str,
        snapshot_id: str | None,
        snapshot_run_id: str | None,
        snapshot_created_at: float,
        snapshot_artifact_status: str,
        rows: list,
        normal_before: float,
        failed_before: float,
    ) -> dict:
        active_statuses = ("QUEUED", "RUNNING", "CANCEL_REQUESTED")
        record_ids = [row[0] for row in rows]
        run_ids = {row[1] for row in rows if row[1]}
        if snapshot_run_id:
            run_ids.add(snapshot_run_id)
        placeholders = ",".join("?" for _ in run_ids)
        active_run = False
        if run_ids:
            active_run = bool(self._conn.execute(
                f"""SELECT 1 FROM agent_runs
                    WHERE (run_id IN ({placeholders})
                           OR parent_run_id IN ({placeholders}))
                      AND status IN ({','.join('?' for _ in active_statuses)})
                    LIMIT 1""",
                (*run_ids, *run_ids, *active_statuses),
            ).fetchone())

        nonpurged = [row for row in rows if row[2] != "PURGED"]
        snapshot_purged = snapshot_id is not None and snapshot_artifact_status == "PURGED"
        if snapshot_purged:
            reason = "already_purged"
        elif active_run:
            reason = "active_run"
        elif any(row[4] is None or row[8] == "RUNNING" for row in nonpurged):
            reason = "active_execution"
        else:
            reason = None

        failed = False
        oldest_required_time = snapshot_created_at
        for row in nonpurged:
            _, _, _, created_at, finished_at, exit_code, termination, run_status, tool_status = row
            finished = float(finished_at if finished_at is not None else created_at)
            is_failed = (
                tool_status != "SUCCEEDED"
                or run_status != "SUCCEEDED"
                or (exit_code is not None and int(exit_code) != 0)
                or (termination is not None and termination != "exited")
            )
            failed = failed or is_failed
            oldest_required_time = max(oldest_required_time, finished)
            if reason is None:
                deadline = failed_before if is_failed else normal_before
                if finished > deadline:
                    reason = "failed_retention" if is_failed else "retention"

        if not rows and snapshot_run_id:
            owner = self._conn.execute(
                "SELECT status FROM agent_runs WHERE run_id = ?", (snapshot_run_id,)
            ).fetchone()
            if owner and owner[0] in active_statuses:
                reason = "active_run"
            elif snapshot_created_at > normal_before:
                reason = "retention"

        return {
            "group_type": group_type,
            "group_id": group_id,
            "snapshot_id": snapshot_id,
            "snapshot_run_id": snapshot_run_id,
            "record_ids": record_ids,
            "record_locations": [(row[0], row[1]) for row in rows if row[1]],
            "record_runs": sorted(run_ids),
            "failed": failed,
            "created_at": snapshot_created_at,
            "last_activity_at": oldest_required_time,
            "already_purged": snapshot_purged or (bool(rows) and not nonpurged),
            "eligible": reason is None,
            "blocked_reason": reason,
        }

    # ── Graph Engineering 工作流状态 ───────────────────────────────

    def create_workflow_run(
        self,
        *,
        workflow_run_id: str,
        root_task_id: str,
        workflow_id: str,
        workflow_version: int,
        definition_snapshot: dict | str,
        state: dict | str,
        conversation_id: str | None = None,
        root_agent_run_id: str | None = None,
    ) -> dict:
        """登记一个尚未执行的 GraphRun，定义快照和状态都必须先通过校验。"""
        _validate_artifact_identifier(workflow_run_id, "workflow_run_id")
        _validate_artifact_identifier(root_task_id, "root_task_id")
        _validate_workflow_identifier(workflow_id, "workflow_id")
        if isinstance(workflow_version, bool) or not isinstance(workflow_version, int) or workflow_version < 1:
            raise ValueError("invalid workflow_version")
        if root_agent_run_id is not None:
            _validate_artifact_identifier(root_agent_run_id, "root_agent_run_id")
        definition_json, definition = _normalize_workflow_json(
            definition_snapshot, "definition_snapshot_json", limit=_WORKFLOW_STATE_MAX_BYTES
        )
        try:
            from agent.graph import workflow_definition_from_record

            restored = workflow_definition_from_record(definition)
        except (ImportError, ValueError) as exc:
            raise ValueError(f"invalid definition_snapshot_json: {exc}") from exc
        if restored.workflow_id != workflow_id or restored.version != workflow_version:
            raise ValueError("definition snapshot does not match workflow identity")
        state_json = _normalize_workflow_state(state, root_task_id=root_task_id)
        now = time.time()
        with self._transaction():
            if not self._conn.execute(
                "SELECT 1 FROM agent_tasks WHERE task_id = ?", (root_task_id,)
            ).fetchone():
                raise KeyError(f"unknown root agent task: {root_task_id}")
            if root_agent_run_id:
                run_row = self._conn.execute(
                    "SELECT task_id FROM agent_runs WHERE run_id = ?", (root_agent_run_id,)
                ).fetchone()
                if not run_row:
                    raise KeyError(f"unknown root agent run: {root_agent_run_id}")
                if run_row[0] != root_task_id:
                    raise ValueError("root agent run does not belong to root task")
            self._conn.execute(
                """INSERT INTO workflow_runs
                   (workflow_run_id, root_task_id, root_agent_run_id, workflow_id,
                    workflow_version, definition_snapshot_json, conversation_id,
                    status, state_json, state_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, 0, ?)""",
                (
                    workflow_run_id, root_task_id, root_agent_run_id, workflow_id,
                    workflow_version, definition_json, conversation_id, state_json, now,
                ),
            )
        return self.get_workflow_run(workflow_run_id)

    def start_workflow_run(self, workflow_run_id: str) -> dict:
        with self._transaction():
            update = self._conn.execute(
                """UPDATE workflow_runs SET status = 'RUNNING', started_at = ?
                   WHERE workflow_run_id = ? AND status = 'QUEUED'""",
                (time.time(), workflow_run_id),
            )
            if update.rowcount != 1:
                raise RuntimeError(f"illegal workflow start transition: {workflow_run_id}")
        return self.get_workflow_run(workflow_run_id)

    def create_and_start_workflow_agent_node(
        self,
        *,
        workflow_run_id: str,
        root_task_id: str,
        root_agent_run_id: str,
        workflow_id: str,
        workflow_version: int,
        definition_snapshot: dict | str,
        state: dict | str,
        conversation_id: str | None,
        node_run_id: str,
        node_id: str,
        agent_task_id: str,
        agent_run_id: str,
    ) -> dict:
        """原子登记正在运行的 GraphRun、AGENT NodeRun 与 ``__start__`` 边。"""
        _validate_artifact_identifier(workflow_run_id, "workflow_run_id")
        _validate_artifact_identifier(root_task_id, "root_task_id")
        _validate_artifact_identifier(root_agent_run_id, "root_agent_run_id")
        _validate_artifact_identifier(node_run_id, "node_run_id")
        _validate_artifact_identifier(agent_task_id, "agent_task_id")
        _validate_artifact_identifier(agent_run_id, "agent_run_id")
        _validate_workflow_identifier(workflow_id, "workflow_id")
        _validate_workflow_identifier(node_id, "node_id")
        if isinstance(workflow_version, bool) or not isinstance(workflow_version, int) or workflow_version < 1:
            raise ValueError("invalid workflow_version")
        if root_agent_run_id != agent_run_id or root_task_id != agent_task_id:
            raise ValueError("main workflow root must match its agent node")
        definition_json, definition = _normalize_workflow_json(
            definition_snapshot, "definition_snapshot_json", limit=_WORKFLOW_STATE_MAX_BYTES
        )
        try:
            from agent.graph import workflow_definition_from_record

            restored = workflow_definition_from_record(definition)
        except (ImportError, ValueError) as exc:
            raise ValueError(f"invalid definition_snapshot_json: {exc}") from exc
        if restored.workflow_id != workflow_id or restored.version != workflow_version:
            raise ValueError("definition snapshot does not match workflow identity")
        if restored.start_node_id != node_id:
            raise ValueError("main workflow node must be the definition start node")
        self._validate_workflow_node_definition(
            definition_json, node_id=node_id, node_kind="AGENT"
        )
        state_json = _normalize_workflow_state(state, root_task_id=root_task_id)
        now = time.time()
        with self._transaction():
            task = self._conn.execute(
                "SELECT status FROM agent_tasks WHERE task_id = ?", (root_task_id,)
            ).fetchone()
            if not task:
                raise KeyError(f"unknown root agent task: {root_task_id}")
            if task[0] != "RUNNING":
                raise RuntimeError("main workflow requires a running agent task")
            agent_run = self._conn.execute(
                "SELECT task_id, status FROM agent_runs WHERE run_id = ?", (agent_run_id,)
            ).fetchone()
            if not agent_run:
                raise KeyError(f"unknown root agent run: {agent_run_id}")
            if agent_run[0] != root_task_id:
                raise ValueError("root agent run does not belong to root task")
            if agent_run[1] != "RUNNING":
                raise RuntimeError("main workflow requires a running agent run")
            self._conn.execute(
                """INSERT INTO workflow_runs
                   (workflow_run_id, root_task_id, root_agent_run_id, workflow_id,
                    workflow_version, definition_snapshot_json, conversation_id,
                    status, state_json, state_version, created_at, started_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?, 0, ?, ?)""",
                (
                    workflow_run_id, root_task_id, root_agent_run_id, workflow_id,
                    workflow_version, definition_json, conversation_id, state_json, now, now,
                ),
            )
            self._conn.execute(
                """INSERT INTO workflow_node_runs
                   (node_run_id, workflow_run_id, node_id, branch_key, attempt,
                    node_kind, status, input_state_version, agent_task_id,
                    agent_run_id, created_at, started_at)
                   VALUES (?, ?, ?, 'main', 1, 'AGENT', 'RUNNING', 0, ?, ?, ?, ?)""",
                (node_run_id, workflow_run_id, node_id, agent_task_id, agent_run_id, now, now),
            )
            self._conn.execute(
                """INSERT INTO workflow_transitions
                   (workflow_run_id, from_node_run_id, to_node_run_id, edge_id,
                    reason_code, state_version, created_at)
                   VALUES (?, NULL, ?, '__start__', 'run_started', 0, ?)""",
                (workflow_run_id, node_run_id, now),
            )
        return {
            "workflow_run": self.get_workflow_run(workflow_run_id),
            "node_run": self.get_workflow_node_run(node_run_id),
        }

    def finish_agent_run_with_workflow_node(
        self,
        *,
        run_id: str,
        task_id: str,
        status: str,
        completion_reason: str,
        end_session_id: str | None,
        result_preview: str = "",
        error_code: str | None = None,
        error_message: str | None = None,
        iterations_used: int = 0,
        provider_attempts: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        reasoning_tokens: int = 0,
        workflow_run_id: str,
        node_run_id: str,
        expected_state_version: int,
        state: dict | str,
        output_summary: dict | str | None,
    ) -> None:
        """原子完成 G1 主 AgentRun 与其唯一的 Graph Node。"""
        terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED"}
        if status not in terminal:
            raise ValueError(f"not a terminal run status: {status}")
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int) or expected_state_version < 0:
            raise ValueError("invalid expected_state_version")
        _validate_artifact_identifier(run_id, "run_id")
        _validate_artifact_identifier(task_id, "task_id")
        _validate_artifact_identifier(workflow_run_id, "workflow_run_id")
        _validate_artifact_identifier(node_run_id, "node_run_id")
        summary_json = self._normalize_workflow_node_summary(output_summary)
        allowed_from = {
            "SUCCEEDED": ("RUNNING",),
            "FAILED": ("RUNNING", "CANCEL_REQUESTED"),
            "CANCELLED": ("CANCEL_REQUESTED",),
            "TIMED_OUT": ("CANCEL_REQUESTED",),
            "INTERRUPTED": ("RUNNING", "CANCEL_REQUESTED"),
        }[status]
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
        placeholders = ",".join("?" for _ in allowed_from)
        now = time.time()
        with self._transaction():
            workflow = self._conn.execute(
                """SELECT root_task_id, root_agent_run_id, state_version, status
                   FROM workflow_runs WHERE workflow_run_id = ?""",
                (workflow_run_id,),
            ).fetchone()
            if not workflow:
                raise KeyError(f"unknown workflow run: {workflow_run_id}")
            if workflow[0] != task_id or workflow[1] != run_id:
                raise ValueError("workflow root does not match agent run")
            if workflow[2] != expected_state_version:
                raise RuntimeError("workflow state version conflict")
            if workflow[3] != "RUNNING":
                raise RuntimeError("workflow is not running")
            state_json = _normalize_workflow_state(state, root_task_id=task_id)
            node = self._conn.execute(
                """SELECT workflow_run_id, agent_task_id, agent_run_id, status
                   FROM workflow_node_runs WHERE node_run_id = ?""",
                (node_run_id,),
            ).fetchone()
            if not node:
                raise KeyError(f"unknown workflow node run: {node_run_id}")
            if node[:3] != (workflow_run_id, task_id, run_id):
                raise ValueError("workflow node does not match agent run")
            if node[3] != "RUNNING":
                raise RuntimeError("workflow node is not running")

            run_update = self._conn.execute(
                f"""UPDATE agent_runs SET status = ?, end_session_id = ?,
                           finished_at = ?, completion_reason = ?, error_code = ?,
                           error_message = ?, iterations_used = ?, provider_attempts = ?,
                           prompt_tokens = ?, completion_tokens = ?, reasoning_tokens = ?
                       WHERE run_id = ? AND task_id = ?
                         AND status IN ({placeholders})""",
                (
                    status, end_session_id, now, completion_reason, error_code,
                    error_message, iterations_used, provider_attempts, prompt_tokens,
                    completion_tokens, reasoning_tokens, run_id, task_id, *allowed_from,
                ),
            )
            if run_update.rowcount != 1:
                raise RuntimeError(f"illegal terminal transition for run {run_id} -> {status}")
            task_update = self._conn.execute(
                """UPDATE agent_tasks SET status = ?, finished_at = ?,
                          result_preview = ?, error_code = ?, error_message = ?
                   WHERE task_id = ? AND status = 'RUNNING'""",
                (task_status, now, result_preview, error_code, error_message, task_id),
            )
            if task_update.rowcount != 1:
                raise RuntimeError(f"illegal terminal transition for task {task_id}")
            workflow_update = self._conn.execute(
                """UPDATE workflow_runs SET state_json = ?, state_version = state_version + 1,
                           status = ?, completion_reason = ?, error_code = ?,
                           error_message = ?, finished_at = ?
                   WHERE workflow_run_id = ? AND state_version = ? AND status = 'RUNNING'""",
                (
                    state_json, status, completion_reason, error_code, error_message,
                    now, workflow_run_id, expected_state_version,
                ),
            )
            if workflow_update.rowcount != 1:
                raise RuntimeError("workflow completion lost")
            node_update = self._conn.execute(
                """UPDATE workflow_node_runs
                   SET status = ?, output_state_version = ?, output_summary_json = ?,
                       error_code = ?, error_message = ?, finished_at = ?
                   WHERE node_run_id = ? AND status = 'RUNNING'""",
                (
                    status, expected_state_version + 1, summary_json,
                    error_code, error_message, now, node_run_id,
                ),
            )
            if node_update.rowcount != 1:
                raise RuntimeError("workflow node completion lost")
            self._conn.execute(
                """INSERT INTO workflow_transitions
                   (workflow_run_id, from_node_run_id, to_node_run_id, edge_id,
                    reason_code, state_version, created_at)
                   VALUES (?, ?, NULL, '__end__', ?, ?, ?)""",
                (workflow_run_id, node_run_id, "agent_" + status.lower(), expected_state_version + 1, now),
            )
            self._append_agent_event(
                task_id, run_id, run_event, {"completion_reason": completion_reason}
            )
            self._append_agent_event(task_id, run_id, task_event)
            self._finalize_repair_recoveries_locked(
                run_id=run_id,
                task_id=task_id,
                run_status=status,
                completion_reason=completion_reason,
                now=now,
            )

    def update_workflow_state(
        self,
        *,
        workflow_run_id: str,
        expected_state_version: int,
        state: dict | str,
    ) -> dict:
        """CAS 更新状态；调用者必须声明自己基于哪个检查点计算结果。"""
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int) or expected_state_version < 0:
            raise ValueError("invalid expected_state_version")
        with self._transaction():
            row = self._conn.execute(
                """SELECT root_task_id, state_version, status FROM workflow_runs
                   WHERE workflow_run_id = ?""",
                (workflow_run_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown workflow run: {workflow_run_id}")
            root_task_id, actual_version, status = row
            if status not in _WORKFLOW_RUN_ACTIVE_STATUSES:
                raise RuntimeError("cannot update state of a terminal workflow")
            if actual_version != expected_state_version:
                raise RuntimeError("workflow state version conflict")
            state_json = _normalize_workflow_state(state, root_task_id=root_task_id)
            update = self._conn.execute(
                """UPDATE workflow_runs SET state_json = ?, state_version = state_version + 1
                   WHERE workflow_run_id = ? AND state_version = ?""",
                (state_json, workflow_run_id, expected_state_version),
            )
            if update.rowcount != 1:
                raise RuntimeError("workflow state version conflict")
        return self.get_workflow_run(workflow_run_id)

    def complete_workflow_node(
        self,
        *,
        node_run_id: str,
        status: str,
        expected_state_version: int,
        state: dict | str,
        output_summary: dict | str | None,
        edge_id: str,
        reason_code: str,
        error_code: str | None = None,
        error_message: str | None = None,
        next_node: dict | None = None,
    ) -> dict:
        """原子提交节点结果、状态检查点和下一条已选边。

        ``next_node`` 是下一节点的受限登记信息；省略时只能通过 ``__end__``
        结束流程。真正的节点处理和路由仍由后续 GraphRunner 负责。
        """
        if status not in _WORKFLOW_NODE_TERMINAL_STATUSES:
            raise ValueError("complete_workflow_node requires a terminal node status")
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int) or expected_state_version < 0:
            raise ValueError("invalid expected_state_version")
        _validate_workflow_edge_id(edge_id)
        _validate_workflow_identifier(reason_code, "reason_code")
        if (next_node is None) != (edge_id == "__end__"):
            raise ValueError("__end__ transitions must not create a next node")
        summary_json = self._normalize_workflow_node_summary(output_summary)
        if next_node is not None and not isinstance(next_node, dict):
            raise ValueError("next_node must be an object or None")

        with self._transaction():
            node = self._conn.execute(
                """SELECT n.workflow_run_id, n.node_id, n.node_kind, n.status,
                          n.input_state_version, w.root_task_id, w.state_version,
                          w.status, w.definition_snapshot_json
                   FROM workflow_node_runs n
                   JOIN workflow_runs w ON w.workflow_run_id = n.workflow_run_id
                   WHERE n.node_run_id = ?""",
                (node_run_id,),
            ).fetchone()
            if not node:
                raise KeyError(f"unknown workflow node run: {node_run_id}")
            (
                workflow_run_id, source_node_id, source_node_kind, current_node_status,
                input_state_version, root_task_id, current_state_version,
                workflow_status, definition_json,
            ) = node
            if workflow_status != "RUNNING":
                raise RuntimeError("cannot complete a node for a workflow that is not running")
            if current_node_status != "RUNNING":
                raise RuntimeError("complete_workflow_node requires a running node")
            if current_state_version != expected_state_version:
                raise RuntimeError("workflow state version conflict")
            if input_state_version > expected_state_version:
                raise RuntimeError("node input state version cannot exceed workflow state")

            state_json = _normalize_workflow_state(state, root_task_id=root_task_id)
            workflow_terminal_status = None
            if edge_id == "__end__":
                workflow_terminal_status = {
                    "SUCCEEDED": "SUCCEEDED",
                    "FAILED": "FAILED",
                    "CANCELLED": "CANCELLED",
                    "TIMED_OUT": "TIMED_OUT",
                    "INTERRUPTED": "INTERRUPTED",
                    "SKIPPED": "CANCELLED",
                }[status]
            next_payload = None
            if next_node is not None:
                next_payload = self._validate_next_workflow_node(
                    next_node,
                    workflow_run_id=workflow_run_id,
                    input_state_version=expected_state_version + 1,
                    definition_json=definition_json,
                )
            self._validate_workflow_transition_definition(
                definition_json,
                edge_id=edge_id,
                from_node=(workflow_run_id, source_node_id, source_node_kind),
                to_node=(
                    (workflow_run_id, next_payload["node_id"], next_payload["node_kind"])
                    if next_payload is not None else None
                ),
            )

            update = self._conn.execute(
                """UPDATE workflow_runs
                   SET state_json = ?, state_version = state_version + 1,
                       status = COALESCE(?, status),
                       completion_reason = CASE
                           WHEN ? IS NULL THEN completion_reason
                           ELSE ?
                       END,
                       finished_at = CASE WHEN ? IS NULL THEN finished_at ELSE ? END
                   WHERE workflow_run_id = ? AND state_version = ? AND status = 'RUNNING'""",
                (
                    state_json, workflow_terminal_status, workflow_terminal_status,
                    f"node_{status.lower()}", workflow_terminal_status, time.time(),
                    workflow_run_id, expected_state_version,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("workflow state version conflict")
            output_state_version = expected_state_version + 1
            update = self._conn.execute(
                """UPDATE workflow_node_runs
                   SET status = ?, output_state_version = ?, output_summary_json = ?,
                       error_code = ?, error_message = ?, finished_at = ?
                   WHERE node_run_id = ? AND status = 'RUNNING'""",
                (
                    status, output_state_version, summary_json, error_code, error_message,
                    time.time(), node_run_id,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("workflow node completion lost")
            next_node_run_id = None
            if next_payload is not None:
                next_node_run_id = next_payload["node_run_id"]
                self._conn.execute(
                    """INSERT INTO workflow_node_runs
                       (node_run_id, workflow_run_id, node_id, branch_key, attempt,
                        node_kind, status, input_state_version, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)""",
                    (
                        next_node_run_id, workflow_run_id, next_payload["node_id"],
                        next_payload["branch_key"], next_payload["attempt"],
                        next_payload["node_kind"], output_state_version, time.time(),
                    ),
                )
            cursor = self._conn.execute(
                """INSERT INTO workflow_transitions
                   (workflow_run_id, from_node_run_id, to_node_run_id, edge_id,
                    reason_code, state_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    workflow_run_id, node_run_id, next_node_run_id, edge_id,
                    reason_code, output_state_version, time.time(),
                ),
            )
        return {
            "workflow_run": self.get_workflow_run(workflow_run_id),
            "node_run": self.get_workflow_node_run(node_run_id),
            "next_node_run": (
                self.get_workflow_node_run(next_node_run_id)
                if next_node_run_id is not None else None
            ),
            "transition": self.get_workflow_transition(cursor.lastrowid),
        }

    def finish_workflow_run(
        self,
        *,
        workflow_run_id: str,
        status: str,
        completion_reason: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        if status not in _WORKFLOW_RUN_TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal workflow status: {status}")
        with self._transaction():
            update = self._conn.execute(
                """UPDATE workflow_runs
                   SET status = ?, completion_reason = ?, error_code = ?,
                       error_message = ?, pause_reason = NULL, finished_at = ?
                   WHERE workflow_run_id = ? AND status IN ('RUNNING', 'WAITING_HUMAN')""",
                (
                    status, str(completion_reason or ""), error_code, error_message,
                    time.time(), workflow_run_id,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError(f"illegal workflow terminal transition: {workflow_run_id}")
        return self.get_workflow_run(workflow_run_id)

    def get_workflow_run(self, workflow_run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_runs WHERE workflow_run_id = ?", (workflow_run_id,)
        ).fetchone()
        return self._workflow_run_to_dict(row)

    def list_workflow_runs(
        self, conversation_id: str | None = None, limit: int = 20
    ) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        if conversation_id is None:
            rows = self._conn.execute(
                "SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM workflow_runs WHERE conversation_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (conversation_id, limit),
            ).fetchall()
        return [self._workflow_run_to_dict(row) for row in rows]

    def create_workflow_node_run(
        self,
        *,
        node_run_id: str,
        workflow_run_id: str,
        node_id: str,
        node_kind: str,
        input_state_version: int,
        branch_key: str = "main",
        attempt: int = 1,
        agent_task_id: str | None = None,
        agent_run_id: str | None = None,
    ) -> dict:
        """创建 PENDING NodeRun；关联的 Agent Task/Run 必须彼此一致且只属于该工作流。"""
        _validate_artifact_identifier(node_run_id, "node_run_id")
        _validate_artifact_identifier(workflow_run_id, "workflow_run_id")
        _validate_workflow_identifier(node_id, "node_id")
        _validate_workflow_identifier(branch_key, "branch_key")
        if node_kind not in _WORKFLOW_NODE_KINDS:
            raise ValueError(f"invalid node_kind: {node_kind}")
        if isinstance(input_state_version, bool) or not isinstance(input_state_version, int) or input_state_version < 0:
            raise ValueError("invalid input_state_version")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("invalid node attempt")
        if (agent_task_id is None) != (agent_run_id is None):
            raise ValueError("agent_task_id and agent_run_id must be provided together")
        if node_kind == "AGENT" and agent_task_id is None:
            # G0 可在真正的 AgentRun 创建之前登记占位节点，G1 才会绑定 Agent。
            pass
        if node_kind != "AGENT" and (agent_task_id is not None or agent_run_id is not None):
            raise ValueError("only AGENT nodes may reference Agent Task/Run")
        with self._transaction():
            workflow = self._conn.execute(
                """SELECT status, state_version, definition_snapshot_json
                   FROM workflow_runs WHERE workflow_run_id = ?""",
                (workflow_run_id,),
            ).fetchone()
            if not workflow:
                raise KeyError(f"unknown workflow run: {workflow_run_id}")
            if workflow[0] not in _WORKFLOW_RUN_ACTIVE_STATUSES:
                raise RuntimeError("cannot create a node for a terminal workflow")
            if input_state_version != workflow[1]:
                raise RuntimeError("node input state version does not match workflow")
            self._validate_workflow_node_definition(
                workflow[2], node_id=node_id, node_kind=node_kind
            )
            if agent_task_id is not None:
                self._validate_node_agent_binding(agent_task_id, agent_run_id)
            self._conn.execute(
                """INSERT INTO workflow_node_runs
                   (node_run_id, workflow_run_id, node_id, branch_key, attempt,
                    node_kind, status, input_state_version, agent_task_id,
                    agent_run_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)""",
                (
                    node_run_id, workflow_run_id, node_id, branch_key, attempt,
                    node_kind, input_state_version, agent_task_id, agent_run_id,
                    time.time(),
                ),
            )
        return self.get_workflow_node_run(node_run_id)

    def bind_workflow_node_agent_run(
        self, *, node_run_id: str, agent_task_id: str, agent_run_id: str
    ) -> dict:
        """G1 使用的延迟绑定入口；仅 PENDING AGENT 节点可绑定一次。"""
        _validate_artifact_identifier(agent_task_id, "agent_task_id")
        _validate_artifact_identifier(agent_run_id, "agent_run_id")
        with self._transaction():
            node = self._conn.execute(
                """SELECT node_kind, status FROM workflow_node_runs
                   WHERE node_run_id = ?""",
                (node_run_id,),
            ).fetchone()
            if not node:
                raise KeyError(f"unknown workflow node run: {node_run_id}")
            if node[0] != "AGENT" or node[1] != "PENDING":
                raise RuntimeError("only pending AGENT nodes may bind an agent run")
            self._validate_node_agent_binding(agent_task_id, agent_run_id)
            update = self._conn.execute(
                """UPDATE workflow_node_runs SET agent_task_id = ?, agent_run_id = ?
                   WHERE node_run_id = ? AND agent_run_id IS NULL""",
                (agent_task_id, agent_run_id, node_run_id),
            )
            if update.rowcount != 1:
                raise RuntimeError("workflow node agent run is already bound")
        return self.get_workflow_node_run(node_run_id)

    def start_workflow_node_run(self, node_run_id: str) -> dict:
        with self._transaction():
            row = self._conn.execute(
                """SELECT w.status FROM workflow_node_runs n
                   JOIN workflow_runs w ON w.workflow_run_id = n.workflow_run_id
                   WHERE n.node_run_id = ?""",
                (node_run_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"unknown workflow node run: {node_run_id}")
            if row[0] != "RUNNING":
                raise RuntimeError("cannot start a node for a workflow that is not running")
            update = self._conn.execute(
                """UPDATE workflow_node_runs SET status = 'RUNNING', started_at = ?
                   WHERE node_run_id = ? AND status = 'PENDING'""",
                (time.time(), node_run_id),
            )
            if update.rowcount != 1:
                raise RuntimeError(f"illegal workflow node start transition: {node_run_id}")
        return self.get_workflow_node_run(node_run_id)

    def finish_workflow_node_run(
        self,
        *,
        node_run_id: str,
        status: str,
        output_state_version: int | None = None,
        output_summary: dict | str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict:
        """仅登记等待状态；终态必须走 ``complete_workflow_node`` 原子提交。"""
        if status not in {"WAITING_HUMAN", "WAITING_CHILDREN"}:
            raise ValueError("terminal workflow nodes must use complete_workflow_node")
        if output_state_version is not None and (
            isinstance(output_state_version, bool)
            or not isinstance(output_state_version, int)
            or output_state_version < 0
        ):
            raise ValueError("invalid output_state_version")
        if output_summary is None:
            summary_json = "{}"
        else:
            _, summary = _normalize_workflow_json(
                output_summary, "output_summary_json", limit=16 * 1024
            )
            try:
                from agent.graph import validate_node_output_summary

                summary = validate_node_output_summary(summary)
            except (ImportError, ValueError) as exc:
                raise ValueError(f"invalid output_summary_json: {exc}") from exc
            summary_json = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._transaction():
            node = self._conn.execute(
                """SELECT n.workflow_run_id, n.status, n.input_state_version,
                          w.state_version, w.definition_snapshot_json
                   FROM workflow_node_runs n
                   JOIN workflow_runs w ON w.workflow_run_id = n.workflow_run_id
                   WHERE n.node_run_id = ?""",
                (node_run_id,),
            ).fetchone()
            if not node:
                raise KeyError(f"unknown workflow node run: {node_run_id}")
            workflow_run_id, current_status, input_version, current_state_version, _ = node
            allowed_targets = {
                "RUNNING": {"WAITING_HUMAN", "WAITING_CHILDREN"},
            }.get(current_status, set())
            if status not in allowed_targets:
                raise RuntimeError("illegal workflow node waiting transition")
            if output_state_version is not None and output_state_version < input_version:
                raise ValueError("output_state_version cannot precede input_state_version")
            if output_state_version is not None and output_state_version != current_state_version:
                raise RuntimeError("output_state_version does not match workflow state")
            update = self._conn.execute(
                """UPDATE workflow_node_runs
                   SET status = ?, output_state_version = ?, output_summary_json = ?,
                       error_code = ?, error_message = ?
                   WHERE node_run_id = ? AND status = 'RUNNING'""",
                (
                    status, output_state_version, summary_json, error_code, error_message,
                    node_run_id,
                ),
            )
            if update.rowcount != 1:
                raise RuntimeError("workflow node transition lost")
        return self.get_workflow_node_run(node_run_id)

    def get_workflow_node_run(self, node_run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_node_runs WHERE node_run_id = ?", (node_run_id,)
        ).fetchone()
        return self._workflow_node_run_to_dict(row)

    def list_workflow_node_runs(self, workflow_run_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM workflow_node_runs WHERE workflow_run_id = ?
               ORDER BY created_at, node_run_id""",
            (workflow_run_id,),
        ).fetchall()
        return [self._workflow_node_run_to_dict(row) for row in rows]

    def get_workflow_node_for_agent_run(self, agent_run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_node_runs WHERE agent_run_id = ?", (agent_run_id,)
        ).fetchone()
        return self._workflow_node_run_to_dict(row)

    def create_workflow_transition(
        self,
        *,
        workflow_run_id: str,
        edge_id: str,
        reason_code: str,
        state_version: int,
        from_node_run_id: str | None = None,
        to_node_run_id: str | None = None,
    ) -> dict:
        """记录一条已发生的边，两个端点必须属于同一个 GraphRun。"""
        _validate_artifact_identifier(workflow_run_id, "workflow_run_id")
        _validate_workflow_edge_id(edge_id)
        _validate_workflow_identifier(reason_code, "reason_code")
        if isinstance(state_version, bool) or not isinstance(state_version, int) or state_version < 0:
            raise ValueError("invalid state_version")
        if from_node_run_id is None and edge_id != "__start__":
            raise ValueError("only __start__ transitions may omit from_node_run_id")
        if to_node_run_id is None and edge_id != "__end__":
            raise ValueError("only __end__ transitions may omit to_node_run_id")
        with self._transaction():
            workflow = self._conn.execute(
                """SELECT state_version, definition_snapshot_json
                   FROM workflow_runs WHERE workflow_run_id = ?""",
                (workflow_run_id,),
            ).fetchone()
            if not workflow:
                raise KeyError(f"unknown workflow run: {workflow_run_id}")
            if workflow[0] != state_version:
                raise RuntimeError("transition state_version does not match workflow")
            node_rows: dict[str, tuple] = {}
            for field, node_run_id in (("from", from_node_run_id), ("to", to_node_run_id)):
                if node_run_id is None:
                    continue
                _validate_artifact_identifier(node_run_id, f"{field}_node_run_id")
                row = self._conn.execute(
                    """SELECT workflow_run_id, node_id, node_kind
                       FROM workflow_node_runs WHERE node_run_id = ?""",
                    (node_run_id,),
                ).fetchone()
                if not row:
                    raise KeyError(f"unknown {field} workflow node run: {node_run_id}")
                if row[0] != workflow_run_id:
                    raise ValueError("workflow transition cannot cross workflow runs")
                node_rows[field] = row
            self._validate_workflow_transition_definition(
                workflow[1],
                edge_id=edge_id,
                from_node=node_rows.get("from"),
                to_node=node_rows.get("to"),
            )
            cursor = self._conn.execute(
                """INSERT INTO workflow_transitions
                   (workflow_run_id, from_node_run_id, to_node_run_id, edge_id,
                    reason_code, state_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    workflow_run_id, from_node_run_id, to_node_run_id, edge_id,
                    reason_code, state_version, time.time(),
                ),
            )
        return self.get_workflow_transition(cursor.lastrowid)

    def get_workflow_transition(self, transition_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_transitions WHERE transition_id = ?", (transition_id,)
        ).fetchone()
        return self._row_to_dict("workflow_transitions", row)

    def list_workflow_transitions(self, workflow_run_id: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT * FROM workflow_transitions WHERE workflow_run_id = ?
               ORDER BY transition_id""",
            (workflow_run_id,),
        ).fetchall()
        return [self._row_to_dict("workflow_transitions", row) for row in rows]

    def create_workflow_gate(
        self,
        *,
        gate_id: str,
        workflow_run_id: str,
        node_run_id: str,
        gate_kind: str,
        request_summary: str,
        artifact_refs: list[str] | tuple[str, ...] = (),
    ) -> dict:
        """创建等待中的业务审批 Gate；与工具审批会话白名单完全分离。"""
        _validate_artifact_identifier(gate_id, "gate_id")
        _validate_artifact_identifier(workflow_run_id, "workflow_run_id")
        _validate_artifact_identifier(node_run_id, "node_run_id")
        _validate_workflow_identifier(gate_kind, "gate_kind")
        if not isinstance(request_summary, str) or not request_summary.strip():
            raise ValueError("invalid gate request_summary")
        _, refs = _normalize_workflow_json(list(artifact_refs), "artifact_refs_json", limit=16 * 1024)
        if not isinstance(refs, list) or any(not isinstance(item, str) or not item for item in refs):
            raise ValueError("invalid artifact_refs_json")
        now = time.time()
        with self._transaction():
            node = self._conn.execute(
                """SELECT workflow_run_id, node_kind, status FROM workflow_node_runs
                   WHERE node_run_id = ?""",
                (node_run_id,),
            ).fetchone()
            if not node:
                raise KeyError(f"unknown workflow node run: {node_run_id}")
            if node[0] != workflow_run_id or node[1] != "HUMAN_GATE":
                raise ValueError("workflow gate must belong to a HUMAN_GATE node in this workflow")
            if node[2] != "RUNNING":
                raise RuntimeError("workflow gate node must be running")
            workflow = self._conn.execute(
                "SELECT status FROM workflow_runs WHERE workflow_run_id = ?", (workflow_run_id,)
            ).fetchone()
            if not workflow or workflow[0] != "RUNNING":
                raise RuntimeError("workflow must be running before a gate is created")
            self._conn.execute(
                """UPDATE workflow_node_runs SET status = 'WAITING_HUMAN'
                   WHERE node_run_id = ? AND status = 'RUNNING'""",
                (node_run_id,),
            )
            self._conn.execute(
                """UPDATE workflow_runs SET status = 'WAITING_HUMAN', pause_reason = ?
                   WHERE workflow_run_id = ? AND status IN ('RUNNING', 'WAITING_HUMAN')""",
                (gate_kind, workflow_run_id),
            )
            self._conn.execute(
                """INSERT INTO workflow_gates
                   (gate_id, workflow_run_id, node_run_id, gate_kind, status,
                    request_summary, artifact_refs_json, requested_at)
                   VALUES (?, ?, ?, ?, 'WAITING', ?, ?, ?)""",
                (
                    gate_id, workflow_run_id, node_run_id, gate_kind, request_summary,
                    json.dumps(refs, ensure_ascii=False, separators=(",", ":")), now,
                ),
            )
        return self.get_workflow_gate(gate_id)

    def resolve_workflow_gate(
        self,
        *,
        gate_id: str,
        status: str,
        response_summary: str = "",
    ) -> dict:
        """只允许一次性响应等待 Gate；G2 才会依据结果继续图。"""
        if status not in {"APPROVED", "DENIED", "CANCELLED", "EXPIRED"}:
            raise ValueError(f"invalid resolved gate status: {status}")
        if not isinstance(response_summary, str):
            raise ValueError("invalid gate response_summary")
        with self._transaction():
            gate = self._conn.execute(
                "SELECT workflow_run_id, node_run_id FROM workflow_gates WHERE gate_id = ? AND status = 'WAITING'",
                (gate_id,),
            ).fetchone()
            if not gate:
                raise RuntimeError(f"workflow gate is already resolved or unknown: {gate_id}")
            workflow_run_id, node_run_id = gate
            self._conn.execute(
                """UPDATE workflow_gates SET status = ?, response_summary = ?, responded_at = ?
                   WHERE gate_id = ? AND status = 'WAITING'""",
                (status, response_summary, time.time(), gate_id),
            )
            node_status = "RUNNING" if status == "APPROVED" else "CANCELLED"
            self._conn.execute(
                """UPDATE workflow_node_runs SET status = ?, finished_at = CASE WHEN ? = 'CANCELLED' THEN ? ELSE NULL END
                   WHERE node_run_id = ? AND status = 'WAITING_HUMAN'""",
                (node_status, node_status, time.time(), node_run_id),
            )
            if status == "APPROVED":
                self._conn.execute(
                    """UPDATE workflow_runs SET status = 'RUNNING', pause_reason = NULL
                       WHERE workflow_run_id = ? AND status = 'WAITING_HUMAN'""",
                    (workflow_run_id,),
                )
            else:
                self._conn.execute(
                    """UPDATE workflow_runs SET status = 'CANCELLED', pause_reason = NULL,
                               completion_reason = ?, finished_at = ?
                       WHERE workflow_run_id = ? AND status = 'WAITING_HUMAN'""",
                    (f"gate_{status.lower()}", time.time(), workflow_run_id),
                )
        return self.get_workflow_gate(gate_id)

    def get_workflow_gate(self, gate_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workflow_gates WHERE gate_id = ?", (gate_id,)
        ).fetchone()
        if row is None:
            return None
        result = self._row_to_dict("workflow_gates", row)
        result["artifact_refs"] = json.loads(result.pop("artifact_refs_json"))
        return result

    def list_workflow_gates(self, status: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        if status is not None and status not in _WORKFLOW_GATE_STATUSES:
            raise ValueError(f"invalid workflow gate status: {status}")
        if status is None:
            rows = self._conn.execute(
                "SELECT * FROM workflow_gates ORDER BY requested_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM workflow_gates WHERE status = ?
                   ORDER BY requested_at DESC LIMIT ?""",
                (status, limit),
            ).fetchall()
        results = []
        for row in rows:
            result = self._row_to_dict("workflow_gates", row)
            result["artifact_refs"] = json.loads(result.pop("artifact_refs_json"))
            results.append(result)
        return results

    def reconcile_workflow_runs(self) -> dict[str, int]:
        """启动对账：只关闭遗留运行态，绝不创建新 Agent 或自动恢复。"""
        now = time.time()
        counts = {"interrupted_workflows": 0, "interrupted_nodes": 0, "waiting_gates": 0}
        with self._transaction():
            running_rows = self._conn.execute(
                "SELECT workflow_run_id FROM workflow_runs WHERE status IN ('QUEUED', 'RUNNING')"
            ).fetchall()
            for (workflow_run_id,) in running_rows:
                self._conn.execute(
                    """UPDATE workflow_runs SET status = 'INTERRUPTED',
                               completion_reason = 'process_restarted', finished_at = ?
                       WHERE workflow_run_id = ? AND status IN ('QUEUED', 'RUNNING')""",
                    (now, workflow_run_id),
                )
                counts["interrupted_workflows"] += 1
            node_rows = self._conn.execute(
                """SELECT node_run_id FROM workflow_node_runs
                   WHERE status IN ('PENDING', 'RUNNING', 'WAITING_CHILDREN')"""
            ).fetchall()
            for (node_run_id,) in node_rows:
                self._conn.execute(
                    """UPDATE workflow_node_runs SET status = 'INTERRUPTED',
                               error_code = 'process_restarted', finished_at = ?
                       WHERE node_run_id = ?
                         AND status IN ('PENDING', 'RUNNING', 'WAITING_CHILDREN')""",
                    (now, node_run_id),
                )
                counts["interrupted_nodes"] += 1
            gate_rows = self._conn.execute(
                "SELECT COUNT(*) FROM workflow_gates WHERE status = 'WAITING'"
            ).fetchone()
            counts["waiting_gates"] = int(gate_rows[0])
        return counts

    def _validate_node_agent_binding(self, agent_task_id: str, agent_run_id: str) -> None:
        _validate_artifact_identifier(agent_task_id, "agent_task_id")
        _validate_artifact_identifier(agent_run_id, "agent_run_id")
        run = self._conn.execute(
            "SELECT task_id FROM agent_runs WHERE run_id = ?", (agent_run_id,)
        ).fetchone()
        if not run:
            raise KeyError(f"unknown agent run: {agent_run_id}")
        if run[0] != agent_task_id:
            raise ValueError("agent run does not belong to agent task")
        existing = self._conn.execute(
            "SELECT node_run_id FROM workflow_node_runs WHERE agent_run_id = ?",
            (agent_run_id,),
        ).fetchone()
        if existing:
            raise ValueError("agent run is already linked to a workflow node")

    def _validate_workflow_node_definition(
        self, definition_json: str, *, node_id: str, node_kind: str
    ) -> None:
        try:
            from agent.graph import workflow_definition_from_record

            definition = workflow_definition_from_record(json.loads(definition_json))
        except (ImportError, TypeError, ValueError) as exc:
            raise RuntimeError(f"stored workflow definition is invalid: {exc}") from exc
        node = next((item for item in definition.nodes if item.node_id == node_id), None)
        if node is None:
            raise ValueError("workflow node is not declared in the definition")
        if node.kind.value != node_kind:
            raise ValueError("workflow node kind does not match the definition")

    def _validate_workflow_transition_definition(
        self,
        definition_json: str,
        *,
        edge_id: str,
        from_node: tuple | None,
        to_node: tuple | None,
    ) -> None:
        try:
            from agent.graph import workflow_definition_from_record

            definition = workflow_definition_from_record(json.loads(definition_json))
        except (ImportError, TypeError, ValueError) as exc:
            raise RuntimeError(f"stored workflow definition is invalid: {exc}") from exc
        if edge_id == "__start__":
            if from_node is not None or to_node is None:
                raise ValueError("__start__ must only transition to an initial node")
            if to_node[1] != definition.start_node_id:
                raise ValueError("__start__ must target the definition start node")
            return
        if edge_id == "__end__":
            if from_node is None or to_node is not None:
                raise ValueError("__end__ must only transition from a node to end")
            source = next(
                (node for node in definition.nodes if node.node_id == from_node[1]), None
            )
            if source is None or not source.is_terminal:
                raise ValueError("__end__ must originate from a terminal node")
            return
        if from_node is None or to_node is None:
            raise ValueError("normal workflow transitions require both node endpoints")
        source_node_id = from_node[1]
        target_node_id = to_node[1]
        if not any(
            edge.edge_id == edge_id
            and edge.source_node_id == source_node_id
            and edge.target_node_id == target_node_id
            for edge in definition.edges
        ):
            raise ValueError("workflow transition is not declared in the definition")

    def _normalize_workflow_node_summary(self, output_summary: dict | str | None) -> str:
        if output_summary is None:
            return "{}"
        _, summary = _normalize_workflow_json(
            output_summary, "output_summary_json", limit=16 * 1024
        )
        try:
            from agent.graph import validate_node_output_summary

            summary = validate_node_output_summary(summary)
        except (ImportError, ValueError) as exc:
            raise ValueError(f"invalid output_summary_json: {exc}") from exc
        return json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _validate_next_workflow_node(
        self,
        next_node: dict,
        *,
        workflow_run_id: str,
        input_state_version: int,
        definition_json: str,
    ) -> dict:
        required = {"node_run_id", "node_id", "node_kind"}
        optional = {"branch_key", "attempt"}
        if set(next_node) - (required | optional) or not required <= set(next_node):
            raise ValueError("next_node has unexpected fields")
        node_run_id = _validate_artifact_identifier(next_node["node_run_id"], "next_node.node_run_id")
        node_id = _validate_workflow_identifier(next_node["node_id"], "next_node.node_id")
        node_kind = next_node["node_kind"]
        if node_kind not in _WORKFLOW_NODE_KINDS:
            raise ValueError("invalid next_node.node_kind")
        branch_key = _validate_workflow_identifier(next_node.get("branch_key", "main"), "next_node.branch_key")
        attempt = next_node.get("attempt", 1)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise ValueError("invalid next_node.attempt")
        self._validate_workflow_node_definition(
            definition_json, node_id=node_id, node_kind=node_kind
        )
        if self._conn.execute(
            "SELECT 1 FROM workflow_node_runs WHERE node_run_id = ?", (node_run_id,)
        ).fetchone():
            raise ValueError("next_node.node_run_id already exists")
        if self._conn.execute(
            """SELECT 1 FROM workflow_node_runs
               WHERE workflow_run_id = ? AND node_id = ? AND branch_key = ? AND attempt = ?""",
            (workflow_run_id, node_id, branch_key, attempt),
        ).fetchone():
            raise ValueError("next_node attempt already exists")
        return {
            "node_run_id": node_run_id,
            "node_id": node_id,
            "node_kind": node_kind,
            "branch_key": branch_key,
            "attempt": attempt,
            "input_state_version": input_state_version,
        }

    def _workflow_run_to_dict(self, row) -> dict | None:
        if row is None:
            return None
        result = self._row_to_dict("workflow_runs", row)
        result["definition_snapshot"] = json.loads(result.pop("definition_snapshot_json"))
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def _workflow_node_run_to_dict(self, row) -> dict | None:
        if row is None:
            return None
        result = self._row_to_dict("workflow_node_runs", row)
        result["output_summary"] = json.loads(result.pop("output_summary_json"))
        return result

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

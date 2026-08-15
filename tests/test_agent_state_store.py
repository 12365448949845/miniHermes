"""Agent Runtime SQLite 迁移和状态机测试。"""

import sqlite3

import pytest

from session import SessionDB
from session.db import SCHEMA_VERSION


def _create_old_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
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
            title TEXT
        );
        CREATE TABLE messages (
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
        INSERT INTO sessions (id, model, started_at, message_count)
        VALUES ('old-session', 'old-model', 1.0, 1);
        INSERT INTO messages (session_id, role, content, timestamp)
        VALUES ('old-session', 'user', 'legacy searchable message', 1.0);
        """
    )
    conn.close()


def _create_task_and_run(db, suffix="1"):
    task = db.create_agent_task(
        task_id=f"task-{suffix}",
        conversation_id="session-1",
        session_id="session-1",
        parent_task_id=None,
        kind="main_turn",
        title="test",
        request_preview="request",
    )
    run = db.create_agent_run(
        run_id=f"run-{suffix}",
        task_id=task["task_id"],
        parent_run_id=None,
        conversation_id="session-1",
        start_session_id="session-1",
        agent_kind="main_turn",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=5,
        timeout_seconds=None,
    )
    return task, run


def test_old_database_migrates_without_losing_messages_or_fts(tmp_path):
    path = tmp_path / "old-state.db"
    _create_old_database(path)

    db = SessionDB(path)

    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert db.get_messages("old-session")[0]["content"] == "legacy searchable message"
    assert db.search_messages("searchable")[0]["session_id"] == "old-session"
    message_columns = {
        row[1] for row in db._conn.execute("PRAGMA table_info(messages)")
    }
    assert "agent_run_id" in message_columns
    assert db.resolve_conversation_id("old-session") == "old-session"
    db.close()


def test_run_state_transitions_are_audited_and_terminal_is_immutable(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test-model")
    task, run = _create_task_and_run(db)

    assert task["status"] == "PENDING"
    assert run["status"] == "QUEUED"

    db.start_agent_run(run["run_id"], task["task_id"])
    db.finish_agent_run(
        run_id=run["run_id"],
        task_id=task["task_id"],
        status="SUCCEEDED",
        completion_reason="stop",
        end_session_id="session-1",
        result_preview="done",
        iterations_used=1,
    )

    stored_run = db.get_agent_run(run["run_id"])
    stored_task = db.get_agent_task(task["task_id"])
    assert stored_run["status"] == "SUCCEEDED"
    assert stored_task["status"] == "SUCCEEDED"
    assert [event["event_type"] for event in db.list_agent_events(run["run_id"])] == [
        "run_queued",
        "run_started",
        "run_succeeded",
        "task_succeeded",
    ]

    with pytest.raises(RuntimeError, match="illegal terminal transition"):
        db.finish_agent_run(
            run_id=run["run_id"],
            task_id=task["task_id"],
            status="FAILED",
            completion_reason="late_failure",
            end_session_id="session-1",
        )
    assert db.get_agent_run(run["run_id"])["status"] == "SUCCEEDED"
    db.close()


def test_startup_reconciliation_closes_queued_and_running_runs(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test-model")
    queued_task, queued_run = _create_task_and_run(db, "queued")
    running_task, running_run = _create_task_and_run(db, "running")
    db.start_agent_run(running_run["run_id"], running_task["task_id"])

    counts = db.reconcile_agent_runs()

    assert counts == {"interrupted": 1, "cancelled": 1}
    assert db.get_agent_run(queued_run["run_id"])["status"] == "CANCELLED"
    assert db.get_agent_task(queued_task["task_id"])["status"] == "CANCELLED"
    interrupted = db.get_agent_run(running_run["run_id"])
    assert interrupted["status"] == "INTERRUPTED"
    assert interrupted["completion_reason"] == "process_restarted"
    assert db.get_agent_task(running_task["task_id"])["status"] == "FAILED"
    db.close()


def test_message_can_be_linked_to_agent_run(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test-model")
    task, run = _create_task_and_run(db)
    db.append_message(
        "session-1",
        "user",
        "hello",
        agent_run_id=run["run_id"],
    )

    assert db.get_messages("session-1")[0]["_agent_run_id"] == run["run_id"]
    db.close()


def test_delete_session_removes_entire_chain_runtime_rows_and_fts(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test-model")
    db.create_child_session("session-1", "session-2", "test-model")
    db.append_message("session-1", "user", "root searchable")
    db.append_message("session-2", "assistant", "child searchable")
    task, run = _create_task_and_run(db)

    db.delete_session("session-2")

    assert db.get_messages("session-1") == []
    assert db.get_messages("session-2") == []
    assert db.get_agent_task(task["task_id"]) is None
    assert db.get_agent_run(run["run_id"]) is None
    assert db.search_messages("searchable") == []
    assert db._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert db._conn.execute(
        "SELECT COUNT(*) FROM messages_fts_docsize"
    ).fetchone()[0] == 0
    db.close()

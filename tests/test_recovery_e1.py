"""E1 safe retry eligibility, attempt audit, and cancellation tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

import pytest

from agent.runtime import AgentRunContext
from cli.commands import handle_slash_command
from session import SessionDB
from session.db import SCHEMA_VERSION
from tools import retry as retry_module
from tools.registry import (
    ToolExecutionContext,
    ToolMetadata,
    ToolRegistry,
    ToolStatus,
    resolve_tool_access_policy,
)
from tools.retry import parse_retry_after, trusted_tool_failure


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _call(name: str, call_id: str = "call-one") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def _running_context(db: SessionDB, suffix: str) -> AgentRunContext:
    task_id = f"task-{suffix}"
    run_id = f"run-{suffix}"
    conversation_id = f"conversation-{suffix}"
    db.create_agent_task(
        task_id=task_id,
        conversation_id=conversation_id,
        session_id=None,
        parent_task_id=None,
        kind="main_turn",
        title=suffix,
        request_preview=suffix,
    )
    db.create_agent_run(
        run_id=run_id,
        task_id=task_id,
        parent_run_id=None,
        conversation_id=conversation_id,
        start_session_id=None,
        agent_kind="main_turn",
        model="test",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=10,
        timeout_seconds=None,
    )
    db.start_agent_run(run_id, task_id)
    return AgentRunContext(
        task_id=task_id,
        run_id=run_id,
        conversation_id=conversation_id,
        start_session_id="",
    )


def _execute_with_db(
    registry: ToolRegistry,
    db: SessionDB,
    context: AgentRunContext,
    *,
    policy=None,
    cancel_check=None,
):
    return registry.execute_detailed(
        _call("web_search", f"call-{context.run_id}"),
        ToolExecutionContext(
            policy=policy or resolve_tool_access_policy(None, registry.get_names()),
            db=db,
            run_context=context,
            cancel_check=cancel_check or context.is_cancelled,
        ),
    )


class _DbRuntimeView:
    def __init__(self, db: SessionDB):
        self.db = db

    def get_run(self, run_id):
        return self.db.get_agent_run(run_id)

    def list_runs(self, conversation_id=None, limit=20):
        return self.db.list_agent_runs(conversation_id, limit)

    def get_task(self, task_id):
        return self.db.get_agent_task(task_id)

    def list_events(self, run_id):
        return self.db.list_agent_events(run_id)

    def list_tool_executions(self, run_id):
        return self.db.list_tool_executions(run_id)

    def list_tool_retry_attempts(self, execution_id):
        return self.db.list_tool_retry_attempts(execution_id)

    @staticmethod
    def list_execution_records(run_id):
        return []


def test_successful_retry_preserves_first_failure_and_wait_audit(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "success")
    events = []
    context.event_callback = lambda name, payload: events.append((name, payload))
    registry = ToolRegistry()
    calls = []
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

    @registry.register(_schema("web_search"))
    def web_search():
        calls.append(1)
        if len(calls) == 1:
            return f"Error: Exa search failed: connection reset {secret}"
        return "search result"

    result = _execute_with_db(registry, db, context)

    assert result.status == ToolStatus.SUCCEEDED
    assert result.attempts == 2
    execution = db.list_tool_executions(context.run_id)[0]
    attempts = db.list_tool_retry_attempts(execution["execution_id"])
    assert [item["status"] for item in attempts] == ["FAILED", "SUCCEEDED"]
    assert attempts[0]["error_code"] == "network_transient"
    assert attempts[0]["wait_status"] == "COMPLETED"
    assert attempts[0]["retry_delay_seconds"] == 0
    assert attempts[1]["wait_status"] == "NOT_SCHEDULED"
    assert secret not in attempts[0]["output_preview"]
    assert db.list_failure_recoveries(context.run_id) == []
    event_names = [name for name, _ in events]
    assert "tool_retry_scheduled" in event_names
    assert event_names.count("tool_attempt_finished") == 2

    handled, _, _, _ = handle_slash_command(
        f"/agent {context.run_id}",
        [],
        db,
        "session",
        runtime=_DbRuntimeView(db),
    )
    query_output = capsys.readouterr().out
    assert handled is True
    assert "#1 FAILED/network_transient" in query_output
    assert "#2 SUCCEEDED" in query_output
    db.close()


@pytest.mark.parametrize(
    "first_failure",
    [
        "Error: HTTP 429 for https://example.test",
        "Error: HTTP 503 for https://example.test",
        "Error: request timed out for https://example.test",
        "Error: connection reset by peer",
    ],
)
def test_controlled_network_failures_retry_within_limit(
    first_failure, monkeypatch
):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    registry = ToolRegistry()
    calls = []

    @registry.register(_schema("web_extract"))
    def web_extract():
        calls.append(1)
        return first_failure if len(calls) == 1 else "page content"

    result = registry.execute_detailed(
        _call("web_extract"),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names())
        ),
    )

    assert result.status == ToolStatus.SUCCEEDED
    assert result.attempts == 2
    assert calls == [1, 1]


@pytest.mark.parametrize(
    ("tool_name", "failure", "error_code"),
    [
        (
            "web_search",
            "Error: Exa API key not configured. Set search.api_key in config.yaml.",
            "missing_configuration",
        ),
        (
            "web_search",
            "Error: Exa API key is invalid. Check search.api_key.",
            "authentication_failed",
        ),
        (
            "web_extract",
            "Error: HTTP 401 for https://example.test",
            "authentication_failed",
        ),
        (
            "web_extract",
            "Error: HTTP 403 for https://example.test",
            "permission_denied",
        ),
    ],
)
def test_configuration_and_permission_failures_are_not_retried(
    tool_name, failure, error_code, monkeypatch
):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    registry = ToolRegistry()
    calls = []

    @registry.register(_schema(tool_name))
    def tool():
        calls.append(1)
        return failure

    result = registry.execute_detailed(
        _call(tool_name),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names())
        ),
    )

    assert result.status == ToolStatus.FAILED
    assert result.error_code == error_code
    assert result.attempts == 1
    assert calls == [1]


def test_bash_nonzero_exit_is_failed_once_without_retry():
    registry = ToolRegistry()
    calls = []

    @registry.register(_schema("bash"))
    def bash():
        calls.append(1)
        return "test failed\n[exit code: 2]"

    result = registry.execute_detailed(
        _call("bash"),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names())
        ),
    )

    assert result.status == ToolStatus.FAILED
    assert result.error_code == "nonzero_exit"
    assert result.attempts == 1
    assert calls == [1]


@pytest.mark.parametrize(
    ("metadata", "output"),
    [
        (
            ToolMetadata(
                side_effect="local",
                retry="transient",
                idempotency="idempotent",
            ),
            "Error: Exa search failed: connection reset",
        ),
        (
            ToolMetadata(
                side_effect="none", retry="transient", idempotency="unknown"
            ),
            "Error: Exa search failed: connection reset",
        ),
        (
            ToolMetadata(
                side_effect="none",
                retry="transient",
                idempotency="non_idempotent",
            ),
            "Error: Exa search failed: connection reset",
        ),
        (
            ToolMetadata(
                side_effect="none", retry="never", idempotency="idempotent"
            ),
            "Error: Exa search failed: connection reset",
        ),
        (
            ToolMetadata(
                side_effect="none", retry="transient", idempotency="idempotent"
            ),
            trusted_tool_failure(
                "Error: authentication failed",
                "authentication_failed",
                retryable=True,
            ),
        ),
        (
            ToolMetadata(
                side_effect="none", retry="transient", idempotency="idempotent"
            ),
            trusted_tool_failure(
                "Error: file not found", "file_not_found", retryable=True
            ),
        ),
        (
            ToolMetadata(
                side_effect="none", retry="transient", idempotency="idempotent"
            ),
            trusted_tool_failure(
                "Error: command exited 1", "nonzero_exit", retryable=True
            ),
        ),
    ],
)
def test_side_effect_idempotency_policy_and_error_code_are_hard_gates(
    metadata, output, monkeypatch
):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    registry = ToolRegistry()
    calls = []

    @registry.register(_schema("web_search"), metadata=metadata)
    def web_search():
        calls.append(1)
        return output

    result = registry.execute_detailed(
        _call("web_search"),
        ToolExecutionContext(
            policy=resolve_tool_access_policy(None, registry.get_names())
        ),
    )

    assert result.status == ToolStatus.FAILED
    assert result.attempts == 1
    assert calls == [1]


def test_exhausted_retry_keeps_all_attempts_and_creates_final_recovery(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "exhausted")
    registry = ToolRegistry()

    @registry.register(_schema("web_search"))
    def web_search():
        return "Error: Exa search failed: temporarily unavailable"

    result = _execute_with_db(registry, db, context)

    assert result.status == ToolStatus.FAILED
    assert result.attempts == 3
    execution = db.list_tool_executions(context.run_id)[0]
    attempts = db.list_tool_retry_attempts(execution["execution_id"])
    assert [item["attempt_number"] for item in attempts] == [1, 2, 3]
    assert {item["error_code"] for item in attempts} == {"network_transient"}
    assert [item["wait_status"] for item in attempts] == [
        "COMPLETED", "COMPLETED", "NOT_SCHEDULED",
    ]
    recoveries = db.list_failure_recoveries(context.run_id)
    assert len(recoveries) == 1
    assert recoveries[0]["status"] == "RETRY_EXHAUSTED"
    assert recoveries[0]["attempt_number"] == 3
    assert recoveries[0]["max_attempts"] == 3
    db.close()


def test_retry_after_is_trusted_bounded_and_cancelled_wait_is_audited(
    tmp_path, monkeypatch
):
    observed_delays = []

    def cancel_wait(seconds, cancel_check):
        observed_delays.append(seconds)
        return True

    monkeypatch.setattr(retry_module, "_interruptible_sleep", cancel_wait)
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "cancel-wait")
    registry = ToolRegistry()

    @registry.register(_schema("web_search"))
    def web_search():
        return trusted_tool_failure(
            "Error: Exa API rate limit hit.",
            "rate_limited",
            retryable=True,
            retry_after="7",
        )

    result = _execute_with_db(registry, db, context)

    assert result.status == ToolStatus.CANCELLED
    assert result.attempts == 1
    assert observed_delays == [7.0]
    execution = db.list_tool_executions(context.run_id)[0]
    attempt = db.list_tool_retry_attempts(execution["execution_id"])[0]
    assert attempt["retry_delay_seconds"] == 7.0
    assert attempt["wait_status"] == "CANCELLED"

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = now + timedelta(seconds=12)
    assert parse_retry_after("120") == 60.0
    assert parse_retry_after(later.strftime("%a, %d %b %Y %H:%M:%S GMT"), now=now) == 12
    assert parse_retry_after("retry in five seconds") is None
    db.close()


def test_retry_rechecks_permission_snapshot_before_second_attempt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "permission")
    registry = ToolRegistry()
    calls = []

    @registry.register(_schema("web_search"))
    def web_search():
        calls.append(1)
        return "Error: Exa search failed: connection reset"

    class RevokingPolicy:
        def __init__(self):
            self.checks = 0

        def allows(self, tool_name, args):
            self.checks += 1
            return (self.checks == 1, None if self.checks == 1 else "revoked")

    result = _execute_with_db(
        registry, db, context, policy=RevokingPolicy()
    )

    assert result.status == ToolStatus.DENIED
    assert result.error_code == "tool_not_allowed"
    assert result.attempts == 1
    assert calls == [1]
    execution = db.list_tool_executions(context.run_id)[0]
    attempt = db.list_tool_retry_attempts(execution["execution_id"])[0]
    assert attempt["status"] == "FAILED"
    assert attempt["wait_status"] == "NOT_SCHEDULED"
    db.close()


def test_deadline_is_rechecked_before_retry_is_scheduled(tmp_path, monkeypatch):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "deadline")
    registry = ToolRegistry()
    calls = []

    @registry.register(_schema("web_search"))
    def web_search():
        calls.append(1)
        context.deadline_monotonic = time.monotonic() - 0.01
        return "Error: Exa search failed: connection reset"

    result = _execute_with_db(registry, db, context)

    assert result.status == ToolStatus.CANCELLED
    assert result.error_code == "deadline_exceeded"
    assert result.attempts == 1
    assert calls == [1]
    execution = db.list_tool_executions(context.run_id)[0]
    attempt = db.list_tool_retry_attempts(execution["execution_id"])[0]
    assert attempt["wait_status"] == "NOT_SCHEDULED"
    db.close()


def test_v9_migrates_to_v10_without_rewriting_tool_executions(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    context = _running_context(db, "migration")
    db.create_tool_execution(
        execution_id="tool-migration",
        run_id=context.run_id,
        tool_call_id="call-migration",
        tool_name="web_search",
    )
    db._conn.execute("DROP TABLE tool_retry_attempts")
    db._conn.execute("PRAGMA user_version=9")
    db.close()

    migrated = SessionDB(path)
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated.get_tool_execution("tool-migration") is not None
    attempt = migrated.start_tool_retry_attempt(
        attempt_id="attempt-migration",
        tool_execution_id="tool-migration",
        attempt_number=1,
    )
    assert attempt["status"] == "RUNNING"
    migrated.close()

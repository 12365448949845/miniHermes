"""E0 failure contracts, audit records, and read-only query tests."""

from __future__ import annotations

import json

import pytest

from agent.recovery import (
    FailureClass,
    RecoveryAction,
    RecoveryController,
    RecoveryPolicy,
    classify_tool_failure,
)
from agent.runtime import AgentRunContext, AgentRuntimeManager
from approval import ApprovalEngine, ApprovalMode
from cli.commands import handle_slash_command
from session import SessionDB
from session.db import SCHEMA_VERSION
from tools.registry import (
    ToolExecutionContext,
    ToolMetadata,
    ToolRegistry,
    resolve_tool_access_policy,
)


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _call(name: str, call_id: str, arguments: str = "{}") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _running_context(db: SessionDB, suffix: str = "one") -> AgentRunContext:
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


def _finish_tool(
    db: SessionDB,
    context: AgentRunContext,
    execution_id: str,
    *,
    status: str = "FAILED",
    error_code: str = "network_transient",
):
    db.create_tool_execution(
        execution_id=execution_id,
        run_id=context.run_id,
        tool_call_id=f"call-{execution_id}",
        tool_name="web_search",
    )
    db.finish_tool_execution(
        execution_id=execution_id,
        status=status,
        attempts=1,
        retryable=True,
        error_code=error_code,
        error_message="failed",
        output_preview="failed",
    )


def test_unknown_error_and_free_text_cannot_become_retry_eligible():
    failure = classify_tool_failure(
        tool_name="third_party",
        tool_status="FAILED",
        error_code="please retry this sk-verysecretvalue",
        retryable=True,
        attempts=1,
        side_effects_possible=False,
        side_effect="none",
        idempotency="idempotent",
    )
    decision = RecoveryPolicy().decide(failure)

    assert failure.failure_class == FailureClass.INTERNAL_UNKNOWN
    assert failure.error_code == "unknown_failure"
    assert failure.registered_error is False
    assert failure.retry_eligible is False
    assert decision.action == RecoveryAction.STOP
    assert decision.status == "NOT_APPLICABLE"
    assert "verysecretvalue" not in json.dumps(decision.reason)

    exception_failure = classify_tool_failure(
        tool_name="third_party",
        tool_status="FAILED",
        error_code=RuntimeError("network_transient"),
        retryable=True,
        attempts=1,
        side_effects_possible=False,
        side_effect="none",
        idempotency="idempotent",
    )
    assert exception_failure.error_code == "unknown_failure"
    assert exception_failure.retry_eligible is False

    legacy_positional = ToolMetadata("none", "policy", "transient", "lock")
    resolved = legacy_positional.resolve({})
    assert resolved.concurrency_key == "lock"
    assert resolved.idempotency == "unknown"


def test_failed_tool_creates_one_sanitized_audit_without_extra_execution(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db)
    events = []
    context.event_callback = lambda name, payload: events.append((name, payload))
    registry = ToolRegistry()
    calls = []
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

    @registry.register(
        _schema("legacy_tool"),
        metadata=ToolMetadata(
            side_effect="none", retry="transient", idempotency="idempotent"
        ),
    )
    def legacy_tool():
        calls.append(1)
        return f"Error: arbitrary external text {secret}"

    policy = resolve_tool_access_policy(None, registry.get_names())
    result = registry.execute_detailed(
        _call("legacy_tool", "call-one"),
        ToolExecutionContext(policy=policy, run_context=context, db=db),
    )

    assert len(calls) == 1
    records = db.list_failure_recoveries(context.run_id)
    assert len(records) == 1
    record = records[0]
    assert record["failure_class"] == "INTERNAL_UNKNOWN"
    assert record["error_code"] == "unknown_failure"
    assert record["selected_action"] == "STOP"
    assert record["status"] == "NOT_APPLICABLE"
    assert secret not in json.dumps(record["reason"])
    execution = db.get_tool_execution(record["tool_execution_id"])
    assert secret not in (execution["output_preview"] or "")

    duplicate_events = []
    RecoveryController(
        db, event_callback=lambda name, payload: duplicate_events.append(name)
    ).record_tool_failure(
        execution_id=record["tool_execution_id"],
        result=result,
        metadata=registry.get_metadata("legacy_tool").resolve({}),
    )
    assert len(db.list_failure_recoveries(context.run_id)) == 1
    assert duplicate_events == []
    names = [name for name, _ in events]
    assert names.count("tool_failure_classified") == 1
    assert names.count("recovery_decided") == 1
    db.close()


def test_policy_hardline_and_cancellation_are_closed_without_execution(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "controls")
    registry = ToolRegistry()
    executed = []

    @registry.register(_schema("bash"))
    def bash():
        executed.append(1)
        return "executed"

    deny_all = resolve_tool_access_policy(
        {"include": set()}, registry.get_names()
    )
    registry.execute_detailed(
        _call("bash", "policy-denied"),
        ToolExecutionContext(policy=deny_all, run_context=context, db=db),
    )

    allow_all = resolve_tool_access_policy(None, registry.get_names())
    registry.execute_detailed(
        _call("bash", "hardline", '{"command":"rm /"}'),
        ToolExecutionContext(
            policy=allow_all,
            approval_engine=ApprovalEngine(),
            approval_mode=ApprovalMode.INTERACTIVE,
            run_context=context,
            db=db,
        ),
    )

    context.cancel_reason = "user_interrupt"
    context.cancel_event.set()
    registry.execute_detailed(
        _call("bash", "cancelled"),
        ToolExecutionContext(
            policy=allow_all,
            run_context=context,
            db=db,
            cancel_check=context.is_cancelled,
        ),
    )

    records = db.list_failure_recoveries(context.run_id)
    assert len(records) == 3
    assert {record["status"] for record in records} == {"NOT_APPLICABLE"}
    assert {record["selected_action"] for record in records} == {"STOP"}
    assert {record["failure_class"] for record in records} == {
        "SECURITY", "CONTROL"
    }
    assert executed == []
    for record in records:
        with pytest.raises(RuntimeError, match="illegal recovery transition"):
            db.transition_failure_recovery(
                record["recovery_id"],
                status="RETRYING",
                expected_version=record["version"],
            )
    db.close()


def test_recovery_state_machine_is_optimistic_and_respects_run_terminal(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "transition")
    _finish_tool(db, context, "tool-transition")
    record, created = db.create_initial_failure_recovery(
        recovery_id="recovery-transition",
        run_id=context.run_id,
        node_run_id=None,
        tool_execution_id="tool-transition",
        failure_class="TRANSIENT",
        error_code="network_transient",
        selected_action="RETRY",
        status="PENDING",
        attempt_number=0,
        max_attempts=2,
        reason={"audit_only": True},
    )
    assert created is True
    retrying = db.transition_failure_recovery(
        record["recovery_id"], status="RETRYING", expected_version=1
    )
    assert retrying["version"] == 2
    with pytest.raises(RuntimeError, match="stale recovery record version"):
        db.transition_failure_recovery(
            record["recovery_id"], status="RETRY_EXHAUSTED", expected_version=1
        )
    exhausted = db.transition_failure_recovery(
        record["recovery_id"], status="RETRY_EXHAUSTED", expected_version=2
    )
    assert exhausted["finished_at"] is not None

    second = _running_context(db, "ended")
    _finish_tool(db, second, "tool-ended")
    pending, _ = db.create_initial_failure_recovery(
        recovery_id="recovery-ended",
        run_id=second.run_id,
        node_run_id=None,
        tool_execution_id="tool-ended",
        failure_class="TRANSIENT",
        error_code="network_transient",
        selected_action="RETRY",
        status="PENDING",
        max_attempts=2,
        reason={},
    )
    db.finish_agent_run(
        run_id=second.run_id,
        task_id=second.task_id,
        status="FAILED",
        completion_reason="test",
        end_session_id=None,
    )
    with pytest.raises(RuntimeError, match="source run ended"):
        db.transition_failure_recovery(
            pending["recovery_id"], status="RETRYING", expected_version=1
        )
    counts = db.reconcile_failure_recoveries()
    assert counts["NOT_APPLICABLE"] == 1
    reconciled = db.get_failure_recovery(pending["recovery_id"])
    assert reconciled["status"] == "NOT_APPLICABLE"
    assert reconciled["finished_at"] is not None
    db.close()


def test_recovery_reason_is_bounded_and_redacted_at_database_boundary(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    context = _running_context(db, "redaction")
    _finish_tool(
        db, context, "tool-redaction", error_code="internal_error"
    )
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    with pytest.raises(ValueError, match="classification does not match"):
        db.create_initial_failure_recovery(
            recovery_id="recovery-wrong-class",
            run_id=context.run_id,
            node_run_id=None,
            tool_execution_id="tool-redaction",
            failure_class="TRANSIENT",
            error_code="network_transient",
            selected_action="STOP",
            status="NOT_APPLICABLE",
            reason={},
        )
    record, _ = db.create_initial_failure_recovery(
        recovery_id="recovery-redaction",
        run_id=context.run_id,
        node_run_id=None,
        tool_execution_id="tool-redaction",
        failure_class="INTERNAL_UNKNOWN",
        error_code="internal_error",
        selected_action="STOP",
        status="NOT_APPLICABLE",
        reason={
            "api_key": secret,
            "summary": f"token={secret} " + ("x" * 2000),
            "nested": {"password": "do-not-store"},
        },
    )
    encoded = json.dumps(record["reason"], ensure_ascii=False)
    assert secret not in encoded
    assert "do-not-store" not in encoded
    assert "[REDACTED]" in encoded
    assert len(encoded.encode("utf-8")) < 8 * 1024
    db.close()


def test_v8_migrates_to_v9_and_cli_queries_recovery_records(tmp_path, capsys):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    context = _running_context(db, "migration")
    _finish_tool(db, context, "tool-migration")
    db._conn.execute("DROP TABLE failure_recovery_records")
    db._conn.execute("PRAGMA user_version=8")
    db.close()

    migrated = SessionDB(path)
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated.get_tool_execution("tool-migration") is not None
    record, _ = migrated.create_initial_failure_recovery(
        recovery_id="recovery-migration",
        run_id=context.run_id,
        node_run_id=None,
        tool_execution_id="tool-migration",
        failure_class="TRANSIENT",
        error_code="network_transient",
        selected_action="RETRY",
        status="RETRY_EXHAUSTED",
        attempt_number=1,
        max_attempts=1,
        reason={"audit_only": True},
    )
    migrated.finish_agent_run(
        run_id=context.run_id,
        task_id=context.task_id,
        status="FAILED",
        completion_reason="test",
        end_session_id=None,
    )
    runtime = AgentRuntimeManager(
        migrated,
        runtime_config={
            "max_concurrency": 1,
            "run_timeout_seconds": {},
            "worktree": {"enabled": False},
        },
    )
    try:
        handled, _, _, _ = handle_slash_command(
            f"/recoveries {context.run_id}",
            [],
            migrated,
            "session",
            runtime=runtime,
        )
        assert handled is True
        assert record["recovery_id"] in capsys.readouterr().out

        handle_slash_command(
            "/recovery recovery-mig",
            [],
            migrated,
            "session",
            runtime=runtime,
        )
        output = capsys.readouterr().out
        assert "TRANSIENT/network_transient" in output
        assert "audit_only=True" in output
    finally:
        runtime.shutdown()
        migrated.close()

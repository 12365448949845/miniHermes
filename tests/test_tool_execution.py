"""Phase 3 结构化工具执行、重试和审计测试。"""

import json

from agent.agent import Agent
from agent.runtime import AgentRunContext, AgentRuntimeManager, RunStatus
from approval import ApprovalEngine, ApprovalMode
from provider import Provider, StreamResult
from provider import provider as provider_module
from session import SessionDB
from tools import retry as retry_module
from tools.registry import (
    ToolAccessPolicy,
    ToolExecutionContext,
    ToolMetadata,
    ToolRegistry,
    ToolStatus,
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


def _call(name: str, arguments: str = "{}", call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _allow_all(registry: ToolRegistry) -> ToolAccessPolicy:
    return resolve_tool_access_policy(None, registry.get_names())


def test_bash_timeout_is_never_retried():
    registry = ToolRegistry()
    attempts = []

    @registry.register(_schema("bash"))
    def bash():
        attempts.append(1)
        return "Error: command timed out after 30s"

    result = registry.execute_detailed(
        _call("bash"),
        ToolExecutionContext(policy=_allow_all(registry)),
    )

    assert result.status == ToolStatus.FAILED
    assert result.error_code == "timeout"
    assert result.attempts == 1
    assert len(attempts) == 1


def test_web_transient_failure_retries_and_records_attempt_count(monkeypatch):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    registry = ToolRegistry()
    attempts = []

    @registry.register(_schema("web_search"))
    def web_search():
        attempts.append(1)
        if len(attempts) < 3:
            return "Error: Exa search failed: connection reset"
        return "search result"

    result = registry.execute_detailed(
        _call("web_search"),
        ToolExecutionContext(policy=_allow_all(registry)),
    )

    assert result.status == ToolStatus.SUCCEEDED
    assert result.attempts == 3
    assert len(attempts) == 3
    assert result.model_output.startswith("[Retried: succeeded on attempt 3]")


def test_legacy_error_is_failed_without_transparent_retry(monkeypatch):
    monkeypatch.setattr(retry_module, "RETRY_DELAY_SECONDS", 0)
    registry = ToolRegistry()
    attempts = []

    @registry.register(
        _schema("legacy_tool"),
        metadata=ToolMetadata(side_effect="none", retry="transient"),
    )
    def legacy_tool():
        attempts.append(1)
        return "Error: old string failure"

    result = registry.execute_detailed(
        _call("legacy_tool"),
        ToolExecutionContext(policy=_allow_all(registry)),
    )

    assert result.status == ToolStatus.FAILED
    assert result.error_code == "legacy_reported_error"
    assert result.attempts == 1
    assert len(attempts) == 1


def test_permission_and_approval_denials_do_not_execute_tool():
    registry = ToolRegistry()
    executed = []

    @registry.register(_schema("bash"))
    def bash():
        executed.append(1)
        return "executed"

    denied_policy = resolve_tool_access_policy(
        {"include": set()}, registry.get_names()
    )
    policy_result = registry.execute_detailed(
        _call("bash", '{"command":"rm old.txt"}', "policy-call"),
        ToolExecutionContext(policy=denied_policy),
    )

    allowed_policy = _allow_all(registry)
    engine = ApprovalEngine()
    user_result = registry.execute_detailed(
        _call("bash", '{"command":"rm old.txt"}', "user-call"),
        ToolExecutionContext(
            policy=allowed_policy,
            approval_engine=engine,
            approval_mode=ApprovalMode.INTERACTIVE,
            approval_callback=lambda *unused: "deny",
        ),
    )
    background_result = registry.execute_detailed(
        _call("bash", '{"command":"rm old.txt"}', "background-call"),
        ToolExecutionContext(
            policy=allowed_policy,
            approval_engine=engine,
            approval_mode=ApprovalMode.DENY_SENSITIVE,
        ),
    )

    assert policy_result.error_code == "tool_not_allowed"
    assert "DENIED by user" in user_result.model_output
    assert "DENIED by approval policy" in background_result.model_output
    assert executed == []


class SequenceProvider:
    model = "test-model"

    def __init__(self, results):
        self.results = iter(results)

    def stream(self, **kwargs):
        return next(self.results)

    @staticmethod
    def build_assistant_message(result):
        message = {"role": "assistant", "content": result.content or ""}
        if result.tool_calls:
            message["tool_calls"] = result.tool_calls
        return message

    @staticmethod
    def build_tool_result_message(tool_call_id, result):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": result}


def test_tool_failure_is_audited_but_agent_run_can_recover(tmp_path):
    missing = tmp_path / "missing.txt"
    provider = SequenceProvider([
        StreamResult(
            tool_calls=[_call(
                "read_file",
                json.dumps({"path": str(missing)}),
            )],
            finish_reason="tool_calls",
        ),
        StreamResult(content="I could not read that file.", finish_reason="stop"),
    ])
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", provider.model)

    def factory():
        return Agent(
            provider=provider,
            db=db,
            tool_db=db,
            system_prompt_override="test system",
            max_iterations_override=3,
        )

    runtime = AgentRuntimeManager(db, agent_factory=factory)
    runtime.open_session("session-1")
    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message="read the missing file",
        history=[],
    )

    assert outcome.status == RunStatus.SUCCEEDED
    executions = runtime.list_tool_executions(outcome.run_id)
    assert len(executions) == 1
    assert executions[0]["status"] == "FAILED"
    assert executions[0]["error_code"] == "legacy_reported_error"
    assert executions[0]["attempts"] == 1
    event_types = [event["event_type"] for event in runtime.list_events(outcome.run_id)]
    assert "tool_started" in event_types
    assert "tool_finished" in event_types

    runtime.shutdown()
    db.close()


def test_provider_retries_record_every_http_attempt_and_event(monkeypatch):
    monkeypatch.setattr(provider_module, "jittered_backoff", lambda attempt: 0)
    monkeypatch.setattr(provider_module, "_cprint", lambda *args: None)
    monkeypatch.setattr(
        provider_module,
        "_interruptible_sleep",
        lambda seconds, interrupt_check: False,
    )
    provider = Provider.__new__(Provider)
    attempts = []

    def stream_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("temporary upstream failure")
        return StreamResult(content="done", finish_reason="stop")

    provider._stream_once = stream_once
    events = []
    run_context = AgentRunContext(
        task_id="task-1",
        run_id="run-1",
        conversation_id="conversation-1",
        start_session_id="session-1",
        event_callback=lambda event_type, payload: events.append((event_type, payload)),
    )

    result = provider.stream(
        messages=[],
        tools=[],
        run_context=run_context,
    )

    assert result.content == "done"
    assert run_context.provider_attempts == 3
    retry_events = [payload for name, payload in events if name == "provider_retrying"]
    assert [event["attempt"] for event in retry_events] == [2, 3]


def test_malformed_tool_debug_dump_is_bounded_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_module, "_DEBUG_DIR", tmp_path)
    fake_secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    path = provider_module._dump_malformed_tool_call({
        "tool_name": "write_file",
        "arguments_preview": fake_secret + ("x" * 5000),
    })

    content = path.read_text(encoding="utf-8")
    assert fake_secret not in content
    assert "[REDACTED]" in content
    assert len(content) < 1500


def test_schema_v1_migrates_to_v2_without_losing_session_data(tmp_path):
    path = tmp_path / "state.db"
    db = SessionDB(path)
    db.create_session("legacy-session", "test-model")
    db.append_message("legacy-session", "user", "keep this message")
    db._conn.execute("DROP TABLE tool_executions")
    db._conn.execute("PRAGMA user_version=1")
    db.close()

    migrated = SessionDB(path)

    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert migrated.get_messages("legacy-session")[0]["content"] == "keep this message"
    assert migrated._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_executions'"
    ).fetchone()[0] == "tool_executions"
    migrated.close()

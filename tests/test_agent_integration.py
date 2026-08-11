"""Phase 2：Delegate、Plan 与查询命令的 Runtime 集成测试。"""

from dataclasses import dataclass

from agent.agent import Agent
from agent.runtime import (
    AgentRunContext,
    AgentRuntimeManager,
    AgentSpec,
    RunStatus,
)
from cli import commands as commands_module
from cli import conversation as conversation_module
from cli.state import AppState
from provider import StreamResult
from session import SessionDB


class SequenceProvider:
    model = "test-model"

    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append({
            "messages": kwargs["messages"],
            "tools": kwargs["tools"],
        })
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


def _delegate_call():
    return {
        "id": "delegate-call-1",
        "type": "function",
        "function": {
            "name": "delegate_task",
            "arguments": '{"task":"inspect the repository","context":"Focus on runtime."}',
        },
    }


@dataclass
class RuntimeFixture:
    db: SessionDB
    runtime: AgentRuntimeManager
    created_specs: list


def _open_runtime(tmp_path, provider, max_iterations=5):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", provider.model)
    created_specs = []
    holder = {"runtime": None}

    def main_factory():
        return Agent(
            provider=provider,
            db=db,
            system_prompt_override="main system",
            max_iterations_override=max_iterations,
            runtime=holder["runtime"],
        )

    def ephemeral_factory(spec, request, run_context):
        created_specs.append(spec)
        return Agent(
            provider=provider,
            db=None,
            system_prompt_override=spec.system_prompt,
            max_iterations_override=spec.max_iterations,
            tool_policy=spec.tool_policy,
            agent_kind=spec.kind,
            approval_mode=spec.approval_mode,
            tool_db=db,
            runtime=None,
        )

    runtime = AgentRuntimeManager(
        db,
        agent_factory=main_factory,
        ephemeral_factory=ephemeral_factory,
    )
    holder["runtime"] = runtime
    runtime.open_session("session-1")
    return RuntimeFixture(db, runtime, created_specs)


def _close(fixture):
    fixture.runtime.shutdown()
    fixture.db.close()


def test_delegate_is_registered_as_child_run_and_keeps_history_isolated(tmp_path):
    provider = SequenceProvider([
        StreamResult(tool_calls=[_delegate_call()], finish_reason="tool_calls"),
        StreamResult(content="child answer", finish_reason="stop"),
        StreamResult(content="parent answer", finish_reason="stop"),
    ])
    fixture = _open_runtime(tmp_path, provider)

    outcome = fixture.runtime.run_main_turn(
        conversation_id="session-1",
        user_message="delegate this",
        history=[],
    )

    assert outcome.status == RunStatus.SUCCEEDED
    runs = fixture.runtime.list_runs("session-1", limit=10)
    assert {run["agent_kind"] for run in runs} == {"main_turn", "delegate"}
    main_run = next(run for run in runs if run["agent_kind"] == "main_turn")
    child_run = next(run for run in runs if run["agent_kind"] == "delegate")
    assert main_run["status"] == "SUCCEEDED"
    assert child_run["status"] == "SUCCEEDED"
    assert child_run["parent_run_id"] == main_run["run_id"]

    child_task = fixture.runtime.get_task(child_run["task_id"])
    assert child_task["parent_task_id"] == main_run["task_id"]
    assert child_task["session_id"] == "session-1"

    messages = fixture.db.get_messages("session-1")
    assert [message["role"] for message in messages] == [
        "user", "assistant", "tool", "assistant"
    ]
    assert all(message["_agent_run_id"] == main_run["run_id"] for message in messages)
    assert messages[2]["content"] == "child answer"
    assert not any(message.get("content") == "inspect the repository" for message in messages)

    child_tools = {
        item["function"]["name"] for item in provider.calls[1]["tools"]
    }
    assert "delegate_task" not in child_tools
    assert "clarify" not in child_tools
    assert len(fixture.created_specs) == 1
    assert fixture.created_specs[0].persist_messages is False
    assert "delegate_task" not in fixture.created_specs[0].tool_policy.effective_tools
    assert "clarify" not in fixture.created_specs[0].tool_policy.effective_tools
    assert (
        fixture.created_specs[0].tool_policy.parent_policy_id
        == fixture.runtime.get_session("session-1").agent.tool_policy.policy_id
    )
    _close(fixture)


def test_plan_uses_runtime_and_does_not_write_main_messages(tmp_path, monkeypatch):
    provider = SequenceProvider([
        StreamResult(content="# Plan\n\n1. Inspect files", finish_reason="stop"),
    ])
    fixture = _open_runtime(tmp_path, provider)
    handle = fixture.runtime.get_session("session-1")
    state = AppState(
        runtime=fixture.runtime,
        agent=handle.agent,
        conversation_id="session-1",
        session_id="session-1",
    )
    plan_path = tmp_path / "plan.md"
    monkeypatch.setattr(
        conversation_module,
        "generate_plan_path",
        lambda description: plan_path,
    )
    monkeypatch.setattr(
        conversation_module,
        "make_plan_approval_callback",
        lambda state: lambda plan_text, saved_path: "cancel",
    )
    monkeypatch.setattr(conversation_module, "_cprint", lambda *args: None)

    result = conversation_module._execute_plan_mode(
        handle.agent,
        state,
        fixture.db,
        renderer=None,
        model_name="test-model",
        user_input="make a plan",
        plan_description="test plan",
    )

    assert result == ("make a plan", True)
    assert plan_path.read_text(encoding="utf-8").startswith("# Plan")
    assert fixture.db.get_messages("session-1") == []
    plan_runs = [
        run for run in fixture.runtime.list_runs("session-1", limit=10)
        if run["agent_kind"] == "plan"
    ]
    assert len(plan_runs) == 1
    assert plan_runs[0]["status"] == "SUCCEEDED"
    assert plan_runs[0]["start_session_id"] == "session-1"
    assert fixture.created_specs[0].kind == "plan"
    assert fixture.created_specs[0].persist_messages is False
    assert fixture.created_specs[0].approval_mode.value == "deny_sensitive"
    assert "write_file" not in fixture.created_specs[0].tool_policy.effective_tools
    assert fixture.created_specs[0].tool_policy.argument_allow["memory"]["action"] == frozenset({"view"})
    assert fixture.created_specs[0].tool_policy.parent_policy_id == handle.agent.tool_policy.policy_id
    _close(fixture)


def test_agents_commands_show_runs_and_parent_relationship(tmp_path, monkeypatch, capsys):
    provider = SequenceProvider([
        StreamResult(tool_calls=[_delegate_call()], finish_reason="tool_calls"),
        StreamResult(content="child answer", finish_reason="stop"),
        StreamResult(content="parent answer", finish_reason="stop"),
    ])
    fixture = _open_runtime(tmp_path, provider)
    outcome = fixture.runtime.run_main_turn(
        conversation_id="session-1",
        user_message="delegate this",
        history=[],
    )

    handled, _, _, _ = commands_module.handle_slash_command(
        "/agents",
        [],
        fixture.db,
        "session-1",
        runtime=fixture.runtime,
        conversation_id="session-1",
    )
    listing = capsys.readouterr().out
    assert handled is True
    assert outcome.run_id in listing
    assert "delegate" in listing
    assert "SUCCEEDED" in listing

    handled, _, _, _ = commands_module.handle_slash_command(
        f"/agent {outcome.run_id[:10]}",
        [],
        fixture.db,
        "session-1",
        runtime=fixture.runtime,
        conversation_id="session-1",
    )
    details = capsys.readouterr().out
    assert handled is True
    assert outcome.run_id in details
    assert "events:" in details
    assert "tools:" in details
    assert "delegate_task: SUCCEEDED" in details
    _close(fixture)


def test_ephemeral_parent_cancelled_before_start_never_builds_agent(tmp_path):
    fixture = _open_runtime(tmp_path, SequenceProvider([]))
    parent_context = AgentRunContext(
        task_id="parent-task",
        run_id="parent-run",
        conversation_id="session-1",
        start_session_id="session-1",
    )
    parent_context.cancel_event.set()

    outcome = fixture.runtime.run_ephemeral(
        spec=AgentSpec(
            kind="plan",
            system_prompt="plan system",
            persist_messages=False,
        ),
        request={"task": "cancelled plan", "model": "test-model"},
        conversation_id="session-1",
        session_id="session-1",
        parent_run_context=parent_context,
    )

    assert outcome.status == RunStatus.CANCELLED
    assert outcome.completion_reason == "parent_cancelled"
    assert fixture.created_specs == []
    stored = fixture.runtime.get_run(outcome.run_id)
    assert stored["status"] == "CANCELLED"
    assert stored["completion_reason"] == "parent_cancelled"
    assert stored["end_session_id"] == "session-1"
    assert fixture.runtime.interrupt_current("session-1") is None
    _close(fixture)


def test_ephemeral_factory_failure_reaches_terminal_state_and_redacts_error(tmp_path):
    fixture = _open_runtime(tmp_path, SequenceProvider([]))
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

    def broken_factory(spec, request, run_context):
        raise RuntimeError(f"factory failed with {secret}")

    fixture.runtime._ephemeral_factory = broken_factory
    outcome = fixture.runtime.run_ephemeral(
        spec=AgentSpec(
            kind="plan",
            system_prompt="plan system",
            persist_messages=False,
        ),
        request={"task": "broken plan", "model": "test-model"},
        conversation_id="session-1",
        session_id="session-1",
    )

    assert outcome.status == RunStatus.FAILED
    assert outcome.completion_reason == "internal_error"
    assert secret not in outcome.error_message
    stored_run = fixture.runtime.get_run(outcome.run_id)
    stored_task = fixture.runtime.get_task(outcome.task_id)
    assert stored_run["status"] == "FAILED"
    assert stored_run["end_session_id"] == "session-1"
    assert stored_task["status"] == "FAILED"
    assert fixture.runtime.interrupt_current("session-1") is None
    _close(fixture)

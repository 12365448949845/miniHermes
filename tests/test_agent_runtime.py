"""Phase 1 串行 AgentRuntimeManager 集成测试。"""

from types import SimpleNamespace
import threading
import time

import pytest

from agent import agent as agent_module
from agent.agent import Agent
from agent.runtime import AgentRuntimeManager, RunStatus
from cli import commands as commands_module
from cli import conversation as conversation_module
from cli.state import AppState
from context.compressor import ContextCompressor
from provider import StreamResult
from session import SessionDB


class RuntimeFakeProvider:
    model = "test-model"

    def __init__(self, results, before_stream=None):
        self.results = iter(results)
        self.before_stream = before_stream

    def stream(self, **kwargs):
        if self.before_stream:
            self.before_stream()
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result

    @staticmethod
    def build_assistant_message(result):
        message = {"role": "assistant", "content": result.content or ""}
        if result.tool_calls:
            message["tool_calls"] = result.tool_calls
        return message

    @staticmethod
    def build_tool_result_message(tool_call_id, result):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": result}


def _open_runtime(tmp_path, provider, max_iterations=5):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", provider.model)

    def factory():
        return Agent(
            provider=provider,
            db=db,
            system_prompt_override="test system",
            max_iterations_override=max_iterations,
        )

    runtime = AgentRuntimeManager(db, agent_factory=factory)
    runtime.open_session("session-1")
    return db, runtime


def test_main_run_is_registered_before_provider_call(tmp_path):
    observed = {}
    provider = RuntimeFakeProvider(
        [StreamResult(
            content="done",
            finish_reason="stop",
            prompt_tokens=12,
            completion_tokens=3,
        )]
    )
    db, runtime = _open_runtime(tmp_path, provider)

    def before_stream():
        run = runtime.list_runs("session-1")[0]
        observed["status"] = run["status"]
        observed["run_id"] = run["run_id"]
        observed["message_run_id"] = db.get_messages("session-1")[0]["_agent_run_id"]
        node = db.get_workflow_node_for_agent_run(run["run_id"])
        assert node is not None
        workflow = db.get_workflow_run(node["workflow_run_id"])
        assert workflow is not None
        observed["workflow_status"] = workflow["status"]
        observed["node_status"] = node["status"]
        observed["start_edge"] = db.list_workflow_transitions(
            workflow["workflow_run_id"]
        )[0]["edge_id"]

    provider.before_stream = before_stream
    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message="hello",
        history=[],
    )

    assert outcome.status == RunStatus.SUCCEEDED
    assert observed == {
        "status": "RUNNING",
        "run_id": outcome.run_id,
        "message_run_id": outcome.run_id,
        "workflow_status": "RUNNING",
        "node_status": "RUNNING",
        "start_edge": "__start__",
    }
    stored = runtime.get_run(outcome.run_id)
    assert stored["completion_reason"] == "stop"
    assert stored["iterations_used"] == 1
    assert stored["provider_attempts"] == 1
    assert stored["prompt_tokens"] == 12
    assert stored["completion_tokens"] == 3
    node = db.get_workflow_node_for_agent_run(outcome.run_id)
    workflow = db.get_workflow_run(node["workflow_run_id"])
    assert node["status"] == "SUCCEEDED"
    assert workflow["status"] == "SUCCEEDED"
    assert workflow["state"]["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "reasoning_tokens": 0,
    }
    assert [edge["edge_id"] for edge in db.list_workflow_transitions(workflow["workflow_run_id"])] == [
        "__start__", "__end__",
    ]
    runtime.shutdown()
    db.close()


def test_queued_run_deadline_closes_as_timed_out_before_start(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_agent_task(
        task_id="queued-task",
        conversation_id="conversation",
        session_id=None,
        parent_task_id=None,
        kind="delegate",
        title="queued",
        request_preview="queued",
    )
    db.create_agent_run(
        run_id="queued-run",
        task_id="queued-task",
        parent_run_id=None,
        conversation_id="conversation",
        start_session_id=None,
        agent_kind="delegate",
        model="test",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=5,
        timeout_seconds=1,
    )

    assert db.request_agent_run_cancel(
        "queued-run", "deadline_exceeded"
    ) == "TIMED_OUT"
    assert db.get_agent_run("queued-run")["status"] == "TIMED_OUT"
    assert db.get_agent_task("queued-task")["status"] == "FAILED"
    assert [
        event["event_type"] for event in db.list_agent_events("queued-run")
    ][-2:] == ["run_timed_out", "task_failed"]
    db.close()


def test_provider_failure_returns_closed_partial_history_and_redacts_error(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    provider = RuntimeFakeProvider([RuntimeError(f"upstream failed with {secret}")])
    db, runtime = _open_runtime(tmp_path, provider)

    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message=f"do not persist {secret}",
        history=[],
    )

    assert outcome.status == RunStatus.FAILED
    assert outcome.completion_reason == "provider_error"
    assert outcome.result is not None
    assert [message["role"] for message in outcome.result.messages] == [
        "user",
        "assistant",
    ]
    assert outcome.result.messages[-1]["_msg_type"] == "runtime_status"
    stored_run = runtime.get_run(outcome.run_id)
    stored_task = db.get_agent_task(outcome.task_id)
    assert secret not in stored_run["error_message"]
    assert "[REDACTED]" in stored_run["error_message"]
    assert secret not in stored_task["request_preview"]
    assert {
        message["_agent_run_id"] for message in db.get_messages("session-1")
    } == {outcome.run_id}
    node = db.get_workflow_node_for_agent_run(outcome.run_id)
    workflow = db.get_workflow_run(node["workflow_run_id"])
    assert node["status"] == "FAILED"
    assert workflow["status"] == "FAILED"
    assert workflow["state"]["node_outputs"][node["node_run_id"]]["outcome"] == "failure"
    runtime.shutdown()
    db.close()


def test_budget_exhaustion_is_failed_not_success(tmp_path, monkeypatch):
    from agent import agent as agent_module

    monkeypatch.setattr(agent_module, "print_budget_warning", lambda *args: None)
    provider = RuntimeFakeProvider([
        StreamResult(
            tool_calls=[{
                "id": "call-1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":'},
            }],
            finish_reason="tool_calls",
        )
    ])
    db, runtime = _open_runtime(tmp_path, provider, max_iterations=1)
    monkeypatch.setattr(agent_module, "_cprint", lambda *args: None)

    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message="read",
        history=[],
    )

    assert outcome.status == RunStatus.FAILED
    assert outcome.completion_reason == "budget_exhausted"
    assert runtime.get_run(outcome.run_id)["status"] == "FAILED"
    runtime.shutdown()
    db.close()


def test_interrupted_stream_uses_cancel_requested_then_cancelled(tmp_path):
    provider = RuntimeFakeProvider([
        StreamResult(content="partial", interrupted=True, finish_reason="interrupted")
    ])
    db, runtime = _open_runtime(tmp_path, provider)

    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message="long task",
        history=[],
    )

    assert outcome.status == RunStatus.CANCELLED
    events = [event["event_type"] for event in db.list_agent_events(outcome.run_id)]
    assert "cancel_requested" in events
    assert "run_cancelled" in events
    assert runtime.get_run(outcome.run_id)["status"] == "CANCELLED"
    node = db.get_workflow_node_for_agent_run(outcome.run_id)
    assert node["status"] == "CANCELLED"
    assert db.get_workflow_run(node["workflow_run_id"])["status"] == "CANCELLED"
    runtime.shutdown()
    db.close()


def test_clear_rebuilds_main_agent_and_resets_session_approval(tmp_path):
    provider = RuntimeFakeProvider([])
    db, runtime = _open_runtime(tmp_path, provider)
    old_handle = runtime.get_session("session-1")
    old_agent = old_handle.agent
    old_agent._approval.add_session_approval("session-1", "delete_file")
    state = AppState(
        agent=old_agent,
        runtime=runtime,
        session_id="session-1",
        conversation_id="session-1",
        conversation_history=[{"role": "user", "content": "old"}],
        model_name="test-model",
    )

    _, handled = conversation_module._handle_slash_commands(
        "/clear", old_agent, state, db, "test-model"
    )

    assert handled is True
    assert state.session_id != "session-1"
    assert state.conversation_id == state.session_id
    assert state.agent is not old_agent
    assert state.conversation_history == []
    assert runtime.get_session("session-1") is None
    assert runtime.get_session(state.conversation_id).agent is state.agent
    assert old_agent._approval.check(
        "bash",
        {"command": "rm old.txt"},
        conversation_id="session-1",
    ).action == "confirm"
    runtime.shutdown()
    db.close()


def test_resume_rebuilds_agent_and_uses_compression_chain_root(tmp_path, monkeypatch):
    provider = RuntimeFakeProvider([])
    db, runtime = _open_runtime(tmp_path, provider)
    old_agent = runtime.get_session("session-1").agent
    db.create_session("root-session", "test-model")
    db.create_child_session("root-session", "child-session", "test-model")
    db.append_message("child-session", "user", "resumed message")
    state = AppState(
        agent=old_agent,
        runtime=runtime,
        session_id="session-1",
        conversation_id="session-1",
        model_name="test-model",
    )
    monkeypatch.setattr(commands_module, "print_resumed_history", lambda messages: None)

    _, handled = conversation_module._handle_slash_commands(
        "/resume root-session", old_agent, state, db, "test-model"
    )

    assert handled is True
    assert state.session_id == "child-session"
    assert state.conversation_id == "root-session"
    assert state.agent is not old_agent
    assert state.conversation_history[0]["content"] == "resumed message"
    runtime.shutdown()
    db.close()


def test_context_compression_links_summary_to_run_and_keeps_root(tmp_path):
    class FakeCompletions:
        @staticmethod
        def create(**kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="compressed summary")
                )],
                usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
            )

    provider = SimpleNamespace(
        model="test-model",
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        ),
    )
    usage = []
    run_context = SimpleNamespace(
        record_provider_call=lambda **kwargs: usage.append(kwargs)
    )
    db = SessionDB(tmp_path / "state.db")
    db.create_session("root-session", "test-model")
    compressor = ContextCompressor(provider)
    compressor._threshold_tokens = 20
    history = [
        {"role": "user", "content": "head user"},
        {"role": "assistant", "content": "head assistant"},
        {"role": "user", "content": "old user one" * 5},
        {"role": "assistant", "content": "old answer one" * 5},
        {"role": "user", "content": "old user two" * 5},
        {"role": "assistant", "content": "old answer two" * 5},
        {"role": "user", "content": "latest user"},
        {"role": "assistant", "content": "latest answer"},
    ]

    compressed, child_id = compressor.compress(
        history,
        db,
        "root-session",
        agent_run_id="run-1",
        run_context=run_context,
    )

    assert child_id != "root-session"
    assert db.resolve_conversation_id(child_id) == "root-session"
    assert any("compressed summary" in message.get("content", "") for message in compressed)
    summary = db.get_messages(child_id)[0]
    assert summary["_msg_type"] == "summary"
    assert summary["_agent_run_id"] == "run-1"
    assert usage == [{"prompt_tokens": 7, "completion_tokens": 2}]
    db.close()


def test_interrupt_during_multiple_tool_calls_closes_every_call(monkeypatch):
    monkeypatch.setattr(agent_module, "_cprint", lambda *args: None)
    calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {
                "name": "clarify",
                "arguments": '{"question":"continue?"}',
            },
        }
        for index in (1, 2)
    ]
    provider = RuntimeFakeProvider([
        StreamResult(tool_calls=calls, finish_reason="tool_calls")
    ])
    agent = Agent(
        provider=provider,
        db=None,
        system_prompt_override="test system",
        max_iterations_override=3,
    )

    def execute_then_interrupt(tool_name, tool_call, args):
        agent.interrupt()
        return "first completed"

    monkeypatch.setattr(agent, "_execute_tool", execute_then_interrupt)
    result = agent.run_conversation("run tools", history=[])

    tool_results = {
        message["tool_call_id"]: message["content"]
        for message in result.messages
        if message.get("role") == "tool"
    }
    assert set(tool_results) == {"call-1", "call-2"}
    assert tool_results["call-1"] == "first completed"
    assert tool_results["call-2"].startswith("CANCELLED:")
    assert result.completion_reason == "user_interrupt"


def test_phase1_nudge_gate_records_pending_without_spawning(monkeypatch):
    monkeypatch.setattr(
        conversation_module.cfg,
        "get_evolution_config",
        lambda: {"enabled": True},
    )
    agent = SimpleNamespace(
        turns_since_memory=9,
        iters_since_skill=10,
    )
    state = AppState(
        conversation_history=[{"role": "user", "content": "hello"}]
    )

    conversation_module._try_nudge(agent, state, user_turns=10)

    assert state.pending_nudges == {"memory", "skill"}
    assert agent.turns_since_memory == 0
    assert agent.iters_since_skill == 0


def test_runtime_records_cancel_request_before_worker_finishes(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider(RuntimeFakeProvider):
        def stream(self, **kwargs):
            entered.set()
            assert release.wait(timeout=2)
            return StreamResult(
                content="partial",
                finish_reason="interrupted",
                interrupted=kwargs["interrupt_check"](),
            )

    provider = BlockingProvider([])
    db, runtime = _open_runtime(tmp_path, provider)
    holder = {}

    def run_turn():
        holder["outcome"] = runtime.run_main_turn(
            conversation_id="session-1",
            user_message="wait",
            history=[],
        )

    worker = threading.Thread(target=run_turn)
    worker.start()
    assert entered.wait(timeout=2)

    assert runtime.interrupt_current("session-1") == "CANCEL_REQUESTED"
    active_run = runtime.list_runs("session-1")[0]
    assert active_run["status"] == "CANCEL_REQUESTED"

    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert holder["outcome"].status == RunStatus.CANCELLED
    assert runtime.get_run(active_run["run_id"])["status"] == "CANCELLED"
    runtime.shutdown()
    db.close()


def test_cancel_race_before_start_returns_cancelled_not_internal_error(tmp_path, monkeypatch):
    provider = RuntimeFakeProvider([
        StreamResult(content="should not run", finish_reason="stop")
    ])
    db, runtime = _open_runtime(tmp_path, provider)
    original_start = db.start_agent_run

    def cancel_then_start(run_id, task_id):
        assert runtime.interrupt_current("session-1") == "CANCELLED"
        return original_start(run_id, task_id)

    monkeypatch.setattr(db, "start_agent_run", cancel_then_start)

    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message="cancel immediately",
        history=[],
    )

    assert outcome.status == RunStatus.CANCELLED
    assert runtime.get_run(outcome.run_id)["status"] == "CANCELLED"
    assert db.get_workflow_node_for_agent_run(outcome.run_id) is None
    assert db.list_workflow_runs("session-1") == []
    runtime.shutdown()
    db.close()


def test_main_turn_timeout_closes_agent_and_graph_together(tmp_path):
    class DeadlineAwareProvider(RuntimeFakeProvider):
        def stream(self, **kwargs):
            while not kwargs["interrupt_check"]():
                time.sleep(0.005)
            return StreamResult(interrupted=True, finish_reason="interrupted")

    provider = DeadlineAwareProvider([])
    db, runtime = _open_runtime(tmp_path, provider)
    runtime._runtime_config["run_timeout_seconds"]["main_turn"] = 0.05

    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message="wait for timeout",
        history=[],
    )

    assert outcome.status == RunStatus.TIMED_OUT
    node = db.get_workflow_node_for_agent_run(outcome.run_id)
    workflow = db.get_workflow_run(node["workflow_run_id"])
    assert node["status"] == "TIMED_OUT"
    assert workflow["status"] == "TIMED_OUT"
    assert workflow["completion_reason"] == "deadline_exceeded"
    runtime.shutdown()
    db.close()


def test_context_compression_keeps_graph_on_conversation_while_agent_run_moves_session(tmp_path, monkeypatch):
    provider = RuntimeFakeProvider([
        StreamResult(content="compacted answer", finish_reason="stop"),
    ])
    db, runtime = _open_runtime(tmp_path, provider)
    handle = runtime.get_session("session-1")
    handle.agent._ctx.force_compress = True
    handle.agent._compressor._threshold_tokens = 20
    monkeypatch.setattr(
        handle.agent._compressor,
        "_generate_summary",
        lambda *_args, **_kwargs: "summary checkpoint",
    )
    history = [
        {"role": "user", "content": "head user"},
        {"role": "assistant", "content": "head assistant"},
        {"role": "user", "content": "middle user " * 12},
        {"role": "assistant", "content": "middle assistant " * 12},
        {"role": "user", "content": "tail user " * 12},
        {"role": "assistant", "content": "tail assistant " * 12},
    ]

    outcome = runtime.run_main_turn(
        conversation_id="session-1",
        user_message="finish after compression",
        history=history,
    )

    assert outcome.status == RunStatus.SUCCEEDED
    assert outcome.result.compressed is True
    assert outcome.result.session_id != "session-1"
    assert runtime.get_run(outcome.run_id)["end_session_id"] == outcome.result.session_id
    node = db.get_workflow_node_for_agent_run(outcome.run_id)
    workflow = db.get_workflow_run(node["workflow_run_id"])
    assert workflow["conversation_id"] == "session-1"
    assert workflow["state"]["root"]["session_id"] == "session-1"
    assert "result_preview" not in node["output_summary"]
    runtime.shutdown()
    db.close()


def test_restart_reconciliation_marks_g1_agent_and_graph_interrupted(tmp_path):
    provider = RuntimeFakeProvider([])
    db, runtime = _open_runtime(tmp_path, provider)
    task = runtime.create_task(
        conversation_id="session-1",
        session_id="session-1",
        kind="main_turn",
        request="resume after restart",
    )
    run_id = "g1-restart-run"
    db.create_agent_run(
        run_id=run_id,
        task_id=task["task_id"],
        parent_run_id=None,
        conversation_id="session-1",
        start_session_id="session-1",
        agent_kind="main_turn",
        model=provider.model,
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=1,
        timeout_seconds=None,
    )
    db.start_agent_run(run_id, task["task_id"])
    graph = runtime._graph_runner.start_main_turn(
        task_id=task["task_id"],
        agent_run_id=run_id,
        conversation_id="session-1",
        session_id="session-1",
    )

    runtime.shutdown()
    db.close()

    restarted_db = SessionDB(tmp_path / "state.db")
    restarted = AgentRuntimeManager(restarted_db, agent_factory=lambda: None)
    assert restarted_db.get_agent_run(run_id)["status"] == "INTERRUPTED"
    assert restarted_db.get_agent_task(task["task_id"])["status"] == "FAILED"
    assert restarted_db.get_workflow_run(graph.workflow_run_id)["status"] == "INTERRUPTED"
    assert restarted_db.get_workflow_node_run(graph.node_run_id)["status"] == "INTERRUPTED"
    restarted.shutdown()
    restarted_db.close()


def test_g1_finish_transaction_rolls_back_agent_and_graph_together(tmp_path, monkeypatch):
    provider = RuntimeFakeProvider([])
    db, runtime = _open_runtime(tmp_path, provider)
    task = runtime.create_task(
        conversation_id="session-1",
        session_id="session-1",
        kind="main_turn",
        request="test atomic workflow finish",
    )
    run_id = "g1-atomic-run"
    db.create_agent_run(
        run_id=run_id,
        task_id=task["task_id"],
        parent_run_id=None,
        conversation_id="session-1",
        start_session_id="session-1",
        agent_kind="main_turn",
        model=provider.model,
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=1,
        timeout_seconds=None,
    )
    db.start_agent_run(run_id, task["task_id"])
    graph = runtime._graph_runner.start_main_turn(
        task_id=task["task_id"],
        agent_run_id=run_id,
        conversation_id="session-1",
        session_id="session-1",
    )
    workflow = db.get_workflow_run(graph.workflow_run_id)

    def fail_audit_write(*_args, **_kwargs):
        raise RuntimeError("injected audit write failure")

    monkeypatch.setattr(db, "_append_agent_event", fail_audit_write)
    with pytest.raises(RuntimeError, match="injected audit write failure"):
        db.finish_agent_run_with_workflow_node(
            run_id=run_id,
            task_id=task["task_id"],
            status="SUCCEEDED",
            completion_reason="stop",
            end_session_id="session-1",
            result_preview="done",
            iterations_used=1,
            provider_attempts=1,
            prompt_tokens=3,
            completion_tokens=2,
            reasoning_tokens=0,
            workflow_run_id=graph.workflow_run_id,
            node_run_id=graph.node_run_id,
            expected_state_version=workflow["state_version"],
            state=workflow["state"],
            output_summary={"status": "SUCCEEDED"},
        )

    assert db.get_agent_run(run_id)["status"] == "RUNNING"
    assert db.get_agent_task(task["task_id"])["status"] == "RUNNING"
    assert db.get_workflow_run(graph.workflow_run_id)["status"] == "RUNNING"
    assert db.get_workflow_run(graph.workflow_run_id)["state_version"] == 0
    assert db.get_workflow_node_run(graph.node_run_id)["status"] == "RUNNING"
    assert [
        item["edge_id"]
        for item in db.list_workflow_transitions(graph.workflow_run_id)
    ] == ["__start__"]
    runtime.shutdown()
    db.close()

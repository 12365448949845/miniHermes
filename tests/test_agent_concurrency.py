"""Phase 5：受控并行 Delegate 的离线验收测试。"""

import json
import threading
import time

import pytest

from agent.agent import Agent
from agent.runtime import AgentRuntimeManager, RunStatus
from provider import StreamResult
from session import SessionDB


def _delegate_call(call_id: str, task: str, tools: list[str]) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "delegate_task",
            "arguments": json.dumps({"task": task, "tools": tools}),
        },
    }


class DelegatingProvider:
    model = "test-model"

    def __init__(self, calls: list[dict], *, delay: float = 0.18,
                 fail_task: str | None = None):
        self._calls = calls
        self._delay = delay
        self._fail_task = fail_task
        self._lock = threading.Lock()
        self.active_children = 0
        self.max_active_children = 0
        self.child_started = threading.Event()

    def stream(self, *, messages, interrupt_check=None, **kwargs):
        if any(message.get("role") == "tool" for message in messages):
            return StreamResult(content="parent complete", finish_reason="stop")

        user_message = messages[-1].get("content", "")
        if user_message == "delegate":
            return StreamResult(tool_calls=self._calls, finish_reason="tool_calls")

        with self._lock:
            self.active_children += 1
            self.max_active_children = max(
                self.max_active_children, self.active_children
            )
            self.child_started.set()
        try:
            deadline = time.monotonic() + self._delay
            while time.monotonic() < deadline:
                if interrupt_check and interrupt_check():
                    return StreamResult(interrupted=True, finish_reason="interrupted")
                time.sleep(0.01)
            if self._fail_task and self._fail_task in user_message:
                raise RuntimeError("intentional child failure")
            task = "first" if "first" in user_message else "second"
            return StreamResult(content=f"child:{task}", finish_reason="stop")
        finally:
            with self._lock:
                self.active_children -= 1

    @staticmethod
    def build_assistant_message(result):
        message = {"role": "assistant", "content": result.content or ""}
        if result.tool_calls:
            message["tool_calls"] = result.tool_calls
        return message

    @staticmethod
    def build_tool_result_message(tool_call_id, result):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": result}


def _open_runtime(tmp_path, provider, *, max_concurrency: int,
                  batch_timeout: float = 5.0):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", provider.model)
    holder = {"runtime": None}

    def main_factory():
        return Agent(
            provider=provider,
            db=db,
            system_prompt_override="test main",
            max_iterations_override=5,
            runtime=holder["runtime"],
        )

    def ephemeral_factory(spec, request, run_context):
        return Agent(
            provider=provider,
            db=None,
            system_prompt_override=spec.system_prompt,
            max_iterations_override=spec.max_iterations,
            tool_policy=spec.tool_policy,
            agent_kind=spec.kind,
            approval_mode=spec.approval_mode,
            tool_db=db,
        )

    runtime = AgentRuntimeManager(
        db,
        agent_factory=main_factory,
        ephemeral_factory=ephemeral_factory,
        runtime_config={
            "max_concurrency": max_concurrency,
            "delegate_batch_timeout_seconds": batch_timeout,
            "cancel_grace_seconds": 1,
            "run_timeout_seconds": {},
        },
    )
    holder["runtime"] = runtime
    runtime.open_session("session-1")
    return db, runtime


def _run_two_delegates(tmp_path, *, max_concurrency: int,
                       tools: list[str] | None = None,
                       fail_task: str | None = None):
    tools = tools or ["read_file"]
    provider = DelegatingProvider([
        _delegate_call("call-1", "first", tools),
        _delegate_call("call-2", "second", tools),
    ], fail_task=fail_task)
    db, runtime = _open_runtime(
        tmp_path, provider, max_concurrency=max_concurrency
    )
    started = time.monotonic()
    outcome = runtime.run_main_turn(
        conversation_id="session-1", user_message="delegate", history=[]
    )
    elapsed = time.monotonic() - started
    return db, runtime, provider, outcome, elapsed


def test_safe_delegate_batch_overlaps_and_keeps_parent_tool_result_order(tmp_path):
    db, runtime, provider, outcome, _ = _run_two_delegates(
        tmp_path, max_concurrency=2
    )
    try:
        assert outcome.status == RunStatus.SUCCEEDED
        assert provider.max_active_children == 2
        tool_messages = [
            message for message in db.get_messages("session-1")
            if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call-1", "call-2",
        ]
        assert [message["content"] for message in tool_messages] == [
            "child:first", "child:second",
        ]
    finally:
        runtime.shutdown()
        db.close()


def test_default_concurrency_remains_serial(tmp_path):
    db, runtime, provider, outcome, _ = _run_two_delegates(
        tmp_path, max_concurrency=1
    )
    try:
        assert outcome.status == RunStatus.SUCCEEDED
        assert provider.max_active_children == 1
    finally:
        runtime.shutdown()
        db.close()


@pytest.mark.parametrize("unsafe_tool", ["web_search", "execute_code"])
def test_unsafe_delegate_effective_tools_force_serial_fallback(tmp_path, unsafe_tool):
    db, runtime, provider, outcome, _ = _run_two_delegates(
        tmp_path, max_concurrency=2, tools=[unsafe_tool]
    )
    try:
        assert outcome.status == RunStatus.SUCCEEDED
        assert provider.max_active_children == 1
    finally:
        runtime.shutdown()
        db.close()


@pytest.mark.parametrize("write_tool", ["write_file", "bash"])
def test_write_delegate_without_worktree_is_rejected_before_child_start(
    tmp_path, write_tool
):
    db, runtime, provider, outcome, _ = _run_two_delegates(
        tmp_path, max_concurrency=2, tools=[write_tool]
    )
    try:
        assert outcome.status == RunStatus.SUCCEEDED
        assert provider.max_active_children == 0
        tool_messages = [
            message for message in db.get_messages("session-1")
            if message["role"] == "tool"
        ]
        assert len(tool_messages) == 2
        assert all(
            "require execution_mode=worktree_write" in message["content"]
            for message in tool_messages
        )
    finally:
        runtime.shutdown()
        db.close()


def test_child_failure_does_not_cancel_independent_sibling(tmp_path):
    db, runtime, provider, outcome, _ = _run_two_delegates(
        tmp_path, max_concurrency=2, fail_task="second"
    )
    try:
        assert outcome.status == RunStatus.SUCCEEDED
        assert provider.max_active_children == 2
        child_runs = [
            run for run in runtime.list_runs("session-1", limit=10)
            if run["agent_kind"] == "delegate"
        ]
        assert sorted(run["status"] for run in child_runs) == ["FAILED", "SUCCEEDED"]
        contents = [
            message["content"] for message in db.get_messages("session-1")
            if message["role"] == "tool"
        ]
        assert contents[0] == "child:first"
        assert contents[1].startswith("Error: Delegation failed:")
    finally:
        runtime.shutdown()
        db.close()


def test_interrupt_cancels_a_running_delegate_batch(tmp_path):
    provider = DelegatingProvider([
        _delegate_call("call-1", "first", ["read_file"]),
        _delegate_call("call-2", "second", ["read_file"]),
    ], delay=2.0)
    db, runtime = _open_runtime(tmp_path, provider, max_concurrency=2)
    holder = {}

    def run_turn():
        holder["outcome"] = runtime.run_main_turn(
            conversation_id="session-1", user_message="delegate", history=[]
        )

    worker = threading.Thread(target=run_turn)
    worker.start()
    assert provider.child_started.wait(timeout=2)
    assert runtime.interrupt_current("session-1") == "CANCEL_REQUESTED"
    worker.join(timeout=3)
    try:
        assert not worker.is_alive()
        assert holder["outcome"].status == RunStatus.CANCELLED
        child_runs = [
            run for run in runtime.list_runs("session-1", limit=10)
            if run["agent_kind"] == "delegate"
        ]
        assert child_runs and all(run["status"] == "CANCELLED" for run in child_runs)
    finally:
        runtime.shutdown()
        db.close()


def test_batch_deadline_returns_ordered_timeouts_and_stops_children(tmp_path):
    provider = DelegatingProvider([
        _delegate_call("call-1", "first", ["read_file"]),
        _delegate_call("call-2", "second", ["read_file"]),
    ], delay=2.0)
    db, runtime = _open_runtime(
        tmp_path, provider, max_concurrency=2, batch_timeout=1.0
    )
    try:
        outcome = runtime.run_main_turn(
            conversation_id="session-1", user_message="delegate", history=[]
        )
        assert outcome.status == RunStatus.SUCCEEDED
        tool_messages = [
            message for message in db.get_messages("session-1")
            if message["role"] == "tool"
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call-1", "call-2",
        ]
        assert all(message["content"].startswith("TIMED_OUT:") for message in tool_messages)
        child_runs = [
            run for run in runtime.list_runs("session-1", limit=10)
            if run["agent_kind"] == "delegate"
        ]
        assert child_runs and all(run["status"] == "TIMED_OUT" for run in child_runs)
    finally:
        runtime.shutdown()
        db.close()

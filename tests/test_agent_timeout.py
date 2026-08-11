"""Phase 4：deadline 传播与终态测试。"""

import time

from agent.agent import Agent
from agent.runtime import AgentRuntimeManager, AgentSpec, RunStatus
from provider import StreamResult
from session import SessionDB


class DeadlineAwareProvider:
    model = "test-model"

    def __init__(self):
        self.calls = 0

    def stream(self, **kwargs):
        self.calls += 1
        while not kwargs["interrupt_check"]():
            time.sleep(0.005)
        return StreamResult(interrupted=True, finish_reason="interrupted")

    @staticmethod
    def build_assistant_message(result):
        return {"role": "assistant", "content": result.content or ""}

    @staticmethod
    def build_tool_result_message(tool_call_id, result):
        return {"role": "tool", "tool_call_id": tool_call_id, "content": result}


def test_deadline_finishes_running_ephemeral_run_as_timed_out(tmp_path):
    provider = DeadlineAwareProvider()
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", provider.model)

    def ephemeral_factory(spec, request, context):
        return Agent(
            provider=provider,
            db=None,
            system_prompt_override="test",
            max_iterations_override=spec.max_iterations,
            tool_policy=spec.tool_policy,
            agent_kind=spec.kind,
            approval_mode=spec.approval_mode,
        )

    runtime = AgentRuntimeManager(db, ephemeral_factory=ephemeral_factory)
    outcome = runtime.run_ephemeral(
        spec=AgentSpec(
            kind="plan",
            system_prompt="test",
            max_iterations=3,
            timeout_seconds=0.05,
            persist_messages=False,
        ),
        request={"task": "wait", "model": provider.model},
        conversation_id="session-1",
        session_id="session-1",
    )

    assert provider.calls == 1
    assert outcome.status == RunStatus.TIMED_OUT
    assert outcome.completion_reason == "deadline_exceeded"
    stored = runtime.get_run(outcome.run_id)
    assert stored["status"] == "TIMED_OUT"
    assert stored["completion_reason"] == "deadline_exceeded"
    runtime.shutdown()
    db.close()

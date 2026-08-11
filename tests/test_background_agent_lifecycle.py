"""Phase 4：Runtime 托管后台 Agent 生命周期测试。"""

import time

from agent.runtime import AgentRuntimeManager, AgentSpec
from evolution.nudge import submit_nudge
from session import SessionDB


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_nudge_is_registered_and_completed_by_runtime_worker(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test-model")

    def factory(spec, request, context):
        raise AssertionError("this test uses a prepared no-op job")

    runtime = AgentRuntimeManager(db, ephemeral_factory=factory)
    outcome = runtime.submit_background(
        spec=AgentSpec(kind="memory_nudge", persist_messages=False),
        request={"task": "memory review", "model": "test-model"},
        conversation_id="session-1",
        session_id="session-1",
        prepare=lambda context: None,
    )

    assert outcome.status.value == "QUEUED"
    assert _wait_until(lambda: runtime.get_run(outcome.run_id)["status"] == "SUCCEEDED")
    events = [event["event_type"] for event in runtime.list_events(outcome.run_id)]
    assert "background_queued" in events
    assert "run_started" in events
    assert "run_succeeded" in events
    runtime.shutdown()
    db.close()


def test_submit_nudge_creates_runtime_managed_queued_runs(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("session-1", "test-model")

    def factory(spec, request, context):
        class ImmediateAgent:
            def run_conversation(self, **kwargs):
                from agent.agent import ConversationResult
                return ConversationResult("done", "", [], completion_reason="completed")
        return ImmediateAgent()

    runtime = AgentRuntimeManager(db, ephemeral_factory=factory)
    outcomes = submit_nudge(
        runtime,
        [{"role": "user", "content": "I prefer concise pytest examples."}],
        "memory",
        conversation_id="session-1",
        session_id="session-1",
        model="test-model",
    )

    assert len(outcomes) == 1
    assert _wait_until(
        lambda: runtime.get_run(outcomes[0].run_id)["status"] == "SUCCEEDED"
    )
    assert runtime.get_run(outcomes[0].run_id)["agent_kind"] == "memory_nudge"
    runtime.shutdown()
    db.close()

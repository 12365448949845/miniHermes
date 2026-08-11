"""Phase 0 回归测试：验证运行边界和工具过滤的基础语义。"""

from agent import agent as agent_module
from agent.agent import Agent
from context import ConversationContext
from provider import StreamResult
from tools.registry import ToolRegistry


class FakeProvider:
    """只覆盖 Agent 主循环需要的 Provider 接口。"""

    model = "test-model"

    def __init__(self, results):
        self.results = iter(results)
        self.calls = 0

    def stream(self, **kwargs):
        self.calls += 1
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


def _tool_call(call_id="call-1", arguments="{}"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": arguments},
    }


def test_conversation_context_start_run_resets_only_iteration_budget():
    context = ConversationContext(2, "system", "[]")
    assert context.consume_budget() is True
    assert context.budget_used == 1

    context.start_run()

    assert context.budget_used == 0
    assert context.consume_budget() is True
    assert context.budget_used == 1


def test_agent_rebuilds_budget_for_each_conversation_run():
    provider = FakeProvider([
        StreamResult(content="first", finish_reason="stop"),
        StreamResult(content="second", finish_reason="stop"),
    ])
    agent = Agent(
        provider=provider,
        db=None,
        system_prompt_override="test system prompt",
        max_iterations_override=1,
    )
    first = agent.run_conversation("one", history=[])
    second = agent.run_conversation("two", history=first.messages)

    assert first.final_response == "first"
    assert second.final_response == "second"
    assert provider.calls == 2
    assert agent.budget_used == 1


def test_empty_include_allowlist_returns_no_schemas():
    registry = ToolRegistry()
    registry.register({
        "type": "function",
        "function": {"name": "example_tool", "description": "test"},
    })(lambda: "ok")

    assert registry.get_schemas(include=None)
    assert registry.get_schemas(include=set()) == []
    assert registry.get_schemas(include={"missing_tool"}) == []


def test_malformed_tool_arguments_still_get_a_matching_tool_result(monkeypatch):
    monkeypatch.setattr(agent_module, "_cprint", lambda *args: None)
    call = _tool_call(arguments='{"path":')
    provider = FakeProvider([
        StreamResult(
            content="",
            tool_calls=[call],
            finish_reason="tool_calls",
        ),
        StreamResult(content="recovered", finish_reason="stop"),
    ])
    agent = Agent(
        provider=provider,
        db=None,
        system_prompt_override="test system prompt",
        max_iterations_override=3,
    )

    result = agent.run_conversation("read a file", history=[])

    assistant_calls = {
        tc["id"]
        for message in result.messages
        if message.get("role") == "assistant"
        for tc in message.get("tool_calls", [])
    }
    tool_results = {
        message["tool_call_id"]
        for message in result.messages
        if message.get("role") == "tool"
    }
    assert assistant_calls == {"call-1"}
    assert tool_results == assistant_calls
    assert "malformed" in next(
        message["content"]
        for message in result.messages
        if message.get("role") == "tool"
    )

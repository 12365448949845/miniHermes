"""Phase 3 工具权限快照测试。"""

from tools.registry import (
    ToolAccessPolicy,
    ToolExecutionContext,
    ToolRegistry,
    ToolStatus,
    resolve_tool_access_policy,
)


def _schema(name: str, properties: dict | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {
                "type": "object",
                "properties": properties or {},
            },
        },
    }


def test_plan_policy_narrows_memory_schema_and_blocks_mutation():
    registry = ToolRegistry()
    executed = []

    @registry.register(_schema("memory", {
        "action": {
            "type": "string",
            "enum": ["add", "update", "delete", "view"],
        },
    }))
    def memory(action: str):
        executed.append(action)
        return action

    @registry.register(_schema("write_file"))
    def write_file():
        executed.append("write_file")
        return "written"

    policy = resolve_tool_access_policy(
        {"include": {"memory", "write_file"}},
        registry.get_names(),
        kind="plan",
    )

    schemas = registry.get_schemas(policy=policy)
    memory_schema = next(
        item for item in schemas if item["function"]["name"] == "memory"
    )
    assert memory_schema["function"]["parameters"]["properties"]["action"]["enum"] == ["view"]
    assert "write_file" not in policy.effective_tools

    denied = registry.execute_detailed(
        {
            "id": "call-add",
            "type": "function",
            "function": {
                "name": "memory",
                "arguments": '{"action":"add"}',
            },
        },
        ToolExecutionContext(policy=policy),
    )
    allowed = registry.execute_detailed(
        {
            "id": "call-view",
            "type": "function",
            "function": {
                "name": "memory",
                "arguments": '{"action":"view"}',
            },
        },
        ToolExecutionContext(policy=policy),
    )

    assert denied.status == ToolStatus.DENIED
    assert denied.error_code == "tool_not_allowed"
    assert allowed.status == ToolStatus.SUCCEEDED
    assert executed == ["view"]


def test_delegate_policy_is_parent_intersection_and_empty_tools_means_none():
    registered = {
        "read_file", "write_file", "web_search", "delegate_task", "clarify",
    }
    parent = resolve_tool_access_policy(
        {"include": {"read_file", "web_search", "delegate_task", "clarify"}},
        registered,
        kind="main_turn",
    )

    child = resolve_tool_access_policy(
        {"include": {"read_file", "write_file", "delegate_task"}},
        registered,
        kind="delegate",
        parent_policy=parent,
    )
    empty_child = resolve_tool_access_policy(
        {"include": set()},
        registered,
        kind="delegate",
        parent_policy=parent,
    )

    assert child.effective_tools == frozenset({"read_file"})
    assert child.parent_policy_id == parent.policy_id
    assert empty_child.effective_tools == frozenset()


def test_delegate_schema_only_exposes_tools_parent_can_grant():
    registry = ToolRegistry()

    for name in ("read_file", "write_file", "clarify"):
        registry.register(_schema(name))(lambda: "ok")

    registry.register(_schema("delegate_task", {
        "tools": {
            "type": "array",
            "items": {"type": "string"},
        },
    }))(lambda tools=None: "ok")

    parent = resolve_tool_access_policy(
        {"include": {"read_file", "delegate_task", "clarify"}},
        registry.get_names(),
        kind="main_turn",
    )
    delegate_schema = next(
        item for item in registry.get_schemas(policy=parent)
        if item["function"]["name"] == "delegate_task"
    )

    enum = delegate_schema["function"]["parameters"]["properties"]["tools"]["items"]["enum"]
    assert enum == ["read_file"]


def test_tool_access_policy_is_immutable_after_resolution():
    include = {"read_file"}
    policy = resolve_tool_access_policy(
        {"include": include},
        {"read_file"},
    )
    include.add("write_file")

    assert isinstance(policy, ToolAccessPolicy)
    assert policy.effective_tools == frozenset({"read_file"})

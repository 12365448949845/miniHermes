"""Graph Engineering G0 纯模型契约测试。"""

import math

import pytest

from agent.graph import (
    EdgeDefinition,
    GraphDefinitionError,
    GraphDefinitionRegistry,
    GraphStateError,
    NodeDefinition,
    NodeKind,
    NodeResult,
    WorkflowDefinition,
    apply_node_result,
    create_initial_workflow_state,
    validate_node_output_summary,
    validate_workflow_state,
    workflow_definition_from_record,
)


def _two_node_workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="sample_flow",
        version=1,
        start_node_id="prepare",
        nodes=(
            NodeDefinition("prepare", NodeKind.FUNCTION, "prepare_handler"),
            NodeDefinition("finish", NodeKind.FUNCTION, "finish_handler", is_terminal=True),
        ),
        edges=(EdgeDefinition("prepare_done", "prepare", "finish"),),
    )


def test_workflow_definition_round_trip_is_stable_and_registry_requires_known_handlers():
    definition = _two_node_workflow()

    restored = workflow_definition_from_record(definition.to_record())
    assert restored.snapshot_json() == definition.snapshot_json()

    registry = GraphDefinitionRegistry()
    with pytest.raises(GraphDefinitionError, match="unknown handler"):
        registry.register(definition)

    registry.register_handler("prepare_handler", allowed_kinds=[NodeKind.FUNCTION])
    registry.register_handler("finish_handler", allowed_kinds=[NodeKind.FUNCTION])
    assert registry.register(definition) is definition
    assert registry.get("sample_flow", 1) is definition


@pytest.mark.parametrize(
    ("nodes", "edges", "start_node_id", "match"),
    [
        (
            (
                NodeDefinition("start", "FUNCTION", "handler"),
                NodeDefinition("start", "FUNCTION", "other", is_terminal=True),
            ),
            (),
            "start",
            "unique",
        ),
        (
            (NodeDefinition("start", "FUNCTION", "handler"),),
            (EdgeDefinition("missing", "start", "absent"),),
            "start",
            "unknown node",
        ),
        (
            (NodeDefinition("start", "FUNCTION", "handler"),),
            (),
            "start",
            "outgoing edge",
        ),
        (
            (NodeDefinition("start", "FUNCTION", "handler", is_terminal=True),),
            (EdgeDefinition("loop", "start", "start"),),
            "start",
            "terminal nodes",
        ),
        (
            (
                NodeDefinition("start", "FUNCTION", "handler", max_visits=None),
                NodeDefinition("finish", "FUNCTION", "end", is_terminal=True),
            ),
            (
                EdgeDefinition("loop", "start", "start"),
                EdgeDefinition("finish_path", "start", "finish", rule="OUTCOME_EQUALS", expected_value="done"),
            ),
            "start",
            "finite max_visits",
        ),
        (
            (
                NodeDefinition("start", "FUNCTION", "handler"),
                NodeDefinition("finish", "FUNCTION", "end", is_terminal=True),
                NodeDefinition("orphan", "FUNCTION", "orphan", is_terminal=True),
            ),
            (EdgeDefinition("finish_path", "start", "finish"),),
            "start",
            "unreachable",
        ),
    ],
)
def test_invalid_workflow_definitions_are_rejected(nodes, edges, start_node_id, match):
    with pytest.raises(GraphDefinitionError, match=match):
        WorkflowDefinition(
            workflow_id="invalid_flow",
            version=1,
            nodes=nodes,
            edges=edges,
            start_node_id=start_node_id,
        )


def test_definition_snapshot_rejects_unexpected_fields():
    snapshot = _two_node_workflow().to_record()
    snapshot["nodes"][0]["unexpected"] = "value"

    with pytest.raises(GraphDefinitionError, match="unexpected fields"):
        workflow_definition_from_record(snapshot)


def test_workflow_state_has_fixed_schema_and_rejects_sensitive_or_non_finite_content():
    state = create_initial_workflow_state(task_id="task-1", conversation_id="session-1")
    assert validate_workflow_state(state, task_id="task-1") == state

    invalid = dict(state)
    invalid["extra"] = True
    with pytest.raises(GraphStateError, match="fixed state schema"):
        validate_workflow_state(invalid)

    state["routing"]["openai_api_key"] = "value"
    with pytest.raises(GraphStateError, match="sensitive field"):
        validate_workflow_state(state)

    with pytest.raises(GraphStateError, match="non-finite"):
        validate_node_output_summary({"score": math.nan})

    with pytest.raises(GraphStateError, match="sensitive field"):
        validate_node_output_summary({"messages": ["complete chat history"]})


def test_node_results_use_an_immutable_output_namespace_and_state_size_limit():
    state = create_initial_workflow_state(task_id="task-1")
    result = NodeResult(
        outcome="success",
        output_summary={"summary": "prepared"},
        artifact_refs=("record-1",),
    )
    updated = apply_node_result(state, node_run_id="node-run-1", result=result)

    assert updated["node_outputs"]["node-run-1"]["summary"] == {"summary": "prepared"}
    assert updated["artifacts"]["node-run-1"] == ["record-1"]
    with pytest.raises(GraphStateError, match="cannot be overwritten"):
        apply_node_result(updated, node_run_id="node-run-1", result=result)

    oversized = create_initial_workflow_state(task_id="task-1")
    oversized["routing"]["payload"] = "x" * (64 * 1024)
    with pytest.raises(GraphStateError, match="size limit"):
        validate_workflow_state(oversized)

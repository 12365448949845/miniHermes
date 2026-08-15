"""Graph Engineering G0 SQLite 迁移、状态机与原子性测试。"""

import pytest

from agent.graph import (
    EdgeDefinition,
    NodeDefinition,
    NodeKind,
    WorkflowDefinition,
    create_initial_workflow_state,
)
from session import SessionDB
from session.db import SCHEMA_VERSION


def _definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="sample_flow",
        version=1,
        start_node_id="prepare",
        nodes=(
            NodeDefinition("prepare", "FUNCTION", "prepare_handler"),
            NodeDefinition("gate", "HUMAN_GATE", "gate_handler"),
            NodeDefinition("finish", "FUNCTION", "finish_handler", is_terminal=True),
        ),
        edges=(
            EdgeDefinition("request_approval", "prepare", "gate"),
            EdgeDefinition("approved_finish", "gate", "finish", rule="OUTCOME_EQUALS", expected_value="approved"),
        ),
    )


def _task(db: SessionDB, suffix: str = "one") -> str:
    task_id = f"task-{suffix}"
    db.create_agent_task(
        task_id=task_id,
        conversation_id="conversation-1",
        session_id="session-1",
        parent_task_id=None,
        kind="main_turn",
        title="graph test",
        request_preview="test graph state",
    )
    return task_id


def _run(db: SessionDB, suffix: str = "one", *, start: bool = True) -> tuple[str, str, WorkflowDefinition]:
    task_id = _task(db, suffix)
    definition = _definition()
    workflow_run_id = f"workflow-{suffix}"
    db.create_workflow_run(
        workflow_run_id=workflow_run_id,
        root_task_id=task_id,
        workflow_id=definition.workflow_id,
        workflow_version=definition.version,
        definition_snapshot=definition.to_record(),
        state=create_initial_workflow_state(task_id=task_id, conversation_id="conversation-1"),
        conversation_id="conversation-1",
    )
    if start:
        db.start_workflow_run(workflow_run_id)
    return task_id, workflow_run_id, definition


def _start_node(db: SessionDB, workflow_run_id: str, suffix: str = "one") -> str:
    node_run_id = f"prepare-node-{suffix}"
    db.create_workflow_node_run(
        node_run_id=node_run_id,
        workflow_run_id=workflow_run_id,
        node_id="prepare",
        node_kind="FUNCTION",
        input_state_version=0,
    )
    db.start_workflow_node_run(node_run_id)
    return node_run_id


def test_v3_to_v4_migration_is_idempotent_and_preserves_existing_runtime_and_r0_rows(tmp_path):
    path = tmp_path / "state.db"
    initial = SessionDB(path)
    initial.create_session("session-1", "test-model")
    task_id = _task(initial, "legacy")
    initial.create_agent_run(
        run_id="run-legacy",
        task_id=task_id,
        parent_run_id=None,
        conversation_id="conversation-1",
        start_session_id="session-1",
        agent_kind="main_turn",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=1,
        timeout_seconds=None,
    )
    initial.create_tool_execution(
        execution_id="tool-legacy",
        run_id="run-legacy",
        tool_call_id="call-legacy",
        tool_name="bash",
    )
    initial.create_workspace_snapshot(
        snapshot_id="snapshot-legacy",
        run_id="run-legacy",
        workspace_root="C:/project",
        git_root="C:/project",
        capture_status="PARTIAL",
    )
    initial.create_execution_record(
        record_id="record-legacy",
        run_id="run-legacy",
        tool_execution_id="tool-legacy",
        tool_name="bash",
        snapshot_id="snapshot-legacy",
    )
    initial._conn.execute("DROP TABLE workflow_gates")
    initial._conn.execute("DROP TABLE workflow_transitions")
    initial._conn.execute("DROP TABLE workflow_node_runs")
    initial._conn.execute("DROP TABLE workflow_runs")
    initial._conn.execute("PRAGMA user_version=3")
    initial.close()

    migrated = SessionDB(path)
    assert migrated._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert migrated.get_messages("session-1") == []
    assert migrated.get_agent_task(task_id) is not None
    assert migrated.get_agent_run("run-legacy") is not None
    assert migrated.get_tool_execution("tool-legacy") is not None
    assert migrated.get_workspace_snapshot("snapshot-legacy") is not None
    assert migrated.get_execution_record("record-legacy") is not None
    assert migrated._conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'workflow_runs'"
    ).fetchone()[0] == "workflow_runs"
    migrated.close()

    reopened = SessionDB(path)
    assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    reopened.close()


def test_graph_state_version_and_node_state_machine_are_enforced(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _, workflow_run_id, _ = _run(db)
    node_run_id = _start_node(db, workflow_run_id)

    current = db.get_workflow_run(workflow_run_id)
    changed = dict(current["state"])
    changed["routing"] = {"step": "prepare"}
    db.update_workflow_state(
        workflow_run_id=workflow_run_id,
        expected_state_version=0,
        state=changed,
    )
    with pytest.raises(RuntimeError, match="state version conflict"):
        db.update_workflow_state(
            workflow_run_id=workflow_run_id,
            expected_state_version=0,
            state=changed,
        )
    with pytest.raises(RuntimeError, match="does not match workflow state"):
        db.finish_workflow_node_run(
            node_run_id=node_run_id,
            status="WAITING_CHILDREN",
            output_state_version=0,
            output_summary={"summary": "stale"},
        )

    waiting = db.finish_workflow_node_run(
        node_run_id=node_run_id,
        status="WAITING_CHILDREN",
        output_state_version=1,
        output_summary={"summary": "done"},
    )
    assert waiting["status"] == "WAITING_CHILDREN"
    with pytest.raises(RuntimeError, match="illegal workflow node waiting"):
        db.finish_workflow_node_run(
            node_run_id=node_run_id,
            status="WAITING_CHILDREN",
            output_state_version=1,
        )
    db.close()


def test_nodes_and_transitions_must_match_the_definition_snapshot(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _, workflow_run_id, _ = _run(db)
    with pytest.raises(ValueError, match="not declared"):
        db.create_workflow_node_run(
            node_run_id="unknown-node",
            workflow_run_id=workflow_run_id,
            node_id="unknown",
            node_kind="FUNCTION",
            input_state_version=0,
        )

    source = _start_node(db, workflow_run_id)
    db.create_workflow_node_run(
        node_run_id="gate-node",
        workflow_run_id=workflow_run_id,
        node_id="gate",
        node_kind="HUMAN_GATE",
        input_state_version=0,
    )
    with pytest.raises(ValueError, match="not declared"):
        db.create_workflow_transition(
            workflow_run_id=workflow_run_id,
            edge_id="invented",
            reason_code="test",
            state_version=0,
            from_node_run_id=source,
            to_node_run_id="gate-node",
        )
    db.close()


def test_atomic_node_completion_writes_state_node_transition_and_next_node_together(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    task_id, workflow_run_id, _ = _run(db)
    node_run_id = _start_node(db, workflow_run_id)
    state = db.get_workflow_run(workflow_run_id)["state"]
    state["routing"] = {"next": "gate"}

    completed = db.complete_workflow_node(
        node_run_id=node_run_id,
        status="SUCCEEDED",
        expected_state_version=0,
        state=state,
        output_summary={"summary": "prepared"},
        edge_id="request_approval",
        reason_code="prepared",
        next_node={
            "node_run_id": "gate-node",
            "node_id": "gate",
            "node_kind": "HUMAN_GATE",
        },
    )

    assert completed["workflow_run"]["state_version"] == 1
    assert completed["node_run"]["status"] == "SUCCEEDED"
    assert completed["node_run"]["output_state_version"] == 1
    assert completed["next_node_run"]["status"] == "PENDING"
    assert completed["next_node_run"]["input_state_version"] == 1
    assert completed["transition"]["edge_id"] == "request_approval"
    assert db.get_workflow_run(workflow_run_id)["state"]["root"]["task_id"] == task_id

    with pytest.raises(RuntimeError, match="requires a running node"):
        db.complete_workflow_node(
            node_run_id=node_run_id,
            status="SUCCEEDED",
            expected_state_version=1,
            state=state,
            output_summary={},
            edge_id="__end__",
            reason_code="again",
        )
    db.close()


def test_atomic_completion_rolls_back_everything_when_the_next_node_is_invalid(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _, workflow_run_id, _ = _run(db)
    node_run_id = _start_node(db, workflow_run_id)
    state = db.get_workflow_run(workflow_run_id)["state"]
    state["routing"] = {"next": "not_real"}

    with pytest.raises(ValueError, match="not declared"):
        db.complete_workflow_node(
            node_run_id=node_run_id,
            status="SUCCEEDED",
            expected_state_version=0,
            state=state,
            output_summary={"summary": "must not persist"},
            edge_id="request_approval",
            reason_code="prepared",
            next_node={
                "node_run_id": "not-real-node",
                "node_id": "not_real",
                "node_kind": "FUNCTION",
            },
        )

    assert db.get_workflow_run(workflow_run_id)["state_version"] == 0
    assert db.get_workflow_node_run(node_run_id)["status"] == "RUNNING"
    assert db.list_workflow_transitions(workflow_run_id) == []
    assert db.list_workflow_node_runs(workflow_run_id) == [
        db.get_workflow_node_run(node_run_id)
    ]
    db.close()


def test_an_agent_run_can_only_be_bound_to_one_workflow_node(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    task_id, workflow_run_id, _ = _run(db)
    db.create_agent_run(
        run_id="agent-run-one",
        task_id=task_id,
        parent_run_id=None,
        conversation_id="conversation-1",
        start_session_id="session-1",
        agent_kind="main_turn",
        model="test-model",
        tool_policy_json="{}",
        approval_mode="interactive",
        max_iterations=1,
        timeout_seconds=None,
    )
    db.create_workflow_node_run(
        node_run_id="agent-node-one",
        workflow_run_id=workflow_run_id,
        node_id="prepare",
        node_kind="FUNCTION",
        input_state_version=0,
    )
    # AGENT 归属约束需要来自定义中真实存在的 AGENT 节点，构造第二张图来验证。
    agent_definition = WorkflowDefinition(
        workflow_id="agent_flow",
        version=1,
        start_node_id="agent_step",
        nodes=(NodeDefinition("agent_step", "AGENT", "agent_handler", is_terminal=True),),
        edges=(),
    )
    db.create_workflow_run(
        workflow_run_id="agent-workflow",
        root_task_id=task_id,
        workflow_id=agent_definition.workflow_id,
        workflow_version=agent_definition.version,
        definition_snapshot=agent_definition.to_record(),
        state=create_initial_workflow_state(task_id=task_id),
    )
    db.start_workflow_run("agent-workflow")
    db.create_workflow_node_run(
        node_run_id="agent-node-two",
        workflow_run_id="agent-workflow",
        node_id="agent_step",
        node_kind="AGENT",
        input_state_version=0,
        agent_task_id=task_id,
        agent_run_id="agent-run-one",
    )
    with pytest.raises(ValueError, match="already linked"):
        db.create_workflow_node_run(
            node_run_id="agent-node-three",
            workflow_run_id="agent-workflow",
            node_id="agent_step",
            node_kind="AGENT",
            input_state_version=0,
            branch_key="second",
            agent_task_id=task_id,
            agent_run_id="agent-run-one",
        )
    db.close()


def test_gate_is_bound_to_running_human_node_and_can_only_be_resolved_once(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _, workflow_run_id, _ = _run(db)
    db.create_workflow_node_run(
        node_run_id="gate-node",
        workflow_run_id=workflow_run_id,
        node_id="gate",
        node_kind="HUMAN_GATE",
        input_state_version=0,
    )
    with pytest.raises(RuntimeError, match="must be running"):
        db.create_workflow_gate(
            gate_id="gate-one",
            workflow_run_id=workflow_run_id,
            node_run_id="gate-node",
            gate_kind="plan_approval",
            request_summary="Approve the plan",
        )

    db.start_workflow_node_run("gate-node")
    db.create_workflow_gate(
        gate_id="gate-one",
        workflow_run_id=workflow_run_id,
        node_run_id="gate-node",
        gate_kind="plan_approval",
        request_summary="Approve the plan",
    )
    assert db.get_workflow_run(workflow_run_id)["status"] == "WAITING_HUMAN"
    assert db.get_workflow_node_run("gate-node")["status"] == "WAITING_HUMAN"

    db.resolve_workflow_gate(gate_id="gate-one", status="APPROVED")
    assert db.get_workflow_run(workflow_run_id)["status"] == "RUNNING"
    assert db.get_workflow_node_run("gate-node")["status"] == "RUNNING"
    with pytest.raises(RuntimeError, match="already resolved"):
        db.resolve_workflow_gate(gate_id="gate-one", status="DENIED")
    db.close()


def test_cross_workflow_links_are_rejected_and_restart_reconciliation_only_marks_interrupted(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _, first_workflow, _ = _run(db, "first")
    _, second_workflow, _ = _run(db, "second")
    first_node = _start_node(db, first_workflow, "first")
    second_node = _start_node(db, second_workflow, "second")
    with pytest.raises(ValueError, match="cannot cross"):
        db.create_workflow_transition(
            workflow_run_id=first_workflow,
            edge_id="request_approval",
            reason_code="bad_link",
            state_version=0,
            from_node_run_id=first_node,
            to_node_run_id=second_node,
        )

    db.create_workflow_node_run(
        node_run_id="waiting-gate-node",
        workflow_run_id=first_workflow,
        node_id="gate",
        node_kind="HUMAN_GATE",
        input_state_version=0,
    )
    db.start_workflow_node_run("waiting-gate-node")
    db.create_workflow_gate(
        gate_id="waiting-gate",
        workflow_run_id=first_workflow,
        node_run_id="waiting-gate-node",
        gate_kind="plan_approval",
        request_summary="Approve",
    )

    counts = db.reconcile_workflow_runs()
    assert counts == {
        "interrupted_workflows": 1,
        "interrupted_nodes": 2,
        "waiting_gates": 1,
    }
    assert db.get_workflow_run(second_workflow)["status"] == "INTERRUPTED"
    assert db.get_workflow_node_run(second_node)["status"] == "INTERRUPTED"
    assert db.get_workflow_run(first_workflow)["status"] == "WAITING_HUMAN"
    assert db.get_workflow_node_run("waiting-gate-node")["status"] == "WAITING_HUMAN"
    assert db.get_workflow_gate("waiting-gate")["status"] == "WAITING"
    assert db._conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    db.close()

"""G1 最小 GraphRunner：把一次主 Agent Run 映射到固定工作流。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from agent.graph import (
    GraphDefinitionRegistry,
    NodeDefinition,
    NodeKind,
    NodeResult,
    WorkflowDefinition,
    apply_node_result,
    create_initial_workflow_state,
)
from session import SessionDB


MAIN_TURN_WORKFLOW = WorkflowDefinition(
    workflow_id="main_turn_v1",
    version=1,
    start_node_id="agent_loop",
    nodes=(
        NodeDefinition(
            node_id="agent_loop",
            kind=NodeKind.AGENT,
            handler_id="main_agent_loop",
            tool_policy_ref="main_turn_policy",
            approval_mode="interactive",
            input_contract=("routing", "artifacts"),
            output_contract=("node_outputs", "artifacts", "usage", "errors"),
            is_terminal=True,
        ),
    ),
    edges=(),
)


@dataclass(frozen=True)
class MainTurnGraphContext:
    workflow_run_id: str
    node_run_id: str


class GraphRunner:
    """只调度节点之间的状态，不参与 Agent 内部的 ReAct 循环。"""

    def __init__(self, db: SessionDB, registry: GraphDefinitionRegistry | None = None):
        self.db = db
        self.registry = registry or GraphDefinitionRegistry()
        self.registry.register_handler(
            "main_agent_loop", allowed_kinds=(NodeKind.AGENT,)
        )
        self.registry.register(MAIN_TURN_WORKFLOW)

    def start_main_turn(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        conversation_id: str,
        session_id: str,
    ) -> MainTurnGraphContext:
        """在首个 Provider 调用前原子登记 ``START -> agent_loop``。"""
        workflow_run_id = uuid.uuid4().hex
        node_run_id = uuid.uuid4().hex
        self.db.create_and_start_workflow_agent_node(
            workflow_run_id=workflow_run_id,
            root_task_id=task_id,
            root_agent_run_id=agent_run_id,
            workflow_id=MAIN_TURN_WORKFLOW.workflow_id,
            workflow_version=MAIN_TURN_WORKFLOW.version,
            definition_snapshot=MAIN_TURN_WORKFLOW.to_record(),
            state=create_initial_workflow_state(
                task_id=task_id,
                conversation_id=conversation_id,
                session_id=session_id,
            ),
            conversation_id=conversation_id,
            node_run_id=node_run_id,
            node_id="agent_loop",
            agent_task_id=task_id,
            agent_run_id=agent_run_id,
        )
        return MainTurnGraphContext(workflow_run_id, node_run_id)

    def finish_main_turn(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        workflow_run_id: str,
        node_run_id: str,
        agent_status: str,
        completion_reason: str,
        end_session_id: str | None,
        result_preview: str,
        error_code: str | None,
        error_message: str | None,
        iterations_used: int,
        provider_attempts: int,
        prompt_tokens: int,
        completion_tokens: int,
        reasoning_tokens: int,
    ) -> None:
        """用同一事务收束 AgentRun、GraphRun 和其唯一的 Agent Node。"""
        workflow = self.db.get_workflow_run(workflow_run_id)
        if workflow is None:
            raise RuntimeError(f"workflow run disappeared: {workflow_run_id}")
        result = NodeResult(
            outcome="success" if agent_status == "SUCCEEDED" else "failure",
            output_summary={
                "agent_run_id": agent_run_id,
                "status": agent_status,
                "completion_reason": str(completion_reason or ""),
                "error_code": error_code,
            },
        )
        state = apply_node_result(workflow["state"], node_run_id=node_run_id, result=result)
        state["usage"] = {
            "prompt_tokens": max(0, int(prompt_tokens or 0)),
            "completion_tokens": max(0, int(completion_tokens or 0)),
            "reasoning_tokens": max(0, int(reasoning_tokens or 0)),
        }
        self.db.finish_agent_run_with_workflow_node(
            run_id=agent_run_id,
            task_id=task_id,
            status=agent_status,
            completion_reason=completion_reason,
            end_session_id=end_session_id,
            result_preview=result_preview,
            error_code=error_code,
            error_message=error_message,
            iterations_used=iterations_used,
            provider_attempts=provider_attempts,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            workflow_run_id=workflow_run_id,
            node_run_id=node_run_id,
            expected_state_version=workflow["state_version"],
            state=state,
            output_summary=result.output_summary,
        )

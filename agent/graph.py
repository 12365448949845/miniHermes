"""Graph Engineering 的纯领域模型。

本模块只定义工作流的节点、边、状态和注册表，不执行 Agent、工具或线程。
运行时调度将在后续 GraphRunner 阶段接入。
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


WORKFLOW_STATE_SCHEMA_VERSION = 1
WORKFLOW_STATE_MAX_BYTES = 64 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_RECORD_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SECRET_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_KEYS = frozenset({
    "api_key", "apikey", "authorization", "password", "secret", "token",
    "access_token", "refresh_token", "reasoning", "messages", "history",
    "environment", "env", "headers", "cookies", "credential", "credentials",
})
_SENSITIVE_KEY_FRAGMENTS = frozenset({
    "api_key", "apikey", "authorization", "password", "secret",
    "access_token", "refresh_token", "credential",
})
_STATE_KEYS = frozenset({
    "schema_version", "root", "routing", "node_outputs", "branches", "gates",
    "artifacts", "usage", "errors",
})
_USAGE_KEYS = frozenset({"prompt_tokens", "completion_tokens", "reasoning_tokens"})


class GraphDefinitionError(ValueError):
    """工作流定义不满足静态安全和确定性约束。"""


class GraphStateError(ValueError):
    """工作流状态不是受限、可持久化的结构化数据。"""


class NodeKind(str, Enum):
    AGENT = "AGENT"
    FUNCTION = "FUNCTION"
    HUMAN_GATE = "HUMAN_GATE"
    JOIN = "JOIN"


class EdgeRule(str, Enum):
    ALWAYS = "ALWAYS"
    OUTCOME_EQUALS = "OUTCOME_EQUALS"
    STATE_EQUALS = "STATE_EQUALS"


class ParallelClass(str, Enum):
    SERIAL = "serial"
    READ_ONLY_PARALLEL = "read_only_parallel"
    WORKTREE_WRITE = "worktree_write"


@dataclass(frozen=True)
class RetryPolicy:
    """节点级重试上限。G0 只描述策略，不实际自动重试。"""

    max_attempts: int = 1

    def __post_init__(self):
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise GraphDefinitionError("retry_policy.max_attempts must be an integer")
        if not 1 <= self.max_attempts <= 16:
            raise GraphDefinitionError("retry_policy.max_attempts must be between 1 and 16")

    def to_record(self) -> dict[str, int]:
        return {"max_attempts": self.max_attempts}


@dataclass(frozen=True)
class NodeDefinition:
    """静态工作流中的一个可验证职责单元。"""

    node_id: str
    kind: NodeKind | str
    handler_id: str
    tool_policy_ref: str | None = None
    approval_mode: str | None = None
    input_contract: tuple[str, ...] = ()
    output_contract: tuple[str, ...] = ()
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    parallel_class: ParallelClass | str = ParallelClass.SERIAL
    is_terminal: bool = False
    max_visits: int | None = 1

    def __post_init__(self):
        _require_identifier(self.node_id, "node_id", GraphDefinitionError)
        _require_identifier(self.handler_id, "handler_id", GraphDefinitionError)
        try:
            object.__setattr__(self, "kind", NodeKind(self.kind))
            object.__setattr__(self, "parallel_class", ParallelClass(self.parallel_class))
        except ValueError as exc:
            raise GraphDefinitionError(str(exc)) from exc
        if self.tool_policy_ref is not None:
            _require_identifier(self.tool_policy_ref, "tool_policy_ref", GraphDefinitionError)
        if self.approval_mode is not None and not isinstance(self.approval_mode, str):
            raise GraphDefinitionError("approval_mode must be a string or None")
        if not isinstance(self.is_terminal, bool):
            raise GraphDefinitionError("is_terminal must be a boolean")
        object.__setattr__(self, "input_contract", _normalize_contract(self.input_contract, "input_contract"))
        object.__setattr__(self, "output_contract", _normalize_contract(self.output_contract, "output_contract"))
        if not isinstance(self.retry_policy, RetryPolicy):
            raise GraphDefinitionError("retry_policy must be RetryPolicy")
        if self.max_visits is not None:
            if isinstance(self.max_visits, bool) or not isinstance(self.max_visits, int):
                raise GraphDefinitionError("max_visits must be an integer or None")
            if not 1 <= self.max_visits <= 1024:
                raise GraphDefinitionError("max_visits must be between 1 and 1024")
        if self.kind is not NodeKind.AGENT and self.tool_policy_ref is not None:
            raise GraphDefinitionError("only AGENT nodes may declare tool_policy_ref")
        if self.kind is NodeKind.HUMAN_GATE and self.approval_mode is not None:
            raise GraphDefinitionError("HUMAN_GATE nodes do not use approval_mode")

    def to_record(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "handler_id": self.handler_id,
            "tool_policy_ref": self.tool_policy_ref,
            "approval_mode": self.approval_mode,
            "input_contract": list(self.input_contract),
            "output_contract": list(self.output_contract),
            "retry_policy": self.retry_policy.to_record(),
            "parallel_class": self.parallel_class.value,
            "is_terminal": self.is_terminal,
            "max_visits": self.max_visits,
        }


@dataclass(frozen=True)
class EdgeDefinition:
    """节点之间的一条确定性或受限条件边。"""

    edge_id: str
    source_node_id: str
    target_node_id: str
    rule: EdgeRule | str = EdgeRule.ALWAYS
    expected_value: str | int | bool | None = None
    state_key: str | None = None
    priority: int = 0

    def __post_init__(self):
        _require_identifier(self.edge_id, "edge_id", GraphDefinitionError)
        _require_identifier(self.source_node_id, "source_node_id", GraphDefinitionError)
        _require_identifier(self.target_node_id, "target_node_id", GraphDefinitionError)
        try:
            object.__setattr__(self, "rule", EdgeRule(self.rule))
        except ValueError as exc:
            raise GraphDefinitionError(str(exc)) from exc
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or self.priority < 0:
            raise GraphDefinitionError("edge priority must be a non-negative integer")
        if self.rule is EdgeRule.ALWAYS:
            if self.expected_value is not None or self.state_key is not None:
                raise GraphDefinitionError("ALWAYS edges cannot declare a condition")
        elif self.rule is EdgeRule.OUTCOME_EQUALS:
            if not isinstance(self.expected_value, str) or not self.expected_value:
                raise GraphDefinitionError("OUTCOME_EQUALS edges require a string expected_value")
            if self.state_key is not None:
                raise GraphDefinitionError("OUTCOME_EQUALS edges cannot declare state_key")
        else:
            if not isinstance(self.state_key, str) or not self.state_key:
                raise GraphDefinitionError("STATE_EQUALS edges require state_key")
            _validate_state_path(self.state_key)
            if isinstance(self.expected_value, float) or not isinstance(self.expected_value, (str, int, bool)):
                raise GraphDefinitionError("STATE_EQUALS edges require a scalar expected_value")

    def to_record(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "rule": self.rule.value,
            "expected_value": self.expected_value,
            "state_key": self.state_key,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class WorkflowDefinition:
    """代码拥有的、不可变且可序列化的工作流定义。"""

    workflow_id: str
    version: int
    nodes: tuple[NodeDefinition, ...]
    edges: tuple[EdgeDefinition, ...]
    start_node_id: str

    def __post_init__(self):
        _require_identifier(self.workflow_id, "workflow_id", GraphDefinitionError)
        _require_identifier(self.start_node_id, "start_node_id", GraphDefinitionError)
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise GraphDefinitionError("workflow version must be a positive integer")
        object.__setattr__(self, "nodes", tuple(self.nodes))
        object.__setattr__(self, "edges", tuple(self.edges))
        _validate_definition(self)

    def to_record(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "version": self.version,
            "start_node_id": self.start_node_id,
            "nodes": [node.to_record() for node in self.nodes],
            "edges": [edge.to_record() for edge in self.edges],
        }

    def snapshot_json(self) -> str:
        return json.dumps(self.to_record(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class NodeResult:
    """节点能交给 GraphRunner 的唯一受限输出格式。"""

    outcome: str
    output_summary: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    route_key: str | None = None

    def __post_init__(self):
        if self.outcome not in {"success", "failure", "approved", "denied", "waiting"}:
            raise GraphStateError(f"unsupported node outcome: {self.outcome!r}")
        if not isinstance(self.output_summary, Mapping):
            raise GraphStateError("output_summary must be an object")
        summary = validate_node_output_summary(self.output_summary)
        if not isinstance(self.artifact_refs, (tuple, list)):
            raise GraphStateError("artifact_refs must be a sequence")
        refs = tuple(self.artifact_refs)
        if len(refs) > 64 or any(not isinstance(item, str) or not item or len(item) > 512 for item in refs):
            raise GraphStateError("artifact_refs contain an invalid value")
        if self.route_key is not None:
            _require_identifier(self.route_key, "route_key", GraphStateError)
        object.__setattr__(self, "output_summary", summary)
        object.__setattr__(self, "artifact_refs", refs)


class GraphDefinitionRegistry:
    """受版本控制代码注册的静态工作流和处理器标识集合，不执行处理器。"""

    def __init__(self):
        self._definitions: dict[tuple[str, int], WorkflowDefinition] = {}
        self._handlers: dict[str, frozenset[NodeKind] | None] = {}

    def register_handler(
        self,
        handler_id: str,
        *,
        allowed_kinds: Iterable[NodeKind | str] | None = None,
    ) -> None:
        """登记由运行时实现的处理器标识，不保存任意可调用对象。"""
        _require_identifier(handler_id, "handler_id", GraphDefinitionError)
        if handler_id in self._handlers:
            raise GraphDefinitionError(f"handler already registered: {handler_id}")
        if allowed_kinds is None:
            self._handlers[handler_id] = None
            return
        if isinstance(allowed_kinds, (str, bytes)):
            raise GraphDefinitionError("allowed_kinds must be a sequence")
        try:
            kinds = frozenset(NodeKind(kind) for kind in allowed_kinds)
        except (TypeError, ValueError) as exc:
            raise GraphDefinitionError("allowed_kinds contains an invalid node kind") from exc
        if not kinds:
            raise GraphDefinitionError("allowed_kinds cannot be empty")
        self._handlers[handler_id] = kinds

    def register(self, definition: WorkflowDefinition) -> WorkflowDefinition:
        if not isinstance(definition, WorkflowDefinition):
            raise GraphDefinitionError("registry accepts WorkflowDefinition only")
        for node in definition.nodes:
            allowed_kinds = self._handlers.get(node.handler_id)
            if node.handler_id not in self._handlers:
                raise GraphDefinitionError(f"unknown handler: {node.handler_id}")
            if allowed_kinds is not None and node.kind not in allowed_kinds:
                raise GraphDefinitionError(
                    f"handler {node.handler_id} does not support {node.kind.value}"
                )
        key = (definition.workflow_id, definition.version)
        if key in self._definitions:
            raise GraphDefinitionError(f"workflow already registered: {definition.workflow_id}@{definition.version}")
        self._definitions[key] = definition
        return definition

    def get(self, workflow_id: str, version: int) -> WorkflowDefinition:
        try:
            return self._definitions[(workflow_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown workflow: {workflow_id}@{version}") from exc

    def list(self) -> tuple[WorkflowDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))


def workflow_definition_from_record(value: Mapping[str, Any]) -> WorkflowDefinition:
    """从持久化快照还原并重新验证工作流定义。"""
    if not isinstance(value, Mapping):
        raise GraphDefinitionError("workflow definition snapshot must be an object")
    required = {"workflow_id", "version", "start_node_id", "nodes", "edges"}
    if set(value) != required:
        raise GraphDefinitionError("workflow definition snapshot has unexpected fields")
    try:
        if not isinstance(value["nodes"], list) or not isinstance(value["edges"], list):
            raise GraphDefinitionError("workflow definition nodes and edges must be lists")
        node_fields = {
            "node_id", "kind", "handler_id", "tool_policy_ref", "approval_mode",
            "input_contract", "output_contract", "retry_policy", "parallel_class",
            "is_terminal", "max_visits",
        }
        edge_fields = {
            "edge_id", "source_node_id", "target_node_id", "rule", "expected_value",
            "state_key", "priority",
        }
        for item in value["nodes"]:
            if not isinstance(item, Mapping) or set(item) != node_fields:
                raise GraphDefinitionError("workflow node snapshot has unexpected fields")
            if not isinstance(item["retry_policy"], Mapping) or set(item["retry_policy"]) != {"max_attempts"}:
                raise GraphDefinitionError("workflow node retry_policy is invalid")
        for item in value["edges"]:
            if not isinstance(item, Mapping) or set(item) != edge_fields:
                raise GraphDefinitionError("workflow edge snapshot has unexpected fields")
        nodes = tuple(
            NodeDefinition(
                node_id=item["node_id"],
                kind=item["kind"],
                handler_id=item["handler_id"],
                tool_policy_ref=item.get("tool_policy_ref"),
                approval_mode=item.get("approval_mode"),
                input_contract=tuple(item.get("input_contract", ())),
                output_contract=tuple(item.get("output_contract", ())),
                retry_policy=RetryPolicy(**item.get("retry_policy", {})),
                parallel_class=item.get("parallel_class", ParallelClass.SERIAL.value),
                is_terminal=item.get("is_terminal", False),
                max_visits=item.get("max_visits", 1),
            )
            for item in value["nodes"]
        )
        edges = tuple(
            EdgeDefinition(
                edge_id=item["edge_id"],
                source_node_id=item["source_node_id"],
                target_node_id=item["target_node_id"],
                rule=item.get("rule", EdgeRule.ALWAYS.value),
                expected_value=item.get("expected_value"),
                state_key=item.get("state_key"),
                priority=item.get("priority", 0),
            )
            for item in value["edges"]
        )
        return WorkflowDefinition(
            workflow_id=value["workflow_id"],
            version=value["version"],
            start_node_id=value["start_node_id"],
            nodes=nodes,
            edges=edges,
        )
    except (KeyError, TypeError, GraphDefinitionError) as exc:
        if isinstance(exc, GraphDefinitionError):
            raise
        raise GraphDefinitionError("workflow definition snapshot is invalid") from exc


def create_initial_workflow_state(
    *,
    task_id: str,
    conversation_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """创建可持久化的固定状态骨架。"""
    _require_record_identifier(task_id, "task_id", GraphStateError)
    state = {
        "schema_version": WORKFLOW_STATE_SCHEMA_VERSION,
        "root": {
            "task_id": task_id,
            "conversation_id": conversation_id or "",
            "session_id": session_id or "",
        },
        "routing": {},
        "node_outputs": {},
        "branches": {},
        "gates": {},
        "artifacts": {},
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
        },
        "errors": [],
    }
    return validate_workflow_state(state, task_id=task_id)


def validate_workflow_state(state: Mapping[str, Any], *, task_id: str | None = None) -> dict[str, Any]:
    """验证并规范化状态，拒绝大对象、密钥和完整对话内容。"""
    normalized = _normalize_safe_json(state, "workflow state", WORKFLOW_STATE_MAX_BYTES)
    if not isinstance(normalized, dict) or set(normalized) != _STATE_KEYS:
        raise GraphStateError("workflow state must use the fixed state schema")
    if normalized["schema_version"] != WORKFLOW_STATE_SCHEMA_VERSION:
        raise GraphStateError("unsupported workflow state schema_version")
    root = normalized["root"]
    if not isinstance(root, dict) or set(root) != {"task_id", "conversation_id", "session_id"}:
        raise GraphStateError("workflow state root is invalid")
    _require_record_identifier(root["task_id"], "root.task_id", GraphStateError)
    if task_id is not None and root["task_id"] != task_id:
        raise GraphStateError("workflow state belongs to a different root task")
    if not all(isinstance(root[key], str) for key in ("conversation_id", "session_id")):
        raise GraphStateError("workflow root identifiers must be strings")
    for key in ("routing", "node_outputs", "branches", "gates", "artifacts"):
        if not isinstance(normalized[key], dict):
            raise GraphStateError(f"workflow state {key} must be an object")
    usage = normalized["usage"]
    if not isinstance(usage, dict) or set(usage) != _USAGE_KEYS:
        raise GraphStateError("workflow usage is invalid")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage.values()):
        raise GraphStateError("workflow usage values must be non-negative integers")
    if not isinstance(normalized["errors"], list):
        raise GraphStateError("workflow errors must be a list")
    return normalized


def validate_node_output_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    """验证节点摘要，禁止持久化完整聊天记录、密钥和不可序列化对象。"""
    if not isinstance(summary, Mapping):
        raise GraphStateError("output_summary must be an object")
    normalized = _normalize_safe_json(dict(summary), "output_summary", 16 * 1024)
    if not isinstance(normalized, dict):
        raise GraphStateError("output_summary must be an object")
    return normalized


def apply_node_result(state: Mapping[str, Any], *, node_run_id: str, result: NodeResult) -> dict[str, Any]:
    """把节点结果写入其专属命名空间，不允许覆盖兄弟节点输出。"""
    _require_record_identifier(node_run_id, "node_run_id", GraphStateError)
    if not isinstance(result, NodeResult):
        raise GraphStateError("result must be NodeResult")
    updated = validate_workflow_state(state)
    if node_run_id in updated["node_outputs"]:
        raise GraphStateError("node output already exists and cannot be overwritten")
    updated = copy.deepcopy(updated)
    updated["node_outputs"][node_run_id] = {
        "outcome": result.outcome,
        "summary": result.output_summary,
        "route_key": result.route_key,
    }
    if result.artifact_refs:
        updated["artifacts"][node_run_id] = list(result.artifact_refs)
    if result.outcome == "failure":
        updated["errors"].append({"node_run_id": node_run_id, "code": "node_failure"})
    return validate_workflow_state(updated)


def _validate_definition(definition: WorkflowDefinition) -> None:
    if not definition.nodes:
        raise GraphDefinitionError("workflow must contain at least one node")
    if any(not isinstance(node, NodeDefinition) for node in definition.nodes):
        raise GraphDefinitionError("workflow nodes must be NodeDefinition instances")
    if any(not isinstance(edge, EdgeDefinition) for edge in definition.edges):
        raise GraphDefinitionError("workflow edges must be EdgeDefinition instances")
    nodes = {node.node_id: node for node in definition.nodes}
    if len(nodes) != len(definition.nodes):
        raise GraphDefinitionError("workflow node_id values must be unique")
    if definition.start_node_id not in nodes:
        raise GraphDefinitionError("workflow start_node_id does not exist")
    edge_ids = {edge.edge_id for edge in definition.edges}
    if len(edge_ids) != len(definition.edges):
        raise GraphDefinitionError("workflow edge_id values must be unique")
    outgoing: dict[str, list[EdgeDefinition]] = {node_id: [] for node_id in nodes}
    for edge in definition.edges:
        if edge.source_node_id not in nodes or edge.target_node_id not in nodes:
            raise GraphDefinitionError("workflow edge references an unknown node")
        outgoing[edge.source_node_id].append(edge)
    for node_id, node in nodes.items():
        node_edges = outgoing[node_id]
        if node.is_terminal and node_edges:
            raise GraphDefinitionError("terminal nodes cannot have outgoing edges")
        if not node.is_terminal and not node_edges:
            raise GraphDefinitionError("non-terminal nodes must have an outgoing edge")
        always_edges = [edge for edge in node_edges if edge.rule is EdgeRule.ALWAYS]
        if len(always_edges) > 1:
            raise GraphDefinitionError("a node may have only one ALWAYS edge")
        conditions: set[tuple[EdgeRule, str | None, Any, int]] = set()
        for edge in node_edges:
            key = (edge.rule, edge.state_key, edge.expected_value, edge.priority)
            if key in conditions:
                raise GraphDefinitionError("ambiguous conditional workflow edge")
            conditions.add(key)
    reachable = _reachable_nodes(definition.start_node_id, outgoing)
    if set(nodes) != reachable:
        raise GraphDefinitionError("workflow contains unreachable nodes")
    if not any(node.is_terminal for node in nodes.values()):
        raise GraphDefinitionError("workflow must contain a terminal node")
    if not any(node_id in reachable and node.is_terminal for node_id, node in nodes.items()):
        raise GraphDefinitionError("workflow start cannot reach a terminal node")
    for cycle in _find_cycles(nodes, outgoing):
        if any(nodes[node_id].max_visits is None for node_id in cycle):
            raise GraphDefinitionError("workflow cycle must declare finite max_visits")


def _reachable_nodes(start_node_id: str, outgoing: Mapping[str, list[EdgeDefinition]]) -> set[str]:
    seen: set[str] = set()
    stack = [start_node_id]
    while stack:
        node_id = stack.pop()
        if node_id in seen:
            continue
        seen.add(node_id)
        stack.extend(edge.target_node_id for edge in outgoing[node_id])
    return seen


def _find_cycles(nodes: Mapping[str, NodeDefinition], outgoing: Mapping[str, list[EdgeDefinition]]) -> list[set[str]]:
    """Tarjan SCC: 单节点自环和多节点环均要求有限访问上限。"""
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[set[str]] = []

    def visit(node_id: str) -> None:
        nonlocal index
        indices[node_id] = index
        lowlinks[node_id] = index
        index += 1
        stack.append(node_id)
        on_stack.add(node_id)
        for edge in outgoing[node_id]:
            target = edge.target_node_id
            if target not in indices:
                visit(target)
                lowlinks[node_id] = min(lowlinks[node_id], lowlinks[target])
            elif target in on_stack:
                lowlinks[node_id] = min(lowlinks[node_id], indices[target])
        if lowlinks[node_id] != indices[node_id]:
            return
        component: set[str] = set()
        while stack:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == node_id:
                break
        if len(component) > 1 or any(edge.target_node_id == node_id for edge in outgoing[node_id]):
            cycles.append(component)

    for node_id in nodes:
        if node_id not in indices:
            visit(node_id)
    return cycles


def _normalize_contract(value: Iterable[str], field_name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise GraphDefinitionError(f"{field_name} must be a sequence")
    try:
        items = tuple(value)
    except TypeError as exc:
        raise GraphDefinitionError(f"{field_name} must be a sequence") from exc
    if len(set(items)) != len(items):
        raise GraphDefinitionError(f"{field_name} contains duplicates")
    for item in items:
        if item not in _STATE_KEYS - {"schema_version", "root"}:
            raise GraphDefinitionError(f"{field_name} contains an unsupported state field")
    return items


def _validate_state_path(value: str) -> None:
    parts = value.split(".")
    if not parts or parts[0] not in _STATE_KEYS:
        raise GraphDefinitionError("state_key must begin with a known state field")
    for part in parts:
        _require_identifier(part, "state_key", GraphDefinitionError)


def _require_identifier(value: str, field_name: str, error_type: type[ValueError]) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise error_type(f"invalid {field_name}")
    return value


def _require_record_identifier(value: str, field_name: str, error_type: type[ValueError]) -> str:
    if not isinstance(value, str) or not _RECORD_IDENTIFIER.fullmatch(value):
        raise error_type(f"invalid {field_name}")
    return value


def _normalize_safe_json(value: Any, label: str, limit: int) -> Any:
    _scan_safe_json(value, label)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise GraphStateError(f"{label} is not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise GraphStateError(f"{label} exceeds the size limit")
    return json.loads(encoded)


def _scan_safe_json(value: Any, label: str, depth: int = 0) -> None:
    if depth > 16:
        raise GraphStateError(f"{label} is nested too deeply")
    if isinstance(value, str):
        if "\x00" in value or _SECRET_VALUE.search(value):
            raise GraphStateError(f"{label} contains sensitive content")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise GraphStateError(f"{label} contains a non-finite number")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise GraphStateError(f"{label} contains a non-string key")
            normalized_key = key.lower()
            if (
                normalized_key in _SENSITIVE_KEYS
                or any(fragment in normalized_key for fragment in _SENSITIVE_KEY_FRAGMENTS)
            ):
                raise GraphStateError(f"{label} contains a sensitive field")
            _scan_safe_json(item, label, depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _scan_safe_json(item, label, depth + 1)
        return
    raise GraphStateError(f"{label} contains an unsupported value")

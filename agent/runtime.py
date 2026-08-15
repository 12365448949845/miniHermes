"""受控并发多 Agent Runtime：统一登记主 Agent Task/Run 与会话句柄。"""

import hashlib
import json
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from agent.agent import Agent, AgentRunAborted, ConversationResult
from agent.graph_runner import GraphRunner
from agent.recovery import RecoveryController
from agent.reproducibility import (
    ArtifactRetentionManager,
    ReplayMaterializer,
    ReplaySetupError,
)
from agent.worktree import (
    IntegrationWorkspace,
    WORKTREE_MUTATING_DELEGATE_TOOLS,
    WORKTREE_REQUIRED_PARENT_TOOLS,
    WORKTREE_WRITE_TOOLS,
    WorkspaceManager,
    WorkspaceIntegrationError,
    WorkspaceOperationError,
)
from agent.workspace_runner import (
    DockerWorkspaceCommandRunner,
    WorkspaceCommandResult,
    WorkspaceRunnerError,
)
from approval import ApprovalEngine, ApprovalMode, ApprovalResult
from provider import ProviderCallLimiter
from session import SessionDB
from tools.registry import (
    ToolAccessPolicy,
    ToolExecutionContext,
    ToolStatus,
    is_parallel_safe_delegate_policy,
    resolve_tool_access_policy,
)
import config as cfg


class AgentRunControlError(Exception):
    """协作式停止信号，不应被当作工具拒绝或普通业务错误。"""

    is_run_control = True

    def __init__(self, completion_reason: str, message: str = ""):
        super().__init__(message or completion_reason)
        self.completion_reason = completion_reason
        self.error_code = completion_reason
        self.safe_message = message or completion_reason


class AgentRunCancelled(AgentRunControlError):
    """用户、父 Run 或会话请求取消。"""

    def __init__(self, completion_reason: str = "user_interrupt",
                 message: str = "Agent run cancelled"):
        super().__init__(completion_reason, message)


class AgentRunTimedOut(AgentRunControlError):
    """Run deadline 到期。"""

    def __init__(self, completion_reason: str = "deadline_exceeded",
                 message: str = "Agent run timed out"):
        super().__init__(completion_reason, message)


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    INTERRUPTED = "INTERRUPTED"


RUN_TRANSITIONS = {
    RunStatus.QUEUED: {
        RunStatus.RUNNING,
        RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    },
    RunStatus.RUNNING: {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCEL_REQUESTED,
        RunStatus.INTERRUPTED,
    },
    RunStatus.CANCEL_REQUESTED: {
        RunStatus.CANCELLED,
        RunStatus.FAILED,
        RunStatus.TIMED_OUT,
        RunStatus.INTERRUPTED,
    },
}


@dataclass(frozen=True)
class AgentSpec:
    kind: str = "main_turn"
    system_prompt: str = ""
    tool_policy: ToolAccessPolicy | dict = field(default_factory=dict)
    approval_mode: ApprovalMode | str = ApprovalMode.INTERACTIVE
    max_iterations: int = 100
    timeout_seconds: float | None = None
    persist_messages: bool = True
    background: bool = False


@dataclass
class AgentRunContext:
    task_id: str
    run_id: str
    conversation_id: str
    start_session_id: str
    tool_policy: ToolAccessPolicy | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    parent_cancel_event: threading.Event | None = None
    parent_deadline_monotonic: float | None = None
    batch_cancel_event: threading.Event | None = None
    batch_deadline_monotonic: float | None = None
    deadline_monotonic: float | None = None
    provider_attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    event_callback: Optional[Callable[[str, dict], None]] = None
    cancel_reason: str | None = None
    # G0 仅携带只读图归属；G1 才由 GraphRunner 实际写入并调度节点。
    # 放在末尾以保留既有位置参数调用的兼容性。
    workflow_run_id: str | None = None
    node_run_id: str | None = None
    workspace_context: object | None = None

    def record_provider_attempt(self):
        self.provider_attempts += 1

    def record_provider_usage(self, prompt_tokens: int = 0,
                              completion_tokens: int = 0,
                              reasoning_tokens: int = 0):
        self.prompt_tokens += int(prompt_tokens or 0)
        self.completion_tokens += int(completion_tokens or 0)
        self.reasoning_tokens += int(reasoning_tokens or 0)

    def record_provider_call(self, prompt_tokens: int = 0,
                             completion_tokens: int = 0,
                             reasoning_tokens: int = 0):
        """旧调用兼容：记录一个 attempt 及其 usage。"""
        self.record_provider_attempt()
        self.record_provider_usage(
            prompt_tokens, completion_tokens, reasoning_tokens
        )

    def emit_event(self, event_type: str, payload: dict | None = None):
        if self.event_callback:
            self.event_callback(event_type, payload or {})

    def is_cancelled(self) -> bool:
        return self.abort_reason() is not None

    def abort_reason(self) -> str | None:
        """返回当前停止原因；deadline 优先于普通取消。"""
        if self.deadline_monotonic is not None and time.monotonic() >= self.deadline_monotonic:
            # deadline 与用户取消共用协作停止事件，子 Run 才能立即收到
            # 父 Run 的超时信号；终态仍由 Runtime 按 deadline 写 TIMED_OUT。
            self.cancel_reason = "deadline_exceeded"
            self.cancel_event.set()
            return "deadline_exceeded"
        if self.cancel_event.is_set():
            return self.cancel_reason or "user_interrupt"
        if (
            self.parent_deadline_monotonic is not None
            and time.monotonic() >= self.parent_deadline_monotonic
        ):
            return "parent_cancelled"
        if self.parent_cancel_event and self.parent_cancel_event.is_set():
            return "parent_cancelled"
        if (
            self.batch_deadline_monotonic is not None
            and time.monotonic() >= self.batch_deadline_monotonic
        ):
            self.cancel_reason = "deadline_exceeded"
            self.cancel_event.set()
            return "deadline_exceeded"
        if self.batch_cancel_event and self.batch_cancel_event.is_set():
            return "deadline_exceeded"
        return None

    def remaining_seconds(self) -> float | None:
        deadlines = [
            value for value in (
                self.deadline_monotonic,
                self.batch_deadline_monotonic,
                self.parent_deadline_monotonic,
            ) if value is not None
        ]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - time.monotonic())

    def raise_if_aborted(self):
        reason = self.abort_reason()
        if reason == "deadline_exceeded":
            raise AgentRunTimedOut()
        if reason:
            raise AgentRunCancelled(reason)


@dataclass
class SessionAgentHandle:
    conversation_id: str
    current_session_id: str
    agent: Agent
    session_cancel_event: threading.Event = field(default_factory=threading.Event)
    run_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class LiveRunHandle:
    run_id: str
    conversation_id: str
    agent: Agent | None
    owns_agent: bool
    cancel_event: threading.Event
    started_monotonic: float
    deadline_monotonic: float | None = None
    run_context: AgentRunContext | None = None


@dataclass
class AgentRunOutcome:
    task_id: str
    run_id: str
    status: RunStatus
    completion_reason: str
    result: ConversationResult | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DelegateBatchItem:
    """一项待调度的 Delegate，不包含父对话的可变状态。"""

    spec: AgentSpec
    request: dict
    renderer: object | None = None


@dataclass
class DelegateBatchOutcome:
    outcomes: list[AgentRunOutcome]
    parallel: bool
    completion_reason: str = "completed"


@dataclass
class _BackgroundJob:
    task_id: str
    run_id: str
    spec: AgentSpec
    request: dict
    run_context: AgentRunContext
    prepare: Callable | None = None
    on_complete: Callable | None = None
    sequence: int = 0


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*[^\s,;]+"
    ),
)


def sanitize_preview(value: str, limit: int = 500) -> str:
    """截断并脱敏 Runtime 表中的预览文本。"""
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = text.replace("\x00", "")
    return text[:limit]


class AgentRuntimeManager:
    """统一主 Run、Delegate、Plan 与后台 Agent 的生命周期。"""

    def __init__(self, db: SessionDB,
                 agent_factory: Optional[Callable[[], Agent]] = None,
                 ephemeral_factory: Optional[Callable[[AgentSpec, dict, AgentRunContext], Agent]] = None,
                 runtime_config: dict | None = None,
                 evidence_recorder=None,
                 approval_engine: ApprovalEngine | None = None,
                 approval_callback=None,
                 replay_root: str | Path | None = None,
                 workspace_manager: WorkspaceManager | None = None,
                 workspace_runner=None):
        self.db = db
        self._agent_factory = agent_factory
        self._ephemeral_factory = ephemeral_factory
        self._evidence_recorder = evidence_recorder
        self._replay_root = replay_root
        self._approval_engine = approval_engine or ApprovalEngine()
        self._approval_callback = approval_callback
        self._sessions: dict[str, SessionAgentHandle] = {}
        self._live_runs: dict[str, LiveRunHandle] = {}
        self._live_lock = threading.Lock()
        self._execution_gate = threading.RLock()
        self._background_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._background_stop = threading.Event()
        self._background_condition = threading.Condition(threading.Lock())
        self._foreground_waiters = 0
        self._background_sequence = 0
        self._background_thread = threading.Thread(
            target=self._background_worker,
            name="minihermes-background-agent",
            daemon=True,
        )
        self._runtime_config = runtime_config or cfg.get_agent_runtime_config()
        raw_worktree = self._runtime_config.get("worktree", {})
        if not isinstance(raw_worktree, dict):
            raw_worktree = {}
        try:
            max_write_concurrency = min(
                max(int(raw_worktree.get("max_write_concurrency", 1)), 1), 2
            )
        except (TypeError, ValueError):
            max_write_concurrency = 1
        self._worktree_config = {
            "enabled": bool(raw_worktree.get("enabled", False)),
            "max_write_concurrency": max_write_concurrency,
            "runner": str(raw_worktree.get("runner", "docker") or "docker").lower(),
            "docker_image": str(raw_worktree.get("docker_image", "") or "").strip(),
            "docker_user": str(raw_worktree.get("docker_user", "65532:65532") or "65532:65532"),
            "pids_limit": raw_worktree.get("pids_limit", 256),
            "memory_limit": str(raw_worktree.get("memory_limit", "1g") or "1g"),
            "preserve_failed_days": raw_worktree.get("preserve_failed_days", 30),
            "integration_verification_command": str(
                raw_worktree.get("integration_verification_command", "") or ""
            ).strip(),
        }
        self._workspace_manager = workspace_manager or WorkspaceManager()
        self._workspace_runner = workspace_runner
        self._workspace_runner_error: str | None = None
        if self._worktree_config["enabled"] and self._workspace_runner is None:
            if self._worktree_config["runner"] != "docker":
                self._workspace_runner_error = "strict_docker_runner_required"
            else:
                try:
                    self._workspace_runner = DockerWorkspaceCommandRunner(
                        self._worktree_config["docker_image"],
                        container_user=self._worktree_config["docker_user"],
                        pids_limit=self._worktree_config["pids_limit"],
                        memory_limit=self._worktree_config["memory_limit"],
                    )
                except (WorkspaceRunnerError, TypeError, ValueError) as exc:
                    self._workspace_runner_error = getattr(
                        exc, "reason_code", "runner_configuration_invalid"
                    )
        self._worktree_write_gate = threading.BoundedSemaphore(
            self._worktree_config["max_write_concurrency"]
        )
        # Git Worktree 创建、集成和清理会修改仓库共享元数据，始终串行。
        self._worktree_lifecycle_gate = threading.Lock()
        try:
            self._max_delegate_concurrency = min(
                max(int(self._runtime_config.get("max_concurrency", 1)), 1), 16
            )
        except (TypeError, ValueError):
            self._max_delegate_concurrency = 1
        self._delegate_execution_gate = threading.BoundedSemaphore(
            self._max_delegate_concurrency
        )
        # Worker 数量负责让等待项先登记为 QUEUED；真正同时执行的数量仍由
        # _delegate_execution_gate 和 _worktree_write_gate 严格限制。
        self._delegate_executor = ThreadPoolExecutor(
            max_workers=16,
            thread_name_prefix="minihermes-delegate",
        )
        self._provider_limiter = ProviderCallLimiter(
            self._max_delegate_concurrency
        )
        self.db.reconcile_agent_runs()
        self.db.reconcile_failure_recoveries()
        self.db.reconcile_workflow_runs()
        self.db.reconcile_worktree_integrations()
        self._workspace_manager.reconcile(
            self.db,
            artifact_store=getattr(self._evidence_recorder, "store", None),
        )
        self._graph_runner = GraphRunner(self.db)
        self._background_thread.start()

    def _attach_provider_limiter(self, agent: Agent | None):
        provider = getattr(agent, "provider", None)
        setter = getattr(provider, "set_call_limiter", None)
        if callable(setter):
            setter(self._provider_limiter)

    def _default_timeout(self, kind: str) -> float | None:
        values = self._runtime_config.get("run_timeout_seconds", {})
        value = values.get(kind)
        if value is None:
            return None
        try:
            return float(value) if float(value) > 0 else None
        except (TypeError, ValueError):
            return None

    def _effective_spec(self, spec: AgentSpec) -> AgentSpec:
        if spec.timeout_seconds is not None:
            return spec
        return replace(spec, timeout_seconds=self._default_timeout(spec.kind))

    def _prepare_worktree_context(
        self,
        *,
        task: dict,
        run_id: str,
        request: dict,
        parent_run_id: str | None,
        parent_run_context: AgentRunContext | None,
        resolved_policy: ToolAccessPolicy,
    ):
        if not self._worktree_config["enabled"]:
            raise WorkspaceOperationError(
                "worktree_disabled", "worktree_write is disabled in configuration"
            )
        if self._workspace_runner is None:
            raise WorkspaceOperationError(
                self._workspace_runner_error or "strict_runner_unavailable"
            )
        if parent_run_id is None or parent_run_context is None:
            raise WorkspaceOperationError(
                "worktree_parent_required",
                "worktree_write must be delegated by a live parent Agent run",
            )
        parent_policy = parent_run_context.tool_policy
        if (
            parent_policy is None
            or not WORKTREE_REQUIRED_PARENT_TOOLS <= parent_policy.effective_tools
        ):
            raise WorkspaceOperationError(
                "parent_write_permission_missing",
                "parent policy must grant bash and write_file before delegation",
            )
        if (
            not WORKTREE_REQUIRED_PARENT_TOOLS <= resolved_policy.effective_tools
            or not resolved_policy.effective_tools <= WORKTREE_WRITE_TOOLS
        ):
            raise WorkspaceOperationError(
                "worktree_tool_policy_invalid",
                "effective tools do not match the fixed worktree_write allowlist",
            )
        source_directory = request.get("_host_working_directory") or str(Path.cwd())
        with self._worktree_lifecycle_gate:
            parent_run_context.raise_if_aborted()
            return self._workspace_manager.provision(
                db=self.db,
                runner=self._workspace_runner,
                artifact_store=getattr(self._evidence_recorder, "store", None),
                task_id=task["task_id"],
                run_id=run_id,
                parent_run_id=parent_run_id,
                working_directory=source_directory,
                write_scope=request.get("write_scope") or (),
                preserve_failed_days=self._worktree_config["preserve_failed_days"],
            )

    @staticmethod
    def _is_control_error(exc: BaseException) -> bool:
        return bool(getattr(exc, "is_run_control", False))

    def _set_deadline(self, run_context: AgentRunContext, spec: AgentSpec):
        if spec.timeout_seconds is not None:
            run_context.deadline_monotonic = time.monotonic() + spec.timeout_seconds

    def _request_cancel_if_running(self, run_id: str, reason: str):
        current = self.db.get_agent_run(run_id)
        if current and current["status"] == "RUNNING":
            return self.db.request_agent_run_cancel(run_id, reason)
        return current["status"] if current else None

    def _foreground_waiter_enter(self):
        with self._background_condition:
            self._foreground_waiters += 1

    def _foreground_waiter_leave(self):
        with self._background_condition:
            self._foreground_waiters = max(0, self._foreground_waiters - 1)
            self._background_condition.notify_all()

    def _acquire_background_gate(self) -> bool:
        """在持有排队判定锁时取得执行门，避免前台插队。"""
        with self._background_condition:
            while (
                self._foreground_waiters > 0
                and not self._background_stop.is_set()
            ):
                self._background_condition.wait(0.1)
            if self._background_stop.is_set():
                return False
            self._execution_gate.acquire()
            return True

    def open_session(self, session_id: str, *, agent: Agent | None = None,
                     conversation_id: str | None = None) -> SessionAgentHandle:
        conversation_id = conversation_id or self.db.resolve_conversation_id(session_id)
        if conversation_id in self._sessions:
            raise RuntimeError(f"conversation already open: {conversation_id}")
        if agent is None:
            if self._agent_factory is None:
                raise RuntimeError("main Agent factory is not configured")
            agent = self._agent_factory()
        self._attach_provider_limiter(agent)
        handle = SessionAgentHandle(
            conversation_id=conversation_id,
            current_session_id=session_id,
            agent=agent,
        )
        self._sessions[conversation_id] = handle
        return handle

    def close_session(self, conversation_id: str):
        handle = self._sessions.pop(conversation_id, None)
        if not handle:
            return
        handle.session_cancel_event.set()
        self._cancel_live_runs(conversation_id, "session_cancelled")
        handle.agent.interrupt()
        handle.agent._approval.reset_session(conversation_id)

    def _cancel_live_runs(self, conversation_id: str, reason: str):
        with self._live_lock:
            live_runs = [
                live for live in self._live_runs.values()
                if live.conversation_id == conversation_id
            ]
            for live in live_runs:
                if live.run_context is not None:
                    live.run_context.cancel_reason = reason
                live.cancel_event.set()
                live.agent and live.agent.interrupt()
        for live in live_runs:
            try:
                self.db.request_agent_run_cancel(live.run_id, reason)
            except (KeyError, RuntimeError):
                pass

    def replace_session(self, old_conversation_id: str, new_session_id: str
                        ) -> SessionAgentHandle:
        self.close_session(old_conversation_id)
        return self.open_session(new_session_id)

    def get_session(self, conversation_id: str) -> SessionAgentHandle | None:
        return self._sessions.get(conversation_id)

    def create_task(self, *, conversation_id: str | None,
                    session_id: str | None,
                    kind: str, request: str, context: str = "",
                    parent_task_id: str | None = None) -> dict:
        task_id = uuid.uuid4().hex
        title = sanitize_preview(request.strip().splitlines()[0] if request.strip() else kind, 100)
        return self.db.create_agent_task(
            task_id=task_id,
            conversation_id=conversation_id,
            session_id=session_id,
            parent_task_id=parent_task_id,
            kind=kind,
            title=title,
            request_preview=sanitize_preview(request),
            context_preview=sanitize_preview(context),
        )

    def run_main_turn(self, *, conversation_id: str, user_message: str,
                      history: list[dict], renderer=None) -> AgentRunOutcome:
        handle = self._sessions.get(conversation_id)
        if handle is None:
            raise KeyError(f"conversation is not open: {conversation_id}")

        agent = handle.agent
        spec = AgentSpec(
            max_iterations=agent.max_iterations,
            tool_policy=agent.tool_policy,
            system_prompt=agent.system_prompt,
            approval_mode=agent.approval_mode,
            persist_messages=True,
        )
        spec = self._effective_spec(spec)
        approval_mode = ApprovalMode.coerce(spec.approval_mode)
        task = self.create_task(
            conversation_id=conversation_id,
            session_id=handle.current_session_id,
            kind=spec.kind,
            request=user_message,
        )
        run_id = uuid.uuid4().hex
        run = self.db.create_agent_run(
            run_id=run_id,
            task_id=task["task_id"],
            parent_run_id=None,
            conversation_id=conversation_id,
            start_session_id=handle.current_session_id,
            agent_kind=spec.kind,
            model=agent.provider.model,
            tool_policy_json=json.dumps(
                spec.tool_policy.to_record(), ensure_ascii=False
            ),
            approval_mode=approval_mode.value,
            max_iterations=spec.max_iterations,
            timeout_seconds=spec.timeout_seconds,
        )
        run_context = AgentRunContext(
            task_id=task["task_id"],
            run_id=run["run_id"],
            conversation_id=conversation_id,
            start_session_id=handle.current_session_id,
            tool_policy=spec.tool_policy,
            parent_cancel_event=handle.session_cancel_event,
            event_callback=lambda event_type, payload: self.db.append_agent_event(
                task["task_id"], run_id, event_type, payload
            ),
        )
        live = LiveRunHandle(
            run_id=run_id,
            conversation_id=conversation_id,
            agent=agent,
            owns_agent=False,
            cancel_event=run_context.cancel_event,
            started_monotonic=time.monotonic(),
            run_context=run_context,
        )
        with self._live_lock:
            self._live_runs[run_id] = live

        result = None
        foreground_waiting = True
        self._foreground_waiter_enter()
        try:
            if run_context.is_cancelled():
                reason = run_context.abort_reason() or "user_interrupt"
                self.db.request_agent_run_cancel(run_id, reason)
                return AgentRunOutcome(
                    task_id=task["task_id"],
                    run_id=run_id,
                    status=RunStatus.CANCELLED,
                    completion_reason=reason,
                    error_code=reason,
                )
            with handle.run_lock:
                with self._execution_gate:
                    self._foreground_waiter_leave()
                    foreground_waiting = False
                    try:
                        self.db.start_agent_run(run_id, task["task_id"])
                    except RuntimeError:
                        current = self.db.get_agent_run(run_id)
                        if current and current["status"] == "CANCELLED":
                            return AgentRunOutcome(
                                task_id=task["task_id"], run_id=run_id,
                                status=RunStatus.CANCELLED,
                                completion_reason=current.get("completion_reason")
                                or "user_interrupt",
                                error_code=current.get("completion_reason")
                                or "user_interrupt",
                            )
                        raise
                    graph_context = self._graph_runner.start_main_turn(
                        task_id=task["task_id"],
                        agent_run_id=run_id,
                        conversation_id=conversation_id,
                        session_id=handle.current_session_id,
                    )
                    run_context.workflow_run_id = graph_context.workflow_run_id
                    run_context.node_run_id = graph_context.node_run_id
                    self._set_deadline(run_context, spec)
                    run_context.raise_if_aborted()
                    try:
                        result = agent.run_conversation(
                            user_message=user_message,
                            history=history,
                            renderer=renderer,
                            session_id=handle.current_session_id,
                            run_context=run_context,
                        )
                    except AgentRunAborted as exc:
                        result = exc.partial_result
                        if run_context.abort_reason() == "deadline_exceeded":
                            self._request_cancel_if_running(run_id, "deadline_exceeded")
                            return self._finish_outcome(
                                handle, task["task_id"], run_context, result,
                                RunStatus.TIMED_OUT, "deadline_exceeded",
                                "deadline_exceeded", "Agent run timed out",
                            )
                        if run_context.abort_reason():
                            reason = run_context.abort_reason()
                            self._request_cancel_if_running(run_id, reason)
                            return self._finish_outcome(
                                handle, task["task_id"], run_context, result,
                                RunStatus.CANCELLED, reason, reason,
                                "Agent run cancelled",
                            )
                        return self._finish_outcome(
                            handle, task["task_id"], run_context, result,
                            RunStatus.FAILED, exc.completion_reason,
                            exc.error_code, exc.safe_message,
                        )
                    except KeyboardInterrupt:
                        self.db.request_agent_run_cancel(run_id, "user_interrupt")
                        return self._finish_outcome(
                            handle, task["task_id"], run_context, result,
                            RunStatus.CANCELLED, "user_interrupt",
                            "user_interrupt", "Agent run interrupted",
                        )

                    stored_status = self.db.get_agent_run(run_id)["status"]
                    abort_reason = run_context.abort_reason()
                    if abort_reason == "deadline_exceeded":
                        self._request_cancel_if_running(run_id, "deadline_exceeded")
                        terminal = RunStatus.TIMED_OUT
                        result.completion_reason = "deadline_exceeded"
                    elif abort_reason:
                        self._request_cancel_if_running(run_id, abort_reason)
                        terminal = RunStatus.CANCELLED
                        result.completion_reason = abort_reason
                    elif stored_status == "CANCEL_REQUESTED":
                        terminal = RunStatus.CANCELLED
                        result.completion_reason = "user_interrupt"
                    elif result.completion_reason == "user_interrupt":
                        self._request_cancel_if_running(run_id, "user_interrupt")
                        terminal = RunStatus.CANCELLED
                    elif result.completion_reason in ("stop", "completed"):
                        terminal = RunStatus.SUCCEEDED
                    else:
                        terminal = RunStatus.FAILED
                    error_code = None if terminal == RunStatus.SUCCEEDED else result.completion_reason
                    return self._finish_outcome(
                        handle, task["task_id"], run_context, result,
                        terminal, result.completion_reason, error_code,
                        error_code,
                    )
        except AgentRunControlError as exc:
            current = self.db.get_agent_run(run_id)
            if current and current["status"] == "RUNNING":
                self.db.request_agent_run_cancel(run_id, exc.completion_reason)
                current = self.db.get_agent_run(run_id)
            status = (
                RunStatus.TIMED_OUT
                if exc.completion_reason == "deadline_exceeded"
                else RunStatus.CANCELLED
            )
            if current and current["status"] == "CANCEL_REQUESTED":
                return self._finish_outcome(
                    handle, task["task_id"], run_context, result, status,
                    exc.completion_reason, exc.error_code, exc.safe_message,
                )
            return AgentRunOutcome(
                task_id=task["task_id"], run_id=run_id, status=status,
                completion_reason=exc.completion_reason, result=result,
                error_code=exc.error_code, error_message=exc.safe_message,
            )
        except Exception as exc:
            safe_error = sanitize_preview(f"{type(exc).__name__}: {exc}")
            current = self.db.get_agent_run(run_id)
            if current and current["status"] == "CANCELLED":
                return AgentRunOutcome(
                    task_id=task["task_id"],
                    run_id=run_id,
                    status=RunStatus.CANCELLED,
                    completion_reason=current["completion_reason"] or "user_interrupt",
                    result=result,
                    error_code="user_interrupt",
                )
            if current and current["status"] in ("RUNNING", "CANCEL_REQUESTED"):
                return self._finish_outcome(
                    handle, task["task_id"], run_context, result,
                    RunStatus.FAILED, "internal_error", "internal_error", safe_error,
                )
            return AgentRunOutcome(
                task_id=task["task_id"],
                run_id=run_id,
                status=RunStatus.FAILED,
                completion_reason="internal_error",
                result=result,
                error_code="internal_error",
                error_message=safe_error,
            )
        finally:
            if foreground_waiting:
                self._foreground_waiter_leave()
            with self._live_lock:
                self._live_runs.pop(run_id, None)

    def run_ephemeral(
        self,
        *,
        spec: AgentSpec,
        request: dict,
        conversation_id: str | None = None,
        session_id: str | None = None,
        parent_task_id: str | None = None,
        parent_run_id: str | None = None,
        parent_run_context: AgentRunContext | None = None,
        parent_tool_policy: ToolAccessPolicy | None = None,
        renderer=None,
        _batch_cancel_event: threading.Event | None = None,
        _batch_deadline_monotonic: float | None = None,
        _skip_execution_gate: bool = False,
    ) -> AgentRunOutcome:
        """通过统一 Runtime 同步运行一个临时 Agent。"""
        if self._ephemeral_factory is None:
            raise RuntimeError("ephemeral Agent factory is not configured")
        import tools as tool_registry
        spec = self._effective_spec(spec)
        execution_mode = request.get("execution_mode")
        preflight_error: WorkspaceOperationError | None = None
        if execution_mode not in {None, "read_only", "worktree_write"}:
            preflight_error = WorkspaceOperationError("invalid_execution_mode")
        if execution_mode == "worktree_write":
            if spec.kind != "delegate" or spec.background:
                preflight_error = WorkspaceOperationError(
                    "worktree_delegate_required"
                )
            spec = replace(
                spec,
                tool_policy={"include": set(WORKTREE_WRITE_TOOLS)},
                approval_mode=ApprovalMode.DENY_SENSITIVE,
            )

        parent_policy = (
            parent_run_context.tool_policy
            if parent_run_context and parent_run_context.tool_policy
            else parent_tool_policy
        )
        requested_policy = spec.tool_policy
        if isinstance(requested_policy, ToolAccessPolicy):
            # AgentSpec 中即使收到冻结策略，也只能作为“继续向下收窄”的输入；
            # Runtime 必须重新应用 kind 上限和父 Run 权限。
            requested_policy = {
                "include": set(requested_policy.effective_tools),
                "exclude": set(requested_policy.exclude),
                "argument_allow": requested_policy.argument_allow,
            }
        resolved_policy = resolve_tool_access_policy(
            requested_policy,
            tool_registry.get_tool_manager().get_names(),
            kind=spec.kind,
            parent_policy=parent_policy,
        )
        if (
            preflight_error is None
            and spec.kind == "delegate"
            and execution_mode != "worktree_write"
            and resolved_policy.effective_tools & WORKTREE_MUTATING_DELEGATE_TOOLS
        ):
            preflight_error = WorkspaceOperationError(
                "delegate_write_requires_worktree",
                "Delegate write_file and bash access requires worktree_write",
            )
        if (
            preflight_error is None
            and execution_mode == "read_only"
            and not is_parallel_safe_delegate_policy(resolved_policy)
        ):
            preflight_error = WorkspaceOperationError(
                "read_only_tool_policy_invalid"
            )
        approval_mode = ApprovalMode.coerce(spec.approval_mode)
        spec = replace(
            spec,
            tool_policy=resolved_policy,
            approval_mode=approval_mode,
        )
        user_message = request.get("user_message") or request.get("task") or ""
        task_request = request.get("task") or user_message
        context = request.get("context", "")
        task = self.create_task(
            conversation_id=conversation_id or "",
            session_id=session_id,
            kind=spec.kind,
            request=task_request,
            context=context,
            parent_task_id=parent_task_id,
        )
        run_id = uuid.uuid4().hex
        run = self.db.create_agent_run(
            run_id=run_id,
            task_id=task["task_id"],
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
            start_session_id=session_id,
            agent_kind=spec.kind,
            model=request.get("model", ""),
            tool_policy_json=json.dumps(
                resolved_policy.to_record(), ensure_ascii=False
            ),
            approval_mode=approval_mode.value,
            max_iterations=spec.max_iterations,
            timeout_seconds=spec.timeout_seconds,
        )
        run_context = AgentRunContext(
            task_id=task["task_id"],
            run_id=run["run_id"],
            conversation_id=conversation_id or "",
            start_session_id=session_id or "",
            tool_policy=resolved_policy,
            parent_cancel_event=(
                parent_run_context.cancel_event if parent_run_context else None
            ),
            parent_deadline_monotonic=(
                parent_run_context.deadline_monotonic
                if parent_run_context else None
            ),
            batch_cancel_event=_batch_cancel_event,
            batch_deadline_monotonic=_batch_deadline_monotonic,
            event_callback=lambda event_type, payload: self.db.append_agent_event(
                task["task_id"], run_id, event_type, payload
            ),
        )
        live = LiveRunHandle(
            run_id=run_id,
            conversation_id=conversation_id or "",
            agent=None,
            owns_agent=True,
            cancel_event=run_context.cancel_event,
            started_monotonic=time.monotonic(),
            run_context=run_context,
        )
        with self._live_lock:
            self._live_runs[run_id] = live

        result = None
        workspace_context = None
        delegate_gate_acquired = False
        worktree_gate_acquired = False
        worktree_finalized = False
        try:
            execution_gate = (
                nullcontext() if _skip_execution_gate else self._execution_gate
            )
            with execution_gate:
                if run_context.is_cancelled():
                    reason = run_context.abort_reason() or "parent_cancelled"
                    self.db.request_agent_run_cancel(run_id, reason)
                    return AgentRunOutcome(
                        task_id=task["task_id"],
                        run_id=run_id,
                        status=(
                            RunStatus.TIMED_OUT
                            if reason == "deadline_exceeded"
                            else RunStatus.CANCELLED
                        ),
                        completion_reason=reason,
                        error_code=reason,
                    )
                if preflight_error is not None:
                    self.db.start_agent_run(run_id, task["task_id"])
                    self._set_deadline(run_context, spec)
                    raise preflight_error
                if _skip_execution_gate:
                    self._acquire_delegate_execution_gate(run_context)
                    delegate_gate_acquired = True
                if execution_mode == "worktree_write":
                    self._acquire_worktree_write_gate(run_context)
                    worktree_gate_acquired = True
                if run_context.is_cancelled():
                    reason = run_context.abort_reason() or "parent_cancelled"
                    self.db.request_agent_run_cancel(run_id, reason)
                    return AgentRunOutcome(
                        task_id=task["task_id"],
                        run_id=run_id,
                        status=(
                            RunStatus.TIMED_OUT
                            if reason == "deadline_exceeded"
                            else RunStatus.CANCELLED
                        ),
                        completion_reason=reason,
                        error_code=reason,
                    )
                self.db.start_agent_run(run_id, task["task_id"])
                self._set_deadline(run_context, spec)
                run_context.raise_if_aborted()
                if execution_mode == "worktree_write":
                    workspace_context = self._prepare_worktree_context(
                        task=task,
                        run_id=run_id,
                        request=request,
                        parent_run_id=parent_run_id,
                        parent_run_context=parent_run_context,
                        resolved_policy=resolved_policy,
                    )
                    run_context.workspace_context = workspace_context
                    self._workspace_manager.start(workspace_context)
                try:
                    child_agent = self._ephemeral_factory(spec, request, run_context)
                    self._attach_provider_limiter(child_agent)
                    live.agent = child_agent
                    result = child_agent.run_conversation(
                        user_message=user_message,
                        history=request.get("history", []),
                        renderer=renderer,
                        session_id=session_id if spec.persist_messages else None,
                        run_context=run_context,
                    )
                except AgentRunAborted as exc:
                    result = exc.partial_result
                    self.db.finish_agent_run(
                        run_id=run_id,
                        task_id=task["task_id"],
                        status="FAILED",
                        completion_reason=exc.completion_reason,
                        end_session_id=session_id,
                        result_preview=sanitize_preview(result.final_response),
                        error_code=exc.error_code,
                        error_message=sanitize_preview(exc.safe_message),
                        iterations_used=result.iterations_used,
                        provider_attempts=run_context.provider_attempts,
                        prompt_tokens=run_context.prompt_tokens,
                        completion_tokens=run_context.completion_tokens,
                        reasoning_tokens=run_context.reasoning_tokens,
                    )
                    return AgentRunOutcome(
                        task_id=task["task_id"], run_id=run_id,
                        status=RunStatus.FAILED,
                        completion_reason=exc.completion_reason,
                        result=result, error_code=exc.error_code,
                        error_message=sanitize_preview(exc.safe_message),
                    )

                stored_status = self.db.get_agent_run(run_id)["status"]
                abort_reason = run_context.abort_reason()
                if abort_reason == "deadline_exceeded":
                    self._request_cancel_if_running(run_id, "deadline_exceeded")
                    terminal = RunStatus.TIMED_OUT
                    completion_reason = "deadline_exceeded"
                    error_code = completion_reason
                elif abort_reason or stored_status == "CANCEL_REQUESTED" or result.completion_reason == "user_interrupt":
                    cancel_reason = (
                        abort_reason
                        or ("parent_cancelled"
                            if parent_run_context and parent_run_context.is_cancelled()
                            else "user_interrupt")
                    )
                    self._request_cancel_if_running(run_id, cancel_reason)
                    terminal = RunStatus.CANCELLED
                    completion_reason = cancel_reason
                    error_code = completion_reason
                elif workspace_context is not None and workspace_context.failure_code:
                    terminal = RunStatus.FAILED
                    completion_reason = workspace_context.failure_code
                    error_code = workspace_context.failure_code
                elif result.completion_reason in ("stop", "completed"):
                    terminal = RunStatus.SUCCEEDED
                    completion_reason = result.completion_reason
                    error_code = None
                else:
                    terminal = RunStatus.FAILED
                    completion_reason = result.completion_reason
                    error_code = completion_reason
                if workspace_context is not None:
                    try:
                        lease = self._workspace_manager.finalize(
                            workspace_context, terminal.value
                        )
                        worktree_finalized = True
                    except Exception as exc:
                        run_context.emit_event(
                            "worktree_finalize_failed",
                            {"error_type": type(exc).__name__},
                        )
                        terminal = RunStatus.FAILED
                        completion_reason = "candidate_finalize_failed"
                        error_code = completion_reason
                    else:
                        if (
                            terminal == RunStatus.SUCCEEDED
                            and lease["lease_status"] == "FAILED"
                        ):
                            terminal = RunStatus.FAILED
                            completion_reason = (
                                lease.get("failure_code")
                                or "candidate_finalize_failed"
                            )
                            error_code = completion_reason
                self.db.finish_agent_run(
                    run_id=run_id,
                    task_id=task["task_id"],
                    status=terminal.value,
                    completion_reason=completion_reason,
                    end_session_id=session_id,
                    result_preview=sanitize_preview(result.final_response),
                    error_code=error_code,
                    error_message=error_code,
                    iterations_used=result.iterations_used,
                    provider_attempts=run_context.provider_attempts,
                    prompt_tokens=run_context.prompt_tokens,
                    completion_tokens=run_context.completion_tokens,
                    reasoning_tokens=run_context.reasoning_tokens,
                )
                return AgentRunOutcome(
                    task_id=task["task_id"], run_id=run_id,
                    status=terminal, completion_reason=completion_reason,
                    result=result, error_code=error_code,
                    error_message=error_code,
                )
        except AgentRunControlError as exc:
            current = self.db.get_agent_run(run_id)
            if current and current["status"] in {"QUEUED", "RUNNING"}:
                self.db.request_agent_run_cancel(run_id, exc.completion_reason)
                current = self.db.get_agent_run(run_id)
            status = (
                RunStatus.TIMED_OUT
                if exc.completion_reason == "deadline_exceeded"
                else RunStatus.CANCELLED
            )
            if current and current["status"] == "CANCEL_REQUESTED":
                self.db.finish_agent_run(
                    run_id=run_id, task_id=task["task_id"],
                    status=status.value, completion_reason=exc.completion_reason,
                    end_session_id=session_id,
                    result_preview=sanitize_preview(result.final_response if result else ""),
                    error_code=exc.error_code,
                    error_message=sanitize_preview(exc.safe_message),
                    iterations_used=result.iterations_used if result else 0,
                    provider_attempts=run_context.provider_attempts,
                    prompt_tokens=run_context.prompt_tokens,
                    completion_tokens=run_context.completion_tokens,
                    reasoning_tokens=run_context.reasoning_tokens,
                )
            return AgentRunOutcome(
                task_id=task["task_id"], run_id=run_id, status=status,
                completion_reason=exc.completion_reason, result=result,
                error_code=exc.error_code, error_message=exc.safe_message,
            )
        except WorkspaceOperationError as exc:
            safe_error = sanitize_preview(str(exc))
            current = self.db.get_agent_run(run_id)
            if current and current["status"] in ("RUNNING", "CANCEL_REQUESTED"):
                self.db.finish_agent_run(
                    run_id=run_id,
                    task_id=task["task_id"],
                    status="FAILED",
                    completion_reason="worktree_rejected",
                    end_session_id=session_id,
                    error_code=exc.reason_code,
                    error_message=safe_error,
                )
            return AgentRunOutcome(
                task_id=task["task_id"],
                run_id=run_id,
                status=RunStatus.FAILED,
                completion_reason="worktree_rejected",
                error_code=exc.reason_code,
                error_message=safe_error,
            )
        except Exception as exc:
            safe_error = sanitize_preview(f"{type(exc).__name__}: {exc}")
            current = self.db.get_agent_run(run_id)
            if current and current["status"] in {"CANCELLED", "TIMED_OUT"}:
                terminal = RunStatus(current["status"])
                return AgentRunOutcome(
                    task_id=task["task_id"], run_id=run_id,
                    status=terminal,
                    completion_reason=current.get("completion_reason") or terminal.value.lower(),
                    result=result,
                    error_code=current.get("completion_reason") or terminal.value.lower(),
                )
            if current and current["status"] in ("RUNNING", "CANCEL_REQUESTED"):
                self.db.finish_agent_run(
                    run_id=run_id,
                    task_id=task["task_id"],
                    status="FAILED",
                    completion_reason="internal_error",
                    end_session_id=session_id,
                    error_code="internal_error",
                    error_message=safe_error,
                )
            return AgentRunOutcome(
                task_id=task["task_id"], run_id=run_id,
                status=RunStatus.FAILED,
                completion_reason="internal_error",
                result=result, error_code="internal_error",
                error_message=safe_error,
            )
        finally:
            if workspace_context is not None and not worktree_finalized:
                try:
                    current = self.db.get_agent_run(run_id) or {}
                    self._workspace_manager.finalize(
                        workspace_context, current.get("status", "FAILED")
                    )
                except Exception as exc:
                    run_context.emit_event(
                        "worktree_finalize_failed",
                        {"error_type": type(exc).__name__},
                    )
            if worktree_gate_acquired:
                self._worktree_write_gate.release()
            if delegate_gate_acquired:
                self._delegate_execution_gate.release()
            with self._live_lock:
                self._live_runs.pop(run_id, None)

    def _acquire_delegate_execution_gate(
        self, run_context: AgentRunContext
    ) -> None:
        """等待全局 Delegate 槽位；等待期间 Run 保持 QUEUED。"""
        while not self._delegate_execution_gate.acquire(timeout=0.1):
            run_context.raise_if_aborted()
        try:
            run_context.raise_if_aborted()
        except BaseException:
            self._delegate_execution_gate.release()
            raise

    def _acquire_worktree_write_gate(
        self, run_context: AgentRunContext
    ) -> None:
        """等待受限写入槽位，同时保留取消和 deadline 语义。"""
        while not self._worktree_write_gate.acquire(timeout=0.1):
            run_context.raise_if_aborted()
        try:
            run_context.raise_if_aborted()
        except BaseException:
            self._worktree_write_gate.release()
            raise

    def _delegate_batch_timeout(self) -> float:
        try:
            value = float(
                self._runtime_config.get("delegate_batch_timeout_seconds", 300.0)
            )
            return min(max(value, 1.0), 3600.0)
        except (TypeError, ValueError):
            return 300.0

    def _delegate_item_is_parallel_safe(
        self,
        item: DelegateBatchItem,
        parent_policy: ToolAccessPolicy | None,
    ) -> bool:
        """在调度前冻结权限，未知、外部或有副作用工具一律回退串行。"""
        if item.spec.kind != "delegate" or item.spec.background:
            return False
        import tools as tool_registry

        policy = item.spec.tool_policy
        if isinstance(policy, ToolAccessPolicy):
            policy = {
                "include": set(policy.effective_tools),
                "exclude": set(policy.exclude),
                "argument_allow": policy.argument_allow,
            }
        resolved = resolve_tool_access_policy(
            policy,
            tool_registry.get_tool_manager().get_names(),
            kind="delegate",
            parent_policy=parent_policy,
        )
        execution_mode = item.request.get("execution_mode")
        if execution_mode == "worktree_write":
            return bool(
                self._worktree_config["enabled"]
                and self._worktree_config["max_write_concurrency"] > 1
                and self._workspace_runner is not None
                and WORKTREE_REQUIRED_PARENT_TOOLS <= resolved.effective_tools
                and resolved.effective_tools <= WORKTREE_WRITE_TOOLS
            )
        if execution_mode not in {None, "read_only"}:
            return False
        return is_parallel_safe_delegate_policy(resolved)

    def _worktree_batch_is_parallel_ready(
        self, items: list[DelegateBatchItem]
    ) -> bool:
        """验证严格 Runner、仓库身份和每个任务自己的冻结写入范围。"""
        write_items = [
            item for item in items
            if item.request.get("execution_mode") == "worktree_write"
        ]
        if not write_items:
            return True
        if (
            not self._worktree_config["enabled"]
            or self._worktree_config["max_write_concurrency"] <= 1
            or self._workspace_runner is None
        ):
            return False
        try:
            probe = self._workspace_runner.probe()
        except Exception:
            return False
        if getattr(probe, "backend", None) != "docker":
            return False

        for item in write_items:
            source = item.request.get("_host_working_directory") or str(Path.cwd())
            inspection = self._workspace_manager.inspect_git_workspace(source)
            if not inspection.eligible or inspection.git_root is None:
                return False
            try:
                self._workspace_manager.validate_write_scope(
                    item.request.get("write_scope") or (),
                    workspace_root=inspection.git_root,
                )
            except Exception:
                return False
        return True

    @staticmethod
    def _batch_stopped_outcome(reason: str) -> AgentRunOutcome:
        status = (
            RunStatus.TIMED_OUT
            if reason == "deadline_exceeded"
            else RunStatus.CANCELLED
        )
        return AgentRunOutcome(
            task_id="",
            run_id="",
            status=status,
            completion_reason=reason,
            error_code=reason,
            error_message=reason,
        )

    def _cancel_batch_runs(self, batch_event: threading.Event, reason: str):
        """取消已登记的 batch 子 Run；尚未启动的 future 由调用方取消。"""
        with self._live_lock:
            run_ids = [
                live.run_id for live in self._live_runs.values()
                if (
                    live.run_context is not None
                    and live.run_context.batch_cancel_event is batch_event
                )
            ]
        for run_id in run_ids:
            try:
                self.cancel(run_id, reason=reason)
            except (KeyError, RuntimeError):
                pass

    def run_delegate_batch(
        self,
        *,
        items: list[DelegateBatchItem],
        conversation_id: str,
        session_id: str | None,
        parent_task_id: str,
        parent_run_id: str,
        parent_run_context: AgentRunContext,
        parent_tool_policy: ToolAccessPolicy | None = None,
    ) -> DelegateBatchOutcome:
        """执行一个纯 Delegate 批次，并保证返回结果与输入顺序一致。"""
        if not items:
            return DelegateBatchOutcome([], parallel=False)

        parent_policy = parent_run_context.tool_policy or parent_tool_policy
        parallel = (
            self._max_delegate_concurrency > 1
            and all(
                self._delegate_item_is_parallel_safe(item, parent_policy)
                for item in items
            )
            and self._worktree_batch_is_parallel_ready(items)
        )
        batch_event = threading.Event()
        deadline = time.monotonic() + self._delegate_batch_timeout()
        if parent_run_context.deadline_monotonic is not None:
            deadline = min(deadline, parent_run_context.deadline_monotonic)

        def stopped_reason() -> str | None:
            if parent_run_context.is_cancelled():
                return parent_run_context.abort_reason() or "parent_cancelled"
            if time.monotonic() >= deadline:
                return "deadline_exceeded"
            return None

        def run_item(item: DelegateBatchItem, *, queued_renderer=None):
            return self.run_ephemeral(
                spec=item.spec,
                request=item.request,
                conversation_id=conversation_id,
                session_id=session_id,
                parent_task_id=parent_task_id,
                parent_run_id=parent_run_id,
                parent_run_context=parent_run_context,
                parent_tool_policy=parent_policy,
                renderer=queued_renderer if queued_renderer is not None else item.renderer,
                _batch_cancel_event=batch_event,
                _batch_deadline_monotonic=deadline,
                _skip_execution_gate=parallel,
            )

        # 由父 Run 持有执行门：后台维护任务无法插入，而 worker 可在此门内并发。
        with self._execution_gate:
            if not parallel:
                outcomes = []
                for item in items:
                    reason = stopped_reason()
                    if reason:
                        batch_event.set()
                        outcomes.append(self._batch_stopped_outcome(reason))
                        continue
                    outcomes.append(run_item(item))
                return DelegateBatchOutcome(
                    outcomes,
                    parallel=False,
                    completion_reason=(stopped_reason() or "completed"),
                )

            from renderer import QueuedRenderer

            queued_renderers = [
                QueuedRenderer(item.renderer) if item.renderer is not None else None
                for item in items
            ]
            futures = {
                self._delegate_executor.submit(
                    run_item, item, queued_renderer=queued_renderers[index]
                ): index
                for index, item in enumerate(items)
            }
            pending = set(futures)
            outcomes: list[AgentRunOutcome | None] = [None] * len(items)
            batch_reason = "completed"
            cancel_deadline: float | None = None

            while pending:
                done, pending = wait(pending, timeout=0.05)
                for future in done:
                    index = futures[future]
                    if future.cancelled():
                        outcomes[index] = self._batch_stopped_outcome(batch_reason)
                    else:
                        try:
                            outcomes[index] = future.result()
                        except Exception as exc:
                            outcomes[index] = AgentRunOutcome(
                                "", "", RunStatus.FAILED, "internal_error",
                                error_code="internal_error",
                                error_message=sanitize_preview(str(exc)),
                            )
                for queued_renderer in queued_renderers:
                    if queued_renderer is not None:
                        queued_renderer.drain()

                reason = stopped_reason()
                if reason and cancel_deadline is None:
                    batch_reason = reason
                    batch_event.set()
                    self._cancel_batch_runs(batch_event, reason)
                    for future in pending:
                        future.cancel()
                    cancel_deadline = time.monotonic() + float(
                        self._runtime_config.get("cancel_grace_seconds", 3.0)
                    )
                if cancel_deadline is not None and time.monotonic() >= cancel_deadline:
                    # Worktree writer 必须完成候选审计和 lease 收尾后才能把控制权
                    # 交还父 Run；Python 线程无法被安全强杀，提前返回会让它继续写 DB。
                    pending_worktree = any(
                        items[futures[future]].request.get("execution_mode")
                        == "worktree_write"
                        for future in pending
                    )
                    if not pending_worktree:
                        break

            for queued_renderer in queued_renderers:
                if queued_renderer is not None:
                    queued_renderer.drain()
            for future in pending:
                future.cancel()
                index = futures[future]
                outcomes[index] = self._batch_stopped_outcome(batch_reason)
            return DelegateBatchOutcome(
                [outcome or self._batch_stopped_outcome(batch_reason) for outcome in outcomes],
                parallel=True,
                completion_reason=batch_reason,
            )

    def _finish_outcome(self, handle: SessionAgentHandle, task_id: str,
                        run_context: AgentRunContext,
                        result: ConversationResult | None, status: RunStatus,
                        completion_reason: str, error_code: str | None,
                        error_message: str | None) -> AgentRunOutcome:
        if result and result.session_id:
            handle.current_session_id = result.session_id
        safe_error = sanitize_preview(error_message or "") or None
        if status in (RunStatus.CANCELLED, RunStatus.TIMED_OUT):
            self._request_cancel_if_running(run_context.run_id, completion_reason)
        result_preview = sanitize_preview(result.final_response if result else "")
        if run_context.workflow_run_id and run_context.node_run_id:
            self._graph_runner.finish_main_turn(
                task_id=task_id,
                agent_run_id=run_context.run_id,
                workflow_run_id=run_context.workflow_run_id,
                node_run_id=run_context.node_run_id,
                agent_status=status.value,
                completion_reason=completion_reason,
                end_session_id=handle.current_session_id,
                result_preview=result_preview,
                error_code=error_code,
                error_message=safe_error,
                iterations_used=(result.iterations_used if result else 0),
                provider_attempts=run_context.provider_attempts,
                prompt_tokens=run_context.prompt_tokens,
                completion_tokens=run_context.completion_tokens,
                reasoning_tokens=run_context.reasoning_tokens,
            )
        else:
            self.db.finish_agent_run(
                run_id=run_context.run_id,
                task_id=task_id,
                status=status.value,
                completion_reason=completion_reason,
                end_session_id=handle.current_session_id,
                result_preview=result_preview,
                error_code=error_code,
                error_message=safe_error,
                iterations_used=(result.iterations_used if result else 0),
                provider_attempts=run_context.provider_attempts,
                prompt_tokens=run_context.prompt_tokens,
                completion_tokens=run_context.completion_tokens,
                reasoning_tokens=run_context.reasoning_tokens,
            )
        return AgentRunOutcome(
            task_id=task_id,
            run_id=run_context.run_id,
            status=status,
            completion_reason=completion_reason,
            result=result,
            error_code=error_code,
            error_message=safe_error,
        )

    def submit_background(
        self,
        *,
        spec: AgentSpec,
        request: dict,
        conversation_id: str | None = None,
        session_id: str | None = None,
        parent_task_id: str | None = None,
        parent_run_id: str | None = None,
        parent_run_context: AgentRunContext | None = None,
        parent_tool_policy: ToolAccessPolicy | None = None,
        prepare: Callable | None = None,
        on_complete: Callable | None = None,
    ) -> AgentRunOutcome:
        """登记并排队一个低优先级 Run；返回时尚未执行。"""
        if self._background_stop.is_set():
            raise RuntimeError("Agent Runtime is shutting down")
        if self._ephemeral_factory is None:
            raise RuntimeError("ephemeral Agent factory is not configured")
        import tools as tool_registry

        spec = self._effective_spec(replace(spec, background=True))
        parent_policy = (
            parent_run_context.tool_policy
            if parent_run_context and parent_run_context.tool_policy
            else parent_tool_policy
        )
        requested_policy = spec.tool_policy
        if isinstance(requested_policy, ToolAccessPolicy):
            requested_policy = {
                "include": set(requested_policy.effective_tools),
                "exclude": set(requested_policy.exclude),
                "argument_allow": requested_policy.argument_allow,
            }
        resolved_policy = resolve_tool_access_policy(
            requested_policy,
            tool_registry.get_tool_manager().get_names(),
            kind=spec.kind,
            parent_policy=parent_policy,
        )
        spec = replace(
            spec,
            tool_policy=resolved_policy,
            approval_mode=ApprovalMode.coerce(spec.approval_mode),
        )
        user_message = request.get("user_message") or request.get("task") or ""
        task = self.create_task(
            conversation_id=conversation_id or "",
            session_id=session_id,
            kind=spec.kind,
            request=request.get("task") or user_message,
            context=request.get("context", ""),
            parent_task_id=parent_task_id,
        )
        run_id = uuid.uuid4().hex
        run = self.db.create_agent_run(
            run_id=run_id,
            task_id=task["task_id"],
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
            start_session_id=session_id,
            agent_kind=spec.kind,
            model=request.get("model", ""),
            tool_policy_json=json.dumps(resolved_policy.to_record(), ensure_ascii=False),
            approval_mode=spec.approval_mode.value,
            max_iterations=spec.max_iterations,
            timeout_seconds=spec.timeout_seconds,
        )
        session_handle = self._sessions.get(conversation_id or "")
        parent_event = (
            parent_run_context.cancel_event
            if parent_run_context
            else session_handle.session_cancel_event if session_handle else None
        )
        run_context = AgentRunContext(
            task_id=task["task_id"],
            run_id=run_id,
            conversation_id=conversation_id or "",
            start_session_id=session_id or "",
            tool_policy=resolved_policy,
            parent_cancel_event=parent_event,
            parent_deadline_monotonic=(
                parent_run_context.deadline_monotonic
                if parent_run_context else None
            ),
            event_callback=lambda event_type, payload: self.db.append_agent_event(
                task["task_id"], run_id, event_type, payload
            ),
        )
        live = LiveRunHandle(
            run_id=run_id,
            conversation_id=conversation_id or "",
            agent=None,
            owns_agent=True,
            cancel_event=run_context.cancel_event,
            started_monotonic=time.monotonic(),
            run_context=run_context,
        )
        with self._live_lock:
            self._live_runs[run_id] = live
        self._background_sequence += 1
        job = _BackgroundJob(
            task_id=task["task_id"],
            run_id=run_id,
            spec=spec,
            request=dict(request),
            run_context=run_context,
            prepare=prepare,
            on_complete=on_complete,
            sequence=self._background_sequence,
        )
        self.db.append_agent_event(task["task_id"], run_id, "background_queued")
        self._background_queue.put((10, job.sequence, job))
        return AgentRunOutcome(
            task_id=task["task_id"],
            run_id=run_id,
            status=RunStatus.QUEUED,
            completion_reason="queued",
        )

    def _background_worker(self):
        while True:
            _, _, job = self._background_queue.get()
            try:
                if job is None:
                    return
                outcome = self._run_background_job(job)
                if job.on_complete:
                    try:
                        job.on_complete(outcome)
                    except Exception:
                        pass
            finally:
                self._background_queue.task_done()

    def _run_background_job(self, job: _BackgroundJob) -> AgentRunOutcome:
        context = job.run_context
        result = None
        acquired_gate = False
        try:
            current = self.db.get_agent_run(job.run_id)
            if not current or current["status"] == "CANCELLED" or context.is_cancelled():
                if current and current["status"] == "QUEUED":
                    self.db.request_agent_run_cancel(
                        job.run_id, context.abort_reason() or "cancelled_before_start"
                    )
                return AgentRunOutcome(
                    job.task_id, job.run_id, RunStatus.CANCELLED,
                    (context.abort_reason() or (current or {}).get("completion_reason")
                     or "cancelled_before_start"),
                )
            acquired_gate = self._acquire_background_gate()
            if not acquired_gate:
                self.db.request_agent_run_cancel(job.run_id, "runtime_shutdown")
                return AgentRunOutcome(
                    job.task_id, job.run_id, RunStatus.CANCELLED,
                    "runtime_shutdown", error_code="runtime_shutdown",
                )
            current = self.db.get_agent_run(job.run_id)
            if not current or current["status"] != "QUEUED" or context.is_cancelled():
                if current and current["status"] == "QUEUED":
                    self.db.request_agent_run_cancel(
                        job.run_id, context.abort_reason() or "cancelled_before_start"
                    )
                return AgentRunOutcome(
                    job.task_id, job.run_id, RunStatus.CANCELLED,
                    context.abort_reason() or "cancelled_before_start",
                )

            self.db.start_agent_run(job.run_id, job.task_id)
            self._set_deadline(context, job.spec)
            context.raise_if_aborted()
            spec = job.spec
            request = dict(job.request)
            if job.prepare:
                prepared = job.prepare(context)
                context.raise_if_aborted()
                if prepared is None:
                    result = ConversationResult(
                        final_response="",
                        reasoning="",
                        messages=[],
                        completion_reason="completed",
                    )
                else:
                    request.update(prepared)
                    if prepared.get("system_prompt"):
                        spec = replace(spec, system_prompt=prepared["system_prompt"])
            if result is None:
                agent = self._ephemeral_factory(spec, request, context)
                self._attach_provider_limiter(agent)
                with self._live_lock:
                    live = self._live_runs.get(job.run_id)
                    if live:
                        live.agent = agent
                context.raise_if_aborted()
                result = agent.run_conversation(
                    user_message=request.get("user_message") or request.get("task") or "",
                    history=request.get("history", []),
                    renderer=None,
                    session_id=None,
                    run_context=context,
                )

            abort_reason = context.abort_reason()
            if abort_reason == "deadline_exceeded":
                terminal = RunStatus.TIMED_OUT
            elif abort_reason:
                terminal = RunStatus.CANCELLED
            elif result.completion_reason in ("stop", "completed"):
                terminal = RunStatus.SUCCEEDED
            else:
                terminal = RunStatus.FAILED
            reason = abort_reason or result.completion_reason
            if terminal in (RunStatus.CANCELLED, RunStatus.TIMED_OUT):
                self._request_cancel_if_running(job.run_id, reason)
            error_code = None if terminal == RunStatus.SUCCEEDED else reason
            self.db.finish_agent_run(
                run_id=job.run_id,
                task_id=job.task_id,
                status=terminal.value,
                completion_reason=reason,
                end_session_id=context.start_session_id or None,
                result_preview=sanitize_preview(result.final_response),
                error_code=error_code,
                error_message=error_code,
                iterations_used=result.iterations_used,
                provider_attempts=context.provider_attempts,
                prompt_tokens=context.prompt_tokens,
                completion_tokens=context.completion_tokens,
                reasoning_tokens=context.reasoning_tokens,
            )
            return AgentRunOutcome(
                job.task_id, job.run_id, terminal, reason, result,
                error_code=error_code, error_message=error_code,
            )
        except AgentRunAborted as exc:
            result = exc.partial_result
            reason = context.abort_reason()
            if reason:
                terminal = (
                    RunStatus.TIMED_OUT
                    if reason == "deadline_exceeded"
                    else RunStatus.CANCELLED
                )
                self._request_cancel_if_running(job.run_id, reason)
            else:
                terminal = RunStatus.FAILED
                reason = exc.completion_reason
            self.db.finish_agent_run(
                run_id=job.run_id, task_id=job.task_id,
                status=terminal.value, completion_reason=reason,
                end_session_id=context.start_session_id or None,
                result_preview=sanitize_preview(result.final_response),
                error_code=reason,
                error_message=sanitize_preview(exc.safe_message),
                iterations_used=result.iterations_used,
                provider_attempts=context.provider_attempts,
                prompt_tokens=context.prompt_tokens,
                completion_tokens=context.completion_tokens,
                reasoning_tokens=context.reasoning_tokens,
            )
            return AgentRunOutcome(
                job.task_id, job.run_id, terminal, reason, result,
                error_code=reason, error_message=sanitize_preview(exc.safe_message),
            )
        except AgentRunControlError as exc:
            terminal = (
                RunStatus.TIMED_OUT
                if exc.completion_reason == "deadline_exceeded"
                else RunStatus.CANCELLED
            )
            self._request_cancel_if_running(job.run_id, exc.completion_reason)
            current = self.db.get_agent_run(job.run_id)
            if current and current["status"] == "CANCEL_REQUESTED":
                self.db.finish_agent_run(
                    run_id=job.run_id, task_id=job.task_id,
                    status=terminal.value,
                    completion_reason=exc.completion_reason,
                    end_session_id=context.start_session_id or None,
                    error_code=exc.error_code,
                    error_message=sanitize_preview(exc.safe_message),
                    provider_attempts=context.provider_attempts,
                    prompt_tokens=context.prompt_tokens,
                    completion_tokens=context.completion_tokens,
                    reasoning_tokens=context.reasoning_tokens,
                )
            return AgentRunOutcome(
                job.task_id, job.run_id, terminal, exc.completion_reason,
                result, exc.error_code, sanitize_preview(exc.safe_message),
            )
        except Exception as exc:
            safe_error = sanitize_preview(f"{type(exc).__name__}: {exc}")
            current = self.db.get_agent_run(job.run_id)
            if current and current["status"] in ("RUNNING", "CANCEL_REQUESTED"):
                self.db.finish_agent_run(
                    run_id=job.run_id, task_id=job.task_id,
                    status="FAILED", completion_reason="internal_error",
                    end_session_id=context.start_session_id or None,
                    error_code="internal_error", error_message=safe_error,
                    provider_attempts=context.provider_attempts,
                    prompt_tokens=context.prompt_tokens,
                    completion_tokens=context.completion_tokens,
                    reasoning_tokens=context.reasoning_tokens,
                )
            return AgentRunOutcome(
                job.task_id, job.run_id, RunStatus.FAILED, "internal_error",
                result, "internal_error", safe_error,
            )
        finally:
            if acquired_gate:
                self._execution_gate.release()
            with self._live_lock:
                self._live_runs.pop(job.run_id, None)

    def get_run(self, run_id: str) -> dict | None:
        return self.db.get_agent_run(run_id)

    def get_task(self, task_id: str) -> dict | None:
        return self.db.get_agent_task(task_id)

    def list_events(self, run_id: str) -> list[dict]:
        return self.db.list_agent_events(run_id)

    def list_tool_executions(self, run_id: str) -> list[dict]:
        return self.db.list_tool_executions(run_id)

    def list_tool_retry_attempts(self, tool_execution_id: str) -> list[dict]:
        return self.db.list_tool_retry_attempts(tool_execution_id)

    def get_recovery(self, recovery_id: str) -> dict | None:
        return self.db.get_failure_recovery(recovery_id)

    def find_recoveries_by_prefix(
        self, recovery_id_prefix: str, limit: int = 3
    ) -> list[dict]:
        return self.db.find_failure_recoveries_by_prefix(
            recovery_id_prefix, limit=limit
        )

    def list_recoveries(
        self, run_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        return self.db.list_failure_recoveries(run_id=run_id, limit=limit)

    def get_execution_record(self, record_id: str) -> dict | None:
        return self.db.get_execution_record(record_id)

    def get_execution_record_for_tool_execution(
        self, tool_execution_id: str
    ) -> dict | None:
        return self.db.get_execution_record_for_tool_execution(
            tool_execution_id
        )

    def list_execution_records(self, run_id: str) -> list[dict]:
        return self.db.list_execution_records(run_id)

    def find_execution_records_by_prefix(self, record_id_prefix: str) -> list[dict]:
        return self.db.find_execution_records_by_prefix(record_id_prefix)

    def get_workspace_snapshot(self, snapshot_id: str) -> dict | None:
        return self.db.get_workspace_snapshot(snapshot_id)

    def list_workspace_snapshots(self, run_id: str) -> list[dict]:
        return self.db.list_workspace_snapshots(run_id)

    def get_worktree(self, workspace_id: str) -> dict | None:
        return self.db.get_worktree_lease(workspace_id)

    def get_worktree_for_run(self, run_id: str) -> dict | None:
        return self.db.get_worktree_lease_for_run(run_id)

    def list_worktrees(
        self, *, parent_run_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        return self.db.list_worktree_leases(
            parent_run_id=parent_run_id, limit=limit
        )

    def inspect_worktree(self, workspace_id: str) -> dict:
        lease = self.db.get_worktree_lease(workspace_id)
        if not lease:
            raise KeyError(f"unknown worktree lease: {workspace_id}")
        result = dict(lease)
        if Path(lease["worktree_path"]).is_dir():
            audit = self._workspace_manager.inspect_candidate(lease)
            result["current_changes"] = list(audit.changes)
            result["current_violations"] = list(audit.violations)
        else:
            result["current_changes"] = []
            result["current_violations"] = ["worktree directory is unavailable"]
        result["latest_integration"] = self.db.get_latest_worktree_integration(
            workspace_id
        )
        return result

    def get_worktree_integration(self, integration_id: str) -> dict | None:
        return self.db.get_worktree_integration(integration_id)

    def list_worktree_integrations(
        self, *, workspace_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        return self.db.list_worktree_integrations(
            workspace_id=workspace_id, limit=limit
        )

    def integrate_worktree(self, workspace_id: str) -> dict:
        """显式验证并集成一个 PRESERVED Worktree 候选。"""
        command = self._worktree_config["integration_verification_command"]
        if not command:
            raise WorkspaceIntegrationError(
                "integration_verification_command_required",
                "agent_runtime.worktree.integration_verification_command is empty",
            )
        if self._approval_callback is None:
            raise WorkspaceIntegrationError(
                "integration_approval_unavailable",
                "explicit Worktree integration requires an interactive approval callback",
            )
        if self._evidence_recorder is None:
            raise WorkspaceIntegrationError("integration_evidence_unavailable")
        if self._workspace_runner is None:
            raise WorkspaceIntegrationError(
                self._workspace_runner_error or "strict_runner_unavailable"
            )
        lease = self.db.get_worktree_lease(workspace_id)
        if not lease:
            raise KeyError(f"unknown worktree lease: {workspace_id}")
        latest = self.db.get_latest_worktree_integration(workspace_id)
        if lease["lease_status"] == "MERGED":
            if lease["cleanup_status"] != "SUCCEEDED":
                try:
                    self._workspace_manager.cleanup_merged_candidate(
                        db=self.db,
                        runner=self._workspace_runner,
                        workspace_id=workspace_id,
                    )
                except Exception:
                    pass
            result = dict(latest or {})
            result["lease"] = self.db.get_worktree_lease(workspace_id)
            return result
        if lease["lease_status"] != "PRESERVED":
            raise WorkspaceIntegrationError("worktree_not_integration_ready")
        source_run = self.db.get_agent_run(lease["run_id"])
        if not source_run or source_run["status"] != "SUCCEEDED":
            raise WorkspaceIntegrationError("source_run_not_succeeded")

        with self._worktree_lifecycle_gate:
            # gate 等待期间候选可能已被另一个 Runtime 处理，必须重新读取。
            lease = self.db.get_worktree_lease(workspace_id)
            if not lease or lease["lease_status"] != "PRESERVED":
                raise WorkspaceIntegrationError("worktree_not_integration_ready")
            inspection = self._workspace_manager.inspect_git_workspace(
                lease["git_root"]
            )
            if inspection.head_commit is None:
                raise WorkspaceIntegrationError(
                    inspection.primary_reason or "main_head_unavailable"
                )
            integration_id = uuid.uuid4().hex
            task = self.create_task(
                conversation_id=source_run.get("conversation_id"),
                session_id=source_run.get("end_session_id"),
                parent_task_id=lease["task_id"],
                kind="worktree_integration",
                request=f"Integrate Worktree candidate {workspace_id}",
                context=f"source run {lease['run_id']}",
            )
            integration_run_id = uuid.uuid4().hex
            timeout = self._default_timeout("worktree_integration")
            self.db.create_agent_run(
                run_id=integration_run_id,
                task_id=task["task_id"],
                parent_run_id=lease["run_id"],
                conversation_id=source_run.get("conversation_id"),
                start_session_id=source_run.get("end_session_id"),
                agent_kind="worktree_integration",
                model="",
                tool_policy_json="{}",
                approval_mode=ApprovalMode.INTERACTIVE.value,
                max_iterations=0,
                timeout_seconds=timeout,
            )
            self.db.start_agent_run(integration_run_id, task["task_id"])
            try:
                record = self.db.start_worktree_integration(
                    integration_id=integration_id,
                    workspace_id=workspace_id,
                    integration_run_id=integration_run_id,
                    source_main_commit=inspection.head_commit,
                    verification_command_hash=hashlib.sha256(
                        command.encode("utf-8")
                    ).hexdigest(),
                )
            except Exception as exc:
                self.db.finish_agent_run(
                    run_id=integration_run_id,
                    task_id=task["task_id"],
                    status="FAILED",
                    completion_reason="integration_start_failed",
                    end_session_id=source_run.get("end_session_id"),
                    error_code=getattr(exc, "reason_code", "integration_start_failed"),
                    error_message=sanitize_preview(str(exc)),
                )
                raise

            integration_run_context = AgentRunContext(
                task_id=task["task_id"],
                run_id=integration_run_id,
                conversation_id=source_run.get("conversation_id") or "",
                start_session_id=source_run.get("end_session_id") or "",
                event_callback=lambda event_type, payload: self.db.append_agent_event(
                    task["task_id"], integration_run_id, event_type, payload
                ),
            )
            if timeout is not None:
                integration_run_context.deadline_monotonic = time.monotonic() + timeout
            with self._live_lock:
                self._live_runs[integration_run_id] = LiveRunHandle(
                    run_id=integration_run_id,
                    conversation_id=source_run.get("conversation_id") or "",
                    agent=None,
                    owns_agent=True,
                    cancel_event=integration_run_context.cancel_event,
                    started_monotonic=time.monotonic(),
                    deadline_monotonic=integration_run_context.deadline_monotonic,
                    run_context=integration_run_context,
                )
            integration_workspace: IntegrationWorkspace | None = None
            verification_record_id: str | None = None
            try:
                integration_run_context.raise_if_aborted()
                if not inspection.eligible:
                    failure = inspection.failures[0]
                    return self._finish_worktree_integration(
                        record,
                        status="PRECONDITION_FAILED",
                        failure_code=failure.reason_code,
                        failure_message=failure.message,
                    )
                self._workspace_manager.assert_git_identity(lease["git_root"])
                if not self._approve_worktree_integration(
                    record,
                    stage="candidate_commit",
                    description=(
                        "Create an immutable candidate commit for this Worktree "
                        "without moving its branch"
                    ),
                    run_context=integration_run_context,
                ):
                    return self._finish_worktree_integration(
                        record,
                        status="DENIED",
                        failure_code="candidate_commit_denied",
                        failure_message="User denied candidate commit preparation",
                    )
                integration_run_context.raise_if_aborted()
                candidate = self._workspace_manager.prepare_candidate_commit(
                    db=self.db,
                    artifact_store=self._evidence_recorder.store,
                    workspace_id=workspace_id,
                    integration_id=integration_id,
                )
                record = self.db.transition_worktree_integration(
                    integration_id,
                    status="VERIFYING",
                    candidate_commit=candidate["candidate_commit"],
                    candidate_tree_hash=candidate["candidate_tree_hash"],
                )
                integration_workspace = (
                    self._workspace_manager.prepare_integration_workspace(
                        lease=lease,
                        integration_id=integration_id,
                        candidate_commit=candidate["candidate_commit"],
                        source_main_commit=record["source_main_commit"],
                        runner=self._workspace_runner,
                    )
                )
                verification = self._run_integration_verification(
                    record=record,
                    workspace=integration_workspace,
                    command=command,
                    run_context=integration_run_context,
                )
                verification_record_id = verification["record_id"]
                if not verification["success"]:
                    cleanup_error = self._cleanup_integration_workspace(
                        record, integration_workspace
                    )
                    if cleanup_error:
                        return self._finish_worktree_integration(
                            record,
                            status="FAILED",
                            failure_code="integration_temp_cleanup_failed",
                            failure_message=cleanup_error,
                            verification_record_id=verification_record_id,
                        )
                    return self._finish_worktree_integration(
                        record,
                        status=(
                            "CANCELLED"
                            if verification["error_code"] in {
                                "cancelled", "deadline_exceeded", "user_interrupt"
                            }
                            else "VERIFICATION_FAILED"
                        ),
                        failure_code=verification["error_code"],
                        failure_message=verification["message"],
                        verification_record_id=verification_record_id,
                        expected_merge_tree_hash=(
                            integration_workspace.expected_merge_tree_hash
                        ),
                    )
                self._workspace_manager.validate_integration_verification(
                    integration_workspace
                )
                record = self.db.transition_worktree_integration(
                    integration_id,
                    status="READY_TO_APPLY",
                    expected_merge_tree_hash=(
                        integration_workspace.expected_merge_tree_hash
                    ),
                    verification_record_id=verification_record_id,
                )
                cleanup_error = self._cleanup_integration_workspace(
                    record, integration_workspace
                )
                if cleanup_error:
                    return self._finish_worktree_integration(
                        record,
                        status="FAILED",
                        failure_code="integration_temp_cleanup_failed",
                        failure_message=cleanup_error,
                    )
                if not self._approve_worktree_integration(
                    record,
                    stage="final_apply",
                    description=(
                        "Apply the verified candidate tree to the current main branch "
                        "and create a local merge commit"
                    ),
                    run_context=integration_run_context,
                ):
                    return self._finish_worktree_integration(
                        record,
                        status="DENIED",
                        failure_code="final_apply_denied",
                        failure_message="User denied final main-branch update",
                    )
                integration_run_context.raise_if_aborted()
                final_commit, final_tree = (
                    self._workspace_manager.apply_integration_to_main(
                        workspace=integration_workspace,
                        runner=self._workspace_runner,
                    )
                )
                completed = self._finish_worktree_integration(
                    record,
                    status="MERGED",
                    final_merge_commit=final_commit,
                    final_merge_tree_hash=final_tree,
                    verification_record_id=verification_record_id,
                )
                try:
                    lease = self._workspace_manager.cleanup_merged_candidate(
                        db=self.db,
                        runner=self._workspace_runner,
                        workspace_id=workspace_id,
                    )
                except Exception:
                    lease = self.db.get_worktree_lease(workspace_id)
                completed["lease"] = lease
                return completed
            except AgentRunControlError as exc:
                if integration_workspace is not None:
                    self._cleanup_integration_workspace(record, integration_workspace)
                return self._finish_worktree_integration(
                    record,
                    status="CANCELLED",
                    failure_code=exc.error_code,
                    failure_message=exc.safe_message,
                    verification_record_id=verification_record_id,
                )
            except WorkspaceOperationError as exc:
                if integration_workspace is not None:
                    cleanup_error = self._cleanup_integration_workspace(
                        record, integration_workspace
                    )
                    if cleanup_error and exc.reason_code != "integration_temp_cleanup_failed":
                        return self._finish_worktree_integration(
                            record,
                            status="FAILED",
                            failure_code="integration_temp_cleanup_failed",
                            failure_message=cleanup_error,
                            verification_record_id=verification_record_id,
                        )
                elif getattr(exc, "temp_cleanup_succeeded", False):
                    self.db.set_worktree_integration_cleanup_status(
                        record["integration_id"], cleanup_status="SUCCEEDED"
                    )
                reason_code = getattr(exc, "reason_code", "integration_failed")
                conflicts = tuple(getattr(exc, "conflicts", ()))
                status = (
                    "CONFLICT"
                    if reason_code in {
                        "integration_conflict", "final_merge_conflict"
                    }
                    else "VERIFICATION_FAILED"
                    if reason_code.startswith("verification_")
                    else "FAILED"
                    if reason_code in {
                        "candidate_index_restore_failed", "merge_abort_failed",
                        "integration_path_identity_mismatch",
                        "integration_temp_identity_mismatch",
                        "integration_approval_failed",
                    }
                    else "PRECONDITION_FAILED"
                )
                return self._finish_worktree_integration(
                    record,
                    status=status,
                    failure_code=reason_code,
                    failure_message=str(exc),
                    verification_record_id=verification_record_id,
                    expected_merge_tree_hash=(
                        integration_workspace.expected_merge_tree_hash
                        if integration_workspace is not None else None
                    ),
                    details={"conflicts": list(conflicts)},
                )
            finally:
                with self._live_lock:
                    self._live_runs.pop(integration_run_id, None)

    def _approve_worktree_integration(
        self,
        record: dict,
        *,
        stage: str,
        description: str,
        run_context: AgentRunContext,
    ) -> bool:
        try:
            resolution = self._approval_engine.resolve(
                ApprovalResult(
                    action="confirm",
                    pattern_key=f"worktree_integration:{stage}",
                    description=description,
                ),
                "worktree_integration",
                {
                    "path": record["workspace_id"],
                    "stage": stage,
                    "integration_id": record["integration_id"],
                },
                mode=ApprovalMode.INTERACTIVE,
                approval_callback=self._approval_callback,
                conversation_id=(
                    self.db.get_agent_run(record["integration_run_id"]) or {}
                ).get("conversation_id") or "",
                run_context=run_context,
            )
        except AgentRunControlError:
            raise
        except Exception as exc:
            raise WorkspaceIntegrationError(
                "integration_approval_failed", str(exc)
            ) from exc
        return resolution.allowed

    def _run_integration_verification(
        self,
        *,
        record: dict,
        workspace: IntegrationWorkspace,
        command: str,
        run_context: AgentRunContext,
    ) -> dict:
        execution_id = uuid.uuid4().hex
        self.db.create_tool_execution(
            execution_id=execution_id,
            run_id=record["integration_run_id"],
            tool_call_id=f"integration-verification-{record['integration_id']}",
            tool_name="bash",
        )
        capture = None
        try:
            capture = self._evidence_recorder.start_bash(
                run_id=record["integration_run_id"],
                tool_execution_id=execution_id,
                command=command,
                working_directory=workspace.workspace_root,
                workspace_id=workspace.workspace_id,
            )
            remaining = run_context.remaining_seconds()
            timeout = min(
                self._default_timeout("worktree_integration") or 300.0,
                remaining if remaining is not None else 300.0,
            )
            try:
                result = self._workspace_runner.run(
                    workspace_id=workspace.workspace_id,
                    workspace_root=workspace.workspace_root,
                    task_temp_root=workspace.task_temp_root,
                    command=command,
                    cwd_relative=".",
                    timeout=timeout,
                    cancel_check=run_context.is_cancelled,
                )
            except Exception as exc:
                result = WorkspaceCommandResult(
                    stderr=sanitize_preview(str(exc)),
                    termination_reason="spawn_error",
                    error_code=getattr(exc, "reason_code", "runner_failed"),
                )
            capture.complete(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                termination_reason=result.termination_reason,
            )
            abort_reason = run_context.abort_reason()
            success = (
                result.exit_code == 0
                and result.termination_reason == "exited"
                and abort_reason is None
            )
            error_code = (
                abort_reason if abort_reason in {"deadline_exceeded", "user_interrupt"}
                else result.error_code
            ) or (
                None if success else
                "integration_verification_timed_out"
                if result.termination_reason == "timed_out" else
                "cancelled"
                if result.termination_reason == "cancelled" else
                "integration_verification_failed"
            )
            self.db.finish_tool_execution(
                execution_id=execution_id,
                status="SUCCEEDED" if success else (
                    "CANCELLED"
                    if error_code in {"cancelled", "deadline_exceeded", "user_interrupt"}
                    else "FAILED"
                ),
                attempts=1,
                retryable=False,
                error_code=error_code,
                error_message=(None if success else sanitize_preview(result.stderr)),
                output_preview=sanitize_preview(result.stdout),
            )
            return {
                "success": success,
                "error_code": error_code,
                "message": sanitize_preview(result.stderr or result.stdout),
                "record_id": capture.record_id,
            }
        except Exception as exc:
            if capture is not None and not capture.completed:
                capture.mark_unavailable("integration_evidence_failed")
            current = self.db.get_tool_execution(execution_id)
            if current and current["status"] == "RUNNING":
                self.db.finish_tool_execution(
                    execution_id=execution_id,
                    status="FAILED",
                    attempts=1,
                    retryable=False,
                    error_code="integration_evidence_failed",
                    error_message=sanitize_preview(str(exc)),
                    output_preview="",
                )
            raise WorkspaceIntegrationError(
                "integration_evidence_failed", str(exc)
            ) from exc

    def _cleanup_integration_workspace(
        self, record: dict, workspace: IntegrationWorkspace
    ) -> str | None:
        current = self.db.get_worktree_integration(record["integration_id"])
        if current and current["temp_cleanup_status"] == "SUCCEEDED":
            return None
        try:
            self._workspace_manager.cleanup_integration_workspace(
                workspace=workspace, runner=self._workspace_runner
            )
            self.db.set_worktree_integration_cleanup_status(
                record["integration_id"], cleanup_status="SUCCEEDED"
            )
            return None
        except Exception as exc:
            message = sanitize_preview(f"{type(exc).__name__}: {exc}")
            try:
                self.db.set_worktree_integration_cleanup_status(
                    record["integration_id"],
                    cleanup_status="FAILED",
                    failure_message=message,
                )
            except Exception:
                pass
            return message

    def _finish_worktree_integration(
        self,
        record: dict,
        *,
        status: str,
        failure_code: str | None = None,
        failure_message: str | None = None,
        verification_record_id: str | None = None,
        expected_merge_tree_hash: str | None = None,
        final_merge_commit: str | None = None,
        final_merge_tree_hash: str | None = None,
        details: dict | None = None,
    ) -> dict:
        artifact_relpath = (
            f"{record['integration_run_id']}/integrations/"
            f"{record['integration_id']}/result.json"
        )
        payload = {
            "integration_id": record["integration_id"],
            "workspace_id": record["workspace_id"],
            "integration_run_id": record["integration_run_id"],
            "status": status,
            "source_main_commit": record["source_main_commit"],
            "candidate_commit": record.get("candidate_commit"),
            "candidate_tree_hash": record.get("candidate_tree_hash"),
            "expected_merge_tree_hash": (
                expected_merge_tree_hash or record.get("expected_merge_tree_hash")
            ),
            "final_merge_commit": final_merge_commit,
            "final_merge_tree_hash": final_merge_tree_hash,
            "verification_record_id": verification_record_id,
            "verification_command_hash": record["verification_command_hash"],
            "failure_code": failure_code,
            "failure_message": sanitize_preview(failure_message or ""),
            "details": details or {},
            "recorded_at": time.time(),
        }
        artifact_hash = None
        try:
            self._evidence_recorder.store.write_json_atomic(
                artifact_relpath, payload
            )
            artifact_hash = hashlib.sha256(
                self._evidence_recorder.store.read_bytes(artifact_relpath)
            ).hexdigest()
        except Exception:
            if status == "MERGED":
                raise
            artifact_relpath = None
        completed = self.db.finish_worktree_integration(
            record["integration_id"],
            status=status,
            final_merge_commit=final_merge_commit,
            final_merge_tree_hash=final_merge_tree_hash,
            expected_merge_tree_hash=expected_merge_tree_hash,
            verification_record_id=verification_record_id,
            failure_code=failure_code,
            failure_message=failure_message,
            result_artifact_relpath=artifact_relpath,
            result_artifact_hash=artifact_hash,
        )
        run = self.db.get_agent_run(record["integration_run_id"])
        if run and run["status"] in {"RUNNING", "CANCEL_REQUESTED"}:
            run_status = "SUCCEEDED" if status == "MERGED" else (
                "TIMED_OUT" if failure_code == "deadline_exceeded" else
                "CANCELLED" if status in {"DENIED", "CANCELLED"} else "FAILED"
            )
            if run_status in {"CANCELLED", "TIMED_OUT"} and run["status"] == "RUNNING":
                self.db.request_agent_run_cancel(
                    record["integration_run_id"], failure_code or status.lower()
                )
            self.db.finish_agent_run(
                run_id=record["integration_run_id"],
                task_id=run["task_id"],
                status=run_status,
                completion_reason=status.lower(),
                end_session_id=run.get("end_session_id"),
                result_preview=(
                    f"Worktree integration {record['integration_id']}: {status}"
                ),
                error_code=None if status == "MERGED" else failure_code,
                error_message=None if status == "MERGED" else sanitize_preview(
                    failure_message or ""
                ),
            )
        completed["lease"] = self.db.get_worktree_lease(record["workspace_id"])
        return completed

    def discard_worktree(self, workspace_id: str) -> dict:
        lease = self.db.get_worktree_lease(workspace_id)
        if not lease:
            raise KeyError(f"unknown worktree lease: {workspace_id}")
        controller = RecoveryController(
            self.db,
            event_callback=lambda event_type, payload: self.db.append_agent_event(
                lease["task_id"], lease["run_id"], event_type, payload
            ),
        )
        with self._worktree_lifecycle_gate:
            return controller.discard_worktree(
                workspace_manager=self._workspace_manager,
                runner=self._workspace_runner,
                artifact_store=getattr(self._evidence_recorder, "store", None),
                workspace_id=workspace_id,
            )

    def inspect_execution_artifact_retention(self) -> dict:
        """返回只读的制品保留概览；没有证据存储时不伪造结果。"""
        manager, options = self._artifact_retention_manager()
        return manager.inspect(
            retention_days=options["retention_days"],
            keep_failed_days=options["keep_failed_days"],
        )

    def cleanup_execution_artifacts(self) -> dict:
        """显式清理到期制品；所有状态判断和删除都留在受限管理器内。"""
        manager, options = self._artifact_retention_manager()
        return manager.cleanup(
            retention_days=options["retention_days"],
            keep_failed_days=options["keep_failed_days"],
            max_total_artifact_bytes=options["max_total_artifact_bytes"],
        )

    def _artifact_retention_manager(self) -> tuple[ArtifactRetentionManager, dict]:
        if self._evidence_recorder is None:
            raise RuntimeError("execution evidence is not available")
        options = cfg.get_reproducibility_config()
        return ArtifactRetentionManager(self._evidence_recorder.store, self.db), options

    def replay_execution(self, record_id: str, *, conversation_id: str | None = None) -> AgentRunOutcome:
        """在系统管理的临时副本重放一条历史 bash 记录，不调用模型。"""
        if self._evidence_recorder is None:
            raise RuntimeError("execution evidence is not available")
        materializer = ReplayMaterializer(
            self._evidence_recorder.store, self.db, replay_root=self._replay_root
        )
        source_record = self.db.get_execution_record(record_id)
        if source_record is None:
            raise ReplaySetupError("record_not_found")
        original_run = self.db.get_agent_run(source_record["run_id"])
        if original_run is None:
            self._mark_source_replay_unavailable(record_id, "source_run_not_found")
            raise ReplaySetupError("source_run_not_found")
        effective_conversation_id = conversation_id or original_run.get("conversation_id") or ""
        task = self.create_task(
            conversation_id=effective_conversation_id,
            session_id=None,
            kind="replay",
            request=f"Replay bash record {record_id}",
            parent_task_id=original_run["task_id"],
        )
        run_id = uuid.uuid4().hex
        run = self.db.create_agent_run(
            run_id=run_id,
            task_id=task["task_id"],
            parent_run_id=original_run["run_id"],
            conversation_id=effective_conversation_id,
            start_session_id=None,
            agent_kind="replay",
            model="",
            tool_policy_json=json.dumps({"include": ["bash"]}),
            approval_mode=ApprovalMode.INTERACTIVE.value,
            max_iterations=0,
            timeout_seconds=self._default_timeout("replay"),
        )
        run_context = AgentRunContext(
            task_id=task["task_id"],
            run_id=run["run_id"],
            conversation_id=effective_conversation_id,
            start_session_id="",
            tool_policy=resolve_tool_access_policy(
                {"include": {"bash"}}, {"bash"}
            ),
            event_callback=lambda event_type, payload: self.db.append_agent_event(
                task["task_id"], run_id, event_type, payload
            ),
        )
        self._set_deadline(
            run_context,
            AgentSpec(kind="replay", timeout_seconds=self._default_timeout("replay")),
        )
        live = LiveRunHandle(
            run_id=run_id,
            conversation_id=effective_conversation_id,
            agent=None,
            owns_agent=False,
            cancel_event=run_context.cancel_event,
            started_monotonic=time.monotonic(),
            deadline_monotonic=run_context.deadline_monotonic,
            run_context=run_context,
        )
        with self._live_lock:
            self._live_runs[run_id] = live

        capture = None
        execution_id = uuid.uuid4().hex
        try:
            with self._execution_gate:
                try:
                    self.db.start_agent_run(run_id, task["task_id"])
                except RuntimeError:
                    current = self.db.get_agent_run(run_id)
                    if current and current["status"] == "CANCELLED":
                        self.db.update_execution_replay_status(
                            source_record["record_id"], "REPLAY_CANCELLED"
                        )
                        return AgentRunOutcome(
                            task_id=task["task_id"],
                            run_id=run_id,
                            status=RunStatus.CANCELLED,
                            completion_reason="user_interrupt",
                            error_code="user_interrupt",
                        )
                    raise
                self.db.create_tool_execution(
                    execution_id=execution_id,
                    run_id=run_id,
                    tool_call_id=f"replay-{source_record['record_id']}",
                    tool_name="bash",
                )
                run_context.raise_if_aborted()
                try:
                    source = materializer.load_source(source_record["record_id"])
                except ReplaySetupError as exc:
                    self._record_unavailable_replay_attempt(
                        run_id=run_id,
                        tool_execution_id=execution_id,
                        source_record_id=source_record["record_id"],
                        reason=exc.reason_code,
                    )
                    self.db.update_execution_replay_status(
                        source_record["record_id"], "REPLAY_UNAVAILABLE"
                    )
                    return self._finish_replay_run(
                        run_context, task["task_id"], RunStatus.FAILED,
                        "replay_unavailable", exc.reason_code, "replay_unavailable",
                    )
                try:
                    materialization = materializer.materialize(source)
                except ReplaySetupError as exc:
                    capture = self._create_replay_preflight_capture(
                        run_id=run_id,
                        tool_execution_id=execution_id,
                        source=source,
                    )
                    self._finish_replay_without_shell(
                        run_context, task["task_id"], source.record["record_id"],
                        capture, execution_id, "REPLAY_SETUP_FAILED", exc.reason_code,
                    )
                    return AgentRunOutcome(
                        task_id=task["task_id"],
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        completion_reason="replay_setup_failed",
                        error_code="replay_setup_failed",
                        error_message=exc.reason_code,
                    )

                capture = self._evidence_recorder.prepare_replay_bash(
                    run_id=run_id,
                    tool_execution_id=execution_id,
                    command=source.command,
                    working_directory=materialization.working_directory,
                    snapshot_id=source.snapshot["snapshot_id"],
                    working_directory_rel=source.working_directory_rel,
                    replayed_from_record_id=source.record["record_id"],
                )
                import tools as tool_registry
                tool_call = {
                    "id": f"replay-{source.record['record_id']}",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"command": source.command}),
                    },
                }
                result = tool_registry.get_tool_manager().execute_detailed(
                    tool_call,
                    ToolExecutionContext(
                        policy=run_context.tool_policy,
                        # 重放永远使用新的审批状态，不能复用源会话的“本会话允许”。
                        approval_engine=ApprovalEngine(),
                        approval_mode=ApprovalMode.INTERACTIVE,
                        approval_callback=self._approval_callback,
                        run_context=run_context,
                        db=self.db,
                        cancel_check=run_context.is_cancelled,
                        working_directory=str(materialization.working_directory),
                        evidence_recorder=self._evidence_recorder,
                        tool_execution_id=execution_id,
                        precreated_evidence_capture=capture,
                    ),
                )
                if result.status == ToolStatus.DENIED:
                    capture.finish_without_execution(
                        replay_status="REPLAY_DENIED", reason=result.error_code or "approval_denied"
                    )
                    self.db.update_execution_replay_status(
                        source.record["record_id"], "REPLAY_DENIED"
                    )
                    return self._finish_replay_run(
                        run_context, task["task_id"], RunStatus.CANCELLED,
                        "replay_denied", result.model_output, "approval_denied",
                    )
                if result.status == ToolStatus.CANCELLED:
                    abort_reason = result.error_code or "user_interrupt"
                    replay_status = (
                        "REPLAY_TIMED_OUT"
                        if abort_reason == "deadline_exceeded"
                        else "REPLAY_CANCELLED"
                    )
                    capture.finish_without_execution(
                        replay_status=replay_status, reason=abort_reason
                    )
                    self.db.update_execution_replay_status(
                        source.record["record_id"], replay_status
                    )
                    return self._finish_replay_run(
                        run_context, task["task_id"],
                        RunStatus.TIMED_OUT if abort_reason == "deadline_exceeded" else RunStatus.CANCELLED,
                        abort_reason, result.model_output, abort_reason,
                    )
                replay_record = self.db.get_execution_record(capture.record_id)
                replay_status = replay_record.get("replay_status", "REPLAY_COMMAND_FAILED")
                self.db.update_execution_replay_status(source.record["record_id"], replay_status)
                terminal = {
                    "REPLAY_SUCCEEDED": RunStatus.SUCCEEDED,
                    "REPLAY_CANCELLED": RunStatus.CANCELLED,
                    "REPLAY_TIMED_OUT": RunStatus.TIMED_OUT,
                }.get(replay_status, RunStatus.FAILED)
                return self._finish_replay_run(
                    run_context, task["task_id"], terminal,
                    "replay_succeeded" if terminal == RunStatus.SUCCEEDED else (
                        "deadline_exceeded" if terminal == RunStatus.TIMED_OUT else (
                            "user_interrupt" if terminal == RunStatus.CANCELLED else "replay_command_failed"
                        )
                    ),
                    result.model_output,
                    None if terminal == RunStatus.SUCCEEDED else (
                        "deadline_exceeded" if terminal == RunStatus.TIMED_OUT else (
                            "user_interrupt" if terminal == RunStatus.CANCELLED else "replay_command_failed"
                        )
                    ),
                )
        except AgentRunControlError as exc:
            replay_status = (
                "REPLAY_TIMED_OUT"
                if exc.completion_reason == "deadline_exceeded"
                else "REPLAY_CANCELLED"
            )
            if capture is not None and not capture.completed:
                capture.finish_without_execution(
                    replay_status=replay_status, reason=exc.completion_reason
                )
            tool_execution = self.db.get_tool_execution(execution_id)
            if tool_execution and tool_execution["status"] == "RUNNING":
                if capture is None:
                    self._record_preexecution_replay_attempt(
                        run_id=run_id,
                        tool_execution_id=execution_id,
                        source_record_id=source_record["record_id"],
                        replay_status=replay_status,
                        reason=exc.completion_reason,
                    )
                else:
                    self.db.finish_tool_execution(
                        execution_id=execution_id,
                        status="CANCELLED",
                        attempts=0,
                        retryable=False,
                        error_code=exc.completion_reason,
                        error_message=sanitize_preview(exc.safe_message),
                        output_preview="",
                    )
            self.db.update_execution_replay_status(source_record["record_id"], replay_status)
            current = self.db.get_agent_run(run_id)
            if current and current["status"] == "RUNNING":
                self.db.request_agent_run_cancel(run_id, exc.completion_reason)
            return self._finish_replay_run(
                run_context, task["task_id"],
                RunStatus.TIMED_OUT if exc.completion_reason == "deadline_exceeded" else RunStatus.CANCELLED,
                exc.completion_reason, exc.safe_message, exc.error_code,
            )
        except Exception as exc:
            safe_error = sanitize_preview(f"{type(exc).__name__}: {exc}")
            if capture is not None and not capture.completed:
                capture.finish_without_execution(
                    replay_status="REPLAY_SETUP_FAILED", reason="replay_internal_error"
                )
            tool_execution = self.db.get_tool_execution(execution_id)
            if tool_execution and tool_execution["status"] == "RUNNING":
                self.db.finish_tool_execution(
                    execution_id=execution_id,
                    status="FAILED",
                    attempts=0,
                    retryable=False,
                    error_code="replay_internal_error",
                    error_message=safe_error,
                    output_preview="",
                )
            self.db.update_execution_replay_status(
                source_record["record_id"], "REPLAY_SETUP_FAILED"
            )
            current = self.db.get_agent_run(run_id)
            if current and current["status"] in ("RUNNING", "CANCEL_REQUESTED"):
                if current["status"] == "CANCEL_REQUESTED":
                    self.db.finish_agent_run(
                        run_id=run_id, task_id=task["task_id"], status="CANCELLED",
                        completion_reason="user_interrupt", end_session_id=None,
                        error_code="user_interrupt", error_message=safe_error,
                    )
                else:
                    self.db.finish_agent_run(
                        run_id=run_id, task_id=task["task_id"], status="FAILED",
                        completion_reason="replay_internal_error", end_session_id=None,
                        error_code="replay_internal_error", error_message=safe_error,
                    )
            return AgentRunOutcome(
                task["task_id"], run_id, RunStatus.FAILED, "replay_internal_error",
                error_code="replay_internal_error", error_message=safe_error,
            )
        finally:
            with self._live_lock:
                self._live_runs.pop(run_id, None)

    def _mark_source_replay_unavailable(self, record_id: str, reason: str) -> None:
        try:
            self.db.update_execution_replay_status(record_id, "REPLAY_UNAVAILABLE")
        except KeyError:
            pass

    def _record_unavailable_replay_attempt(
        self,
        *,
        run_id: str,
        tool_execution_id: str,
        source_record_id: str,
        reason: str,
    ) -> None:
        """源制品预检失败时，仍留下关联到 ToolExecution 的终态审计记录。"""
        self._record_preexecution_replay_attempt(
            run_id=run_id,
            tool_execution_id=tool_execution_id,
            source_record_id=source_record_id,
            replay_status="REPLAY_UNAVAILABLE",
            reason=reason,
        )

    def _record_preexecution_replay_attempt(
        self,
        *,
        run_id: str,
        tool_execution_id: str,
        source_record_id: str,
        replay_status: str,
        reason: str,
    ) -> None:
        """命令尚未启动时也关闭 ToolExecution 与 ExecutionRecord。"""
        record_id = uuid.uuid4().hex
        self.db.create_execution_record(
            record_id=record_id,
            run_id=run_id,
            tool_execution_id=tool_execution_id,
            tool_name="bash",
            command_preview="replay unavailable",
            replayed_from_record_id=source_record_id,
        )
        self.db.finish_execution_record(
            record_id=record_id,
            log_status="UNAVAILABLE",
            reproducibility_status="UNAVAILABLE",
            artifact_status="INCOMPLETE",
            termination_reason=sanitize_preview(reason),
            replay_status=replay_status,
        )
        self.db.finish_tool_execution(
            execution_id=tool_execution_id,
            status="CANCELLED" if replay_status in {
                "REPLAY_CANCELLED", "REPLAY_TIMED_OUT"
            } else "FAILED",
            attempts=0,
            retryable=False,
            error_code=(
                reason if replay_status in {"REPLAY_CANCELLED", "REPLAY_TIMED_OUT"}
                else "replay_unavailable"
            ),
            error_message=sanitize_preview(reason),
            output_preview="",
        )

    def _finish_replay_without_shell(
        self, run_context: AgentRunContext, task_id: str, source_record_id: str,
        capture, execution_id: str, replay_status: str, reason: str,
    ) -> None:
        if capture is not None:
            capture.finish_without_execution(replay_status=replay_status, reason=reason)
        tool_execution = self.db.get_tool_execution(execution_id)
        if tool_execution and tool_execution["status"] == "RUNNING":
            self.db.finish_tool_execution(
                execution_id=execution_id,
                status="FAILED",
                attempts=0,
                retryable=False,
                error_code="replay_setup_failed",
                error_message=sanitize_preview(reason),
                output_preview="",
            )
        self.db.update_execution_replay_status(source_record_id, replay_status)
        self._finish_replay_run(
            run_context, task_id, RunStatus.FAILED, "replay_setup_failed", reason,
            "replay_setup_failed",
        )

    def _create_replay_preflight_capture(self, *, run_id: str, tool_execution_id: str, source):
        """材料化失败时仍可用源快照信息创建一条未执行的重放记录。"""
        try:
            return self._evidence_recorder.prepare_replay_bash(
                run_id=run_id,
                tool_execution_id=tool_execution_id,
                command=source.command,
                working_directory=source.snapshot["git_root"],
                snapshot_id=source.snapshot["snapshot_id"],
                working_directory_rel=source.working_directory_rel,
                replayed_from_record_id=source.record["record_id"],
            )
        except Exception:
            return None

    def _finish_replay_run(
        self, run_context: AgentRunContext, task_id: str, status: RunStatus,
        completion_reason: str, result_preview: str, error_code: str | None,
    ) -> AgentRunOutcome:
        if status in (RunStatus.CANCELLED, RunStatus.TIMED_OUT):
            self._request_cancel_if_running(run_context.run_id, completion_reason)
        self.db.finish_agent_run(
            run_id=run_context.run_id,
            task_id=task_id,
            status=status.value,
            completion_reason=completion_reason,
            end_session_id=None,
            result_preview=sanitize_preview(result_preview),
            error_code=error_code,
            error_message=sanitize_preview(result_preview) if error_code else None,
        )
        return AgentRunOutcome(
            task_id, run_context.run_id, status, completion_reason,
            error_code=error_code,
            error_message=sanitize_preview(result_preview) if error_code else None,
        )

    def interrupt_current(self, conversation_id: str,
                          reason: str = "user_interrupt") -> str | None:
        """兼容旧接口：取消当前逻辑会话的主 Run、子 Run 和后台 Run。"""
        with self._live_lock:
            lives = [
                item for item in self._live_runs.values()
                if item.conversation_id == conversation_id
            ]
            if not lives:
                return None
            # 主 Agent 借用 SessionAgentHandle，owns_agent 为 False；优先返回
            # 它的状态，保持旧调用方对 interrupt_current() 的预期。
            preferred = next(
                (item for item in lives if not item.owns_agent), lives[0]
            )
            run_ids = [item.run_id for item in lives]
        statuses = {
            run_id: self.cancel(run_id, reason=reason)
            for run_id in run_ids
        }
        return statuses[preferred.run_id]

    def cancel(self, run_id: str, reason: str = "user_interrupt") -> str:
        """请求取消单个 Run；不获取全局执行门。"""
        with self._live_lock:
            live = self._live_runs.get(run_id)
            if live is not None:
                if live.run_context is not None:
                    live.run_context.cancel_reason = reason
                live.cancel_event.set()
                if live.agent is not None:
                    live.agent.interrupt()
        current = self.db.get_agent_run(run_id)
        if current is None:
            raise KeyError(f"unknown agent run: {run_id}")
        if current["status"] in {
            "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED",
        }:
            return current["status"]
        result = self.db.request_agent_run_cancel(run_id, reason)
        return result

    def list_runs(self, conversation_id: str | None = None,
                  limit: int = 20) -> list[dict]:
        return self.db.list_agent_runs(conversation_id, limit)

    def reconcile_interrupted_runs(self) -> dict[str, int]:
        return self.db.reconcile_agent_runs()

    def shutdown(self):
        self._background_stop.set()
        with self._background_condition:
            self._background_condition.notify_all()
        with self._live_lock:
            run_ids = list(self._live_runs)
        for run_id in run_ids:
            try:
                self.cancel(run_id, reason="runtime_shutdown")
            except (KeyError, RuntimeError):
                pass
        for conversation_id in list(self._sessions):
            self.close_session(conversation_id)

        # 不再启动排队的 Delegate；已运行的子 Agent 已在上面的 live run
        # 取消流程中收到协作式停止信号。
        self._delegate_executor.shutdown(wait=False, cancel_futures=True)

        self._background_sequence += 1
        self._background_queue.put((100, self._background_sequence, None))
        grace = float(self._runtime_config.get("cancel_grace_seconds", 3.0))
        self._background_thread.join(timeout=grace)

        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            with self._live_lock:
                if not self._live_runs:
                    return True
            time.sleep(0.05)
        return False

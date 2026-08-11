"""串行多 Agent Runtime：统一登记主 Agent Task/Run 与会话句柄。"""

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
from typing import Callable, Optional

from agent.agent import Agent, AgentRunAborted, ConversationResult
from approval import ApprovalMode
from provider import ProviderCallLimiter
from session import SessionDB
from tools.registry import (
    ToolAccessPolicy,
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
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
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
                 runtime_config: dict | None = None):
        self.db = db
        self._agent_factory = agent_factory
        self._ephemeral_factory = ephemeral_factory
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
        try:
            self._max_delegate_concurrency = min(
                max(int(self._runtime_config.get("max_concurrency", 1)), 1), 16
            )
        except (TypeError, ValueError):
            self._max_delegate_concurrency = 1
        self._delegate_executor = ThreadPoolExecutor(
            max_workers=self._max_delegate_concurrency,
            thread_name_prefix="minihermes-delegate",
        )
        self._provider_limiter = ProviderCallLimiter(
            self._max_delegate_concurrency
        )
        self.db.reconcile_agent_runs()
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
                self.db.finish_agent_run(
                    run_id=run_id, task_id=task["task_id"],
                    status=status.value, completion_reason=exc.completion_reason,
                    end_session_id=handle.current_session_id,
                    error_code=exc.error_code,
                    error_message=exc.safe_message,
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
                self.db.finish_agent_run(
                    run_id=run_id,
                    task_id=task["task_id"],
                    status="FAILED",
                    completion_reason="internal_error",
                    end_session_id=handle.current_session_id,
                    error_code="internal_error",
                    error_message=safe_error,
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
                        status=RunStatus.CANCELLED,
                        completion_reason=reason,
                        error_code=reason,
                    )
                self.db.start_agent_run(run_id, task["task_id"])
                self._set_deadline(run_context, spec)
                run_context.raise_if_aborted()
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
                elif result.completion_reason in ("stop", "completed"):
                    terminal = RunStatus.SUCCEEDED
                    completion_reason = result.completion_reason
                    error_code = None
                else:
                    terminal = RunStatus.FAILED
                    completion_reason = result.completion_reason
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
            if current and current["status"] == "RUNNING":
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
        except Exception as exc:
            safe_error = sanitize_preview(f"{type(exc).__name__}: {exc}")
            current = self.db.get_agent_run(run_id)
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
            with self._live_lock:
                self._live_runs.pop(run_id, None)

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
        return is_parallel_safe_delegate_policy(resolved)

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
        self.db.finish_agent_run(
            run_id=run_context.run_id,
            task_id=task_id,
            status=status.value,
            completion_reason=completion_reason,
            end_session_id=handle.current_session_id,
            result_preview=sanitize_preview(result.final_response if result else ""),
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

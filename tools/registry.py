"""工具注册、冻结权限、结构化执行和元数据。"""

from __future__ import annotations

import copy
import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping, Optional


JsonScalar = str | int | float | bool | None


class ToolStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class ToolExecutionResult:
    status: ToolStatus
    output: str
    model_output: str
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    attempts: int = 0
    duration_seconds: float = 0.0
    side_effects_possible: bool = False
    execution_id: str | None = None


@dataclass(frozen=True)
class ResolvedToolMetadata:
    side_effect: str
    approval: str
    retry: str
    concurrency_key: str | None = None
    idempotency: str = "unknown"


@dataclass(frozen=True)
class ToolMetadata:
    side_effect: str | Callable[[dict], str] = "unknown"
    approval: str | Callable[[dict], str] = "policy"
    retry: str | Callable[[dict], str] = "never"
    concurrency_key: str | Callable[[dict], str | None] | None = None
    idempotency: str | Callable[[dict], str] = "unknown"

    def resolve(self, args: dict) -> ResolvedToolMetadata:
        def value(item):
            return item(args) if callable(item) else item

        return ResolvedToolMetadata(
            side_effect=value(self.side_effect),
            approval=value(self.approval),
            retry=value(self.retry),
            concurrency_key=value(self.concurrency_key),
            idempotency=value(self.idempotency),
        )


def _freeze_argument_allow(value) -> Mapping[str, Mapping[str, frozenset]]:
    normalized = {}
    for tool_name, fields in dict(value or {}).items():
        normalized[tool_name] = MappingProxyType({
            field_name: frozenset(allowed_values)
            for field_name, allowed_values in dict(fields or {}).items()
        })
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class ToolAccessPolicy:
    policy_id: str
    include: frozenset[str] | None
    exclude: frozenset[str] = field(default_factory=frozenset)
    argument_allow: Mapping[str, Mapping[str, frozenset]] = field(default_factory=dict)
    parent_policy_id: str | None = None
    effective_tools: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        object.__setattr__(
            self,
            "include",
            None if self.include is None else frozenset(self.include),
        )
        object.__setattr__(self, "exclude", frozenset(self.exclude))
        object.__setattr__(
            self, "argument_allow", _freeze_argument_allow(self.argument_allow)
        )
        object.__setattr__(self, "effective_tools", frozenset(self.effective_tools))

    def allows(self, tool_name: str, args: dict) -> tuple[bool, str | None]:
        if tool_name not in self.effective_tools:
            return False, f"tool '{tool_name}' is not allowed by policy {self.policy_id}"
        for field_name, allowed_values in self.argument_allow.get(tool_name, {}).items():
            if args.get(field_name) not in allowed_values:
                return (
                    False,
                    f"argument '{tool_name}.{field_name}' value is not allowed",
                )
        return True, None

    def to_record(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "include": None if self.include is None else sorted(self.include),
            "exclude": sorted(self.exclude),
            "argument_allow": {
                tool_name: {
                    field_name: sorted(values, key=lambda item: str(item))
                    for field_name, values in fields.items()
                }
                for tool_name, fields in self.argument_allow.items()
            },
            "parent_policy_id": self.parent_policy_id,
            "effective_tools": sorted(self.effective_tools),
        }

    def __getitem__(self, key: str):
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)


PLAN_TOOL_LIMIT = frozenset({
    "read_file", "list_dir", "web_search", "web_extract",
    "session_search", "process", "memory", "clarify", "todo", "skill_view",
})
DELEGATE_BLOCKED_TOOLS = frozenset({"delegate_task", "clarify"})

# 只允许确定为本地只读、无 UI、无共享外部 client 的工具并行执行。
# 新工具默认不在此集合中，必须在验证线程安全和副作用模型后才可加入。
PARALLEL_SAFE_DELEGATE_TOOLS = frozenset({
    "read_file", "list_dir", "search_files", "process",
    "session_search", "skill_view",
})


def is_parallel_safe_delegate_policy(policy: ToolAccessPolicy) -> bool:
    """仅当 Delegate 的全部有效权限均为明确批准的只读工具时返回 True。"""
    return policy.effective_tools <= PARALLEL_SAFE_DELEGATE_TOOLS

_KIND_LIMITS = {
    "delegate": {
        "exclude": DELEGATE_BLOCKED_TOOLS,
    },
    "plan": {
        "include": PLAN_TOOL_LIMIT,
        "argument_allow": {"memory": {"action": {"view"}}},
    },
    "memory_nudge": {
        "include": {"memory"},
        "argument_allow": {
            "memory": {"action": {"view", "add", "update"}},
        },
    },
    "skill_nudge": {
        "include": {"skill_view", "read_file", "skill_manage"},
        "argument_allow": {"skill_manage": {"action": {"list"}}},
    },
    "curator": {
        "include": {"skill_view", "read_file", "skill_manage"},
        "argument_allow": {"skill_manage": {"action": {"list"}}},
    },
}


def _normalize_policy_input(policy) -> dict:
    if policy is None:
        return {}
    if isinstance(policy, ToolAccessPolicy):
        return policy.to_record()
    return dict(policy)


def _merge_argument_allow(*sources) -> dict:
    merged: dict[str, dict[str, frozenset]] = {}
    for source in sources:
        for tool_name, fields in dict(source or {}).items():
            target = merged.setdefault(tool_name, {})
            for field_name, values in dict(fields or {}).items():
                values = frozenset(values)
                if field_name in target:
                    target[field_name] = target[field_name] & values
                else:
                    target[field_name] = values
    return merged


def resolve_tool_access_policy(
    policy,
    registered_tools: set[str] | frozenset[str],
    *,
    kind: str = "main_turn",
    parent_policy: ToolAccessPolicy | None = None,
) -> ToolAccessPolicy:
    """计算并冻结一次 Run 的最终工具权限。"""
    if isinstance(policy, ToolAccessPolicy):
        if parent_policy is None:
            return policy
        policy = {
            "include": set(policy.effective_tools),
            "exclude": set(policy.exclude),
            "argument_allow": policy.argument_allow,
        }

    raw = _normalize_policy_input(policy)
    registered = frozenset(registered_tools)
    include = raw.get("include")
    include = None if include is None else frozenset(include)
    exclude = frozenset(raw.get("exclude") or ())

    effective = registered if include is None else registered & include
    effective -= exclude

    kind_limit = _KIND_LIMITS.get(kind, {})
    kind_include = kind_limit.get("include")
    if kind_include is not None:
        effective &= frozenset(kind_include)
    kind_exclude = frozenset(kind_limit.get("exclude") or ())
    effective -= kind_exclude

    if parent_policy is not None:
        effective &= parent_policy.effective_tools

    argument_allow = _merge_argument_allow(
        parent_policy.argument_allow if parent_policy else {},
        kind_limit.get("argument_allow"),
        raw.get("argument_allow"),
    )
    return ToolAccessPolicy(
        policy_id=raw.get("policy_id") or uuid.uuid4().hex,
        include=include,
        exclude=exclude | kind_exclude,
        argument_allow=argument_allow,
        parent_policy_id=parent_policy.policy_id if parent_policy else None,
        effective_tools=effective,
    )


def _memory_side_effect(args: dict) -> str:
    return "none" if args.get("action") == "view" else "local"


def _skill_side_effect(args: dict) -> str:
    return "none" if args.get("action") == "list" else "local"


_DEFAULT_METADATA = {
    "bash": ToolMetadata(side_effect="unknown", retry="never"),
    "read_file": ToolMetadata(
        side_effect="none", retry="never", idempotency="idempotent"
    ),
    "write_file": ToolMetadata(side_effect="local", retry="never"),
    "list_dir": ToolMetadata(
        side_effect="none", retry="never", idempotency="idempotent"
    ),
    "web_search": ToolMetadata(
        side_effect="none", retry="transient", idempotency="idempotent"
    ),
    "web_extract": ToolMetadata(
        side_effect="none", retry="transient", idempotency="idempotent"
    ),
    "web_open": ToolMetadata(side_effect="external", retry="never"),
    "execute_code": ToolMetadata(side_effect="external", retry="never"),
    "process": ToolMetadata(side_effect="none", retry="never"),
    "memory": ToolMetadata(side_effect=_memory_side_effect, retry="never"),
    "session_search": ToolMetadata(side_effect="none", retry="never"),
    "skill_view": ToolMetadata(side_effect="none", retry="never"),
    "skill_manage": ToolMetadata(side_effect=_skill_side_effect, retry="never"),
    "todo": ToolMetadata(side_effect="local", retry="never"),
    "clarify": ToolMetadata(side_effect="external", retry="never"),
    "delegate_task": ToolMetadata(side_effect="local", retry="never"),
    "generate_image": ToolMetadata(side_effect="external", retry="never"),
}


@dataclass
class ToolExecutionContext:
    policy: ToolAccessPolicy | None = None
    approval_engine: object | None = None
    approval_mode: object | None = None
    approval_callback: object | None = None
    run_context: object | None = None
    db: object | None = None
    special_executor: Optional[Callable[[str, dict, dict], str]] = None
    special_tool_names: frozenset[str] = field(default_factory=frozenset)
    cancel_check: Optional[Callable[[], bool]] = None
    # 下列字段只在运行时内部传递，永不进入模型可见 schema。
    working_directory: str | None = None
    evidence_recorder: object | None = None
    tool_execution_id: str | None = None
    # Runtime 重放会在审批前预先创建审计记录；普通模型工具调用永远为 None。
    precreated_evidence_capture: object | None = None
    workspace_context: object | None = None
    resolved_metadata: ResolvedToolMetadata | None = None


class _DeferredEvidenceCapture:
    """延后到 retry 执行器确认调用工具时，才创建 bash 证据。"""

    def __init__(self, failure_reporter):
        self.capture = None
        self._failure_reporter = failure_reporter

    @property
    def completed(self) -> bool:
        return bool(self.capture and self.capture.completed)

    def set_capture(self, capture) -> None:
        self.capture = capture

    def complete(self, **kwargs) -> None:
        if self.capture is not None:
            self.capture.complete(**kwargs)

    def mark_unavailable(self, reason: str) -> None:
        if self.capture is not None:
            self.capture.mark_unavailable(reason)

    def report_failure(self, stage: str, error: Exception) -> None:
        self._failure_reporter(stage, error)


_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*[^\s,;]+"),
)


def _sanitize_preview(value: str, limit: int = 500) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text.replace("\x00", "")[:limit]


class ToolRegistry:
    """工具注册、Schema 过滤与结构化执行。"""

    def __init__(self):
        self._registry: dict[str, dict] = {}

    def register(self, schema: dict, metadata: ToolMetadata | None = None):
        def decorator(fn):
            name = schema["function"]["name"]
            self._registry[name] = {
                "fn": fn,
                "schema": schema,
                "metadata": metadata or _DEFAULT_METADATA.get(name, ToolMetadata()),
            }
            return fn

        return decorator

    def get_schemas(
        self,
        include=None,
        exclude=None,
        *,
        policy: ToolAccessPolicy | None = None,
    ) -> list[dict]:
        if policy is None:
            policy = resolve_tool_access_policy(
                {"include": include, "exclude": exclude or ()},
                self.get_names(),
            )

        schemas = []
        for name, entry in self._registry.items():
            if name not in policy.effective_tools:
                continue
            schema = copy.deepcopy(entry["schema"])
            properties = (
                schema.get("function", {})
                .get("parameters", {})
                .get("properties", {})
            )
            for field_name, allowed_values in policy.argument_allow.get(name, {}).items():
                if field_name in properties:
                    properties[field_name]["enum"] = sorted(
                        allowed_values, key=lambda item: str(item)
                    )
            if name == "delegate_task" and "tools" in properties:
                grantable = policy.effective_tools - DELEGATE_BLOCKED_TOOLS
                properties["tools"].setdefault("items", {})["enum"] = sorted(grantable)
            schemas.append(schema)
        return schemas

    def get_metadata(self, name: str) -> ToolMetadata:
        entry = self._registry.get(name)
        return entry["metadata"] if entry else ToolMetadata()

    def execute_detailed(
        self,
        tool_call: dict,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolExecutionResult:
        from tools.retry import execute_with_retry_detailed

        context = execution_context or ToolExecutionContext()
        started = time.monotonic()
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        run_context = context.run_context
        run_id = getattr(run_context, "run_id", None)
        tool_call_id = tool_call.get("id", "")
        execution_id = context.tool_execution_id or uuid.uuid4().hex
        context = replace(context, tool_execution_id=execution_id)

        if context.db and run_id and execution_context is not None and execution_context.tool_execution_id:
            # 非 LLM Runtime（例如 /replay）已在审批前登记 ToolExecution，
            # 这里仅复用同一 ID，避免把拒绝记录丢在执行链外。
            pass
        elif context.db and run_id:
            context.db.create_tool_execution(
                execution_id=execution_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool_name=name or "unknown",
            )
        self._emit(context, "tool_started", {
            "execution_id": execution_id,
            "tool_call_id": tool_call_id,
            "tool_name": name or "unknown",
        })

        if name not in self._registry:
            result = ToolExecutionResult(
                ToolStatus.FAILED,
                f"Error: tool '{name}' is not registered.",
                f"FAILED: tool '{name}' is not registered.",
                "tool_not_found",
                f"tool '{name}' is not registered",
                duration_seconds=time.monotonic() - started,
            )
            return self._finish(context, execution_id, result)

        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            if not isinstance(args, dict):
                raise TypeError("tool arguments must be a JSON object")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            output = f"ERROR: tool arguments are malformed or invalid JSON ({exc})."
            result = ToolExecutionResult(
                ToolStatus.FAILED,
                output,
                output,
                "invalid_arguments",
                str(exc),
                duration_seconds=time.monotonic() - started,
            )
            return self._finish(context, execution_id, result)

        policy = context.policy or resolve_tool_access_policy(None, self.get_names())
        allowed, denial_reason = policy.allows(name, args)
        if not allowed:
            output = (
                f"DENIED by tool policy: {denial_reason}. Operation was not executed."
            )
            result = ToolExecutionResult(
                ToolStatus.DENIED,
                output,
                output,
                "tool_not_allowed",
                denial_reason,
                duration_seconds=time.monotonic() - started,
            )
            return self._finish(context, execution_id, result)

        if context.cancel_check and context.cancel_check():
            reason = getattr(run_context, "abort_reason", lambda: None)() or "user_interrupt"
            prefix = "TIMED_OUT" if reason == "deadline_exceeded" else "CANCELLED"
            output = f"{prefix}: Agent run stopped before the tool started."
            result = ToolExecutionResult(
                ToolStatus.CANCELLED,
                output,
                output,
                reason,
                output,
                duration_seconds=time.monotonic() - started,
            )
            return self._finish(context, execution_id, result)

        if context.workspace_context is not None:
            failure_code = getattr(context.workspace_context, "failure_code", None)
            if failure_code:
                output = (
                    "Error: Worktree execution stopped after an earlier workspace "
                    f"failure ({failure_code})."
                )
                return self._finish(
                    context,
                    execution_id,
                    ToolExecutionResult(
                        ToolStatus.FAILED,
                        output,
                        output,
                        failure_code,
                        getattr(context.workspace_context, "failure_message", output),
                        duration_seconds=time.monotonic() - started,
                    ),
                )

        if context.approval_engine is not None:
            check_result = context.approval_engine.check(
                name,
                args,
                conversation_id=getattr(run_context, "conversation_id", ""),
            )
            try:
                resolution = context.approval_engine.resolve(
                    check_result,
                    tool_name=name,
                    args=args,
                    mode=context.approval_mode,
                    approval_callback=context.approval_callback,
                    conversation_id=getattr(run_context, "conversation_id", ""),
                    run_context=run_context,
                )
            except Exception as exc:
                if not getattr(exc, "is_run_control", False):
                    raise
                reason = getattr(exc, "completion_reason", "user_interrupt")
                prefix = "TIMED_OUT" if reason == "deadline_exceeded" else "CANCELLED"
                output = f"{prefix}: approval wait ended before '{name}' started."
                return self._finish(
                    context,
                    execution_id,
                    ToolExecutionResult(
                        ToolStatus.CANCELLED,
                        output,
                        output,
                        reason,
                        output,
                        duration_seconds=time.monotonic() - started,
                    ),
                )
            if not resolution.allowed:
                result = ToolExecutionResult(
                    ToolStatus.DENIED,
                    resolution.model_output,
                    resolution.model_output,
                    resolution.error_code,
                    resolution.description,
                    duration_seconds=time.monotonic() - started,
                )
                return self._finish(context, execution_id, result)

        metadata = self.get_metadata(name).resolve(args)
        context = replace(context, resolved_metadata=metadata)
        if context.special_executor and name in context.special_tool_names:
            def fn(**unused):
                return context.special_executor(name, tool_call, args)
        else:
            fn = self._registry[name]["fn"]

        attempt_ids: dict[int, str] = {}

        def _attempt_audit_failed(stage: str, attempt: int, exc: Exception):
            self._emit(context, "tool_attempt_audit_failed", {
                "execution_id": execution_id,
                "tool_name": name,
                "attempt": attempt,
                "stage": stage,
                "error_type": type(exc).__name__,
            })

        def on_attempt_started(attempt: int):
            if not (
                context.db
                and run_id
                and hasattr(context.db, "start_tool_retry_attempt")
            ):
                return
            attempt_id = uuid.uuid4().hex
            try:
                context.db.start_tool_retry_attempt(
                    attempt_id=attempt_id,
                    tool_execution_id=execution_id,
                    attempt_number=attempt,
                )
            except Exception as exc:
                _attempt_audit_failed("start", attempt, exc)
                return
            attempt_ids[attempt] = attempt_id
            self._emit(context, "tool_attempt_started", {
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "attempt": attempt,
            })

        def on_attempt_finished(attempt: int, attempt_result, duration: float):
            attempt_id = attempt_ids.get(attempt)
            if not attempt_id:
                return
            try:
                context.db.finish_tool_retry_attempt(
                    attempt_id=attempt_id,
                    status=attempt_result.status,
                    retryable=attempt_result.retryable,
                    error_code=attempt_result.error_code,
                    error_message=attempt_result.error_message,
                    output_preview=attempt_result.output,
                    duration_seconds=duration,
                )
            except Exception as exc:
                _attempt_audit_failed("finish", attempt, exc)
                return
            self._emit(context, "tool_attempt_finished", {
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "attempt": attempt,
                "status": attempt_result.status,
                "error_code": attempt_result.error_code,
                "duration_seconds": round(duration, 6),
            })

        def on_retry(next_attempt: int, error_code: str, delay: float):
            source_attempt = next_attempt - 1
            attempt_id = attempt_ids.get(source_attempt)
            if attempt_id:
                try:
                    context.db.schedule_tool_retry_wait(
                        attempt_id=attempt_id,
                        retry_delay_seconds=delay,
                    )
                except Exception as exc:
                    _attempt_audit_failed("schedule_wait", source_attempt, exc)
            payload = {
                "execution_id": execution_id,
                "tool_name": name,
                "attempt": next_attempt,
                "error_code": error_code,
                "delay_seconds": delay,
            }
            self._emit(context, "tool_retry_scheduled", payload)
            self._emit(context, "tool_retrying", {
                **payload,
            })

        def on_retry_wait_finished(
            next_attempt: int, wait_status: str, duration: float
        ):
            source_attempt = next_attempt - 1
            attempt_id = attempt_ids.get(source_attempt)
            if attempt_id:
                try:
                    context.db.finish_tool_retry_wait(
                        attempt_id=attempt_id,
                        status=wait_status,
                    )
                except Exception as exc:
                    _attempt_audit_failed("finish_wait", source_attempt, exc)
            self._emit(context, "tool_retry_wait_finished", {
                "execution_id": execution_id,
                "attempt_id": attempt_id,
                "next_attempt": next_attempt,
                "status": wait_status,
                "duration_seconds": round(duration, 6),
            })

        invoke_args = dict(args)
        evidence_capture = context.precreated_evidence_capture
        if (
            name == "bash"
            and evidence_capture is None
            and context.evidence_recorder is not None
            and run_id
        ):
            evidence_capture = _DeferredEvidenceCapture(
                lambda stage, exc: self._emit(
                    context,
                    "evidence_capture_failed",
                    {
                        "execution_id": execution_id,
                        "tool_name": name,
                        "stage": stage,
                        "error_type": type(exc).__name__,
                    },
                )
            )
        remaining = (
            run_context.remaining_seconds()
            if run_context and hasattr(run_context, "remaining_seconds")
            else None
        )
        if remaining is not None:
            if name == "bash":
                configured = invoke_args.get("timeout", 30)
                try:
                    configured = float(configured)
                except (TypeError, ValueError):
                    configured = 30.0
                invoke_args["timeout"] = max(0.01, min(configured, remaining))
            elif name == "execute_code":
                configured = invoke_args.get("timeout", remaining)
                try:
                    invoke_args["timeout"] = max(0.01, min(float(configured), remaining))
                except (TypeError, ValueError):
                    invoke_args["timeout"] = max(0.01, remaining)
            elif "timeout" in invoke_args:
                try:
                    invoke_args["timeout"] = max(0.01, min(float(invoke_args["timeout"]), remaining))
                except (TypeError, ValueError):
                    pass

        if name in {"bash", "web_extract", "generate_image"}:
            try:
                parameters = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "_cancel_check" in parameters:
                invoke_args["_cancel_check"] = context.cancel_check
            if "_timeout" in parameters and remaining is not None:
                invoke_args["_timeout"] = max(0.01, remaining)
            if name == "bash":
                if "_evidence_capture" in parameters:
                    invoke_args["_evidence_capture"] = evidence_capture
                if "_working_directory" in parameters:
                    invoke_args["_working_directory"] = context.working_directory

        if context.workspace_context is not None:
            try:
                parameters = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "_workspace_context" in parameters:
                invoke_args["_workspace_context"] = context.workspace_context

        if name == "bash":
            try:
                inspect.signature(fn).bind(**invoke_args)
            except TypeError as exc:
                output = f"ERROR: tool arguments are invalid ({exc})."
                return self._finish(
                    context,
                    execution_id,
                    ToolExecutionResult(
                        ToolStatus.FAILED,
                        output,
                        output,
                        "invalid_arguments",
                        str(exc),
                        duration_seconds=time.monotonic() - started,
                    ),
                )

        def prepare_attempt(attempt: int) -> None:
            current_remaining = (
                run_context.remaining_seconds()
                if run_context and hasattr(run_context, "remaining_seconds")
                else None
            )
            if current_remaining is not None and "_timeout" in invoke_args:
                invoke_args["_timeout"] = max(0.01, current_remaining)
            if evidence_capture is None or attempt != 1:
                return
            if context.precreated_evidence_capture is not None:
                return
            try:
                evidence_capture.set_capture(context.evidence_recorder.start_bash(
                    run_id=run_id,
                    tool_execution_id=execution_id,
                    command=str(invoke_args.get("command", "")),
                    working_directory=context.working_directory or ".",
                    node_run_id=getattr(run_context, "node_run_id", None),
                    workspace_id=getattr(
                        context.workspace_context, "workspace_id", None
                    ),
                    failure_reporter=evidence_capture.report_failure,
                ))
            except Exception as exc:
                evidence_capture.report_failure("start", exc)

        def retry_guard(next_attempt: int) -> str | None:
            if context.cancel_check and context.cancel_check():
                return (
                    getattr(run_context, "abort_reason", lambda: None)()
                    or "user_interrupt"
                )
            if run_context and hasattr(run_context, "remaining_seconds"):
                remaining_seconds = run_context.remaining_seconds()
                if remaining_seconds is not None and remaining_seconds <= 0:
                    return "deadline_exceeded"
            allowed_now, _ = policy.allows(name, args)
            if not allowed_now:
                return "tool_not_allowed"
            if context.db and run_id and hasattr(context.db, "get_agent_run"):
                current_run = context.db.get_agent_run(run_id)
                if not current_run or current_run.get("status") != "RUNNING":
                    if current_run and current_run.get("status") == "CANCEL_REQUESTED":
                        return "user_interrupt"
                    return "runtime_shutdown"
            return None

        try:
            outcome = execute_with_retry_detailed(
                fn,
                invoke_args,
                name,
                retry_policy=metadata.retry,
                side_effect=metadata.side_effect,
                idempotency=metadata.idempotency,
                cancel_check=context.cancel_check,
                before_attempt=prepare_attempt,
                on_attempt_started=on_attempt_started,
                on_attempt_finished=on_attempt_finished,
                on_retry=on_retry,
                on_retry_wait_finished=on_retry_wait_finished,
                retry_guard=retry_guard,
            )
        except Exception as exc:
            if not getattr(exc, "is_run_control", False):
                raise
            reason = getattr(exc, "completion_reason", "user_interrupt")
            prefix = "TIMED_OUT" if reason == "deadline_exceeded" else "CANCELLED"
            output = f"{prefix}: Agent run stopped while waiting for tool '{name}'."
            return self._finish(
                context,
                execution_id,
                ToolExecutionResult(
                    ToolStatus.CANCELLED,
                    output,
                    output,
                    reason,
                    output,
                    attempts=0,
                    duration_seconds=time.monotonic() - started,
                    side_effects_possible=metadata.side_effect != "none",
                ),
            )

        if evidence_capture is not None and not getattr(evidence_capture, "completed", False):
            try:
                evidence_capture.mark_unavailable("adapter_did_not_finalize")
            except Exception as exc:
                self._emit(context, "evidence_capture_failed", {
                    "execution_id": execution_id,
                    "tool_name": name,
                    "stage": "finalize",
                    "error_type": type(exc).__name__,
                })

        if (
            outcome.status != "SUCCEEDED"
            and context.cancel_check
            and context.cancel_check()
            and outcome.status != "CANCELLED"
        ):
            reason = getattr(run_context, "abort_reason", lambda: None)() or "user_interrupt"
            prefix = "TIMED_OUT" if reason == "deadline_exceeded" else "CANCELLED"
            output = (
                f"{prefix}: tool '{name}' stopped; external side effects may have occurred."
            )
            outcome = type(outcome)(
                "CANCELLED", output, output, reason, output, False,
                outcome.attempts, outcome.duration_seconds, True,
            )
        elif outcome.status == "CANCELLED":
            reason = getattr(run_context, "abort_reason", lambda: None)() or "user_interrupt"
            prefix = "TIMED_OUT" if reason == "deadline_exceeded" else "CANCELLED"
            output = f"{prefix}: Agent run stopped during tool execution."
            outcome = type(outcome)(
                "CANCELLED", output, output, reason, output, False,
                outcome.attempts, outcome.duration_seconds,
                outcome.side_effects_possible,
            )
        result = ToolExecutionResult(
            ToolStatus(outcome.status),
            outcome.output,
            outcome.model_output,
            outcome.error_code,
            outcome.error_message,
            outcome.retryable,
            outcome.attempts,
            outcome.duration_seconds,
            outcome.side_effects_possible,
        )
        return self._finish(context, execution_id, result)

    def _emit(self, context: ToolExecutionContext, event_type: str, payload: dict):
        run_context = context.run_context
        if run_context and hasattr(run_context, "emit_event"):
            run_context.emit_event(event_type, payload)

    def _finish(
        self,
        context: ToolExecutionContext,
        execution_id: str,
        result: ToolExecutionResult,
    ) -> ToolExecutionResult:
        if context.db and getattr(context.run_context, "run_id", None):
            sanitize = _sanitize_preview
            recorder = context.evidence_recorder
            if recorder is not None and hasattr(recorder, "sanitize_preview"):
                sanitize = recorder.sanitize_preview
            context.db.finish_tool_execution(
                execution_id=execution_id,
                status=result.status.value,
                attempts=result.attempts,
                retryable=result.retryable,
                error_code=result.error_code,
                error_message=sanitize(result.error_message or "") or None,
                output_preview=sanitize(result.output),
            )
        self._emit(context, "tool_finished", {
            "execution_id": execution_id,
            "status": result.status.value,
            "error_code": result.error_code,
            "attempts": result.attempts,
        })
        if (
            context.db
            and getattr(context.run_context, "run_id", None)
            and hasattr(context.db, "create_initial_failure_recovery")
        ):
            try:
                from agent.recovery import RecoveryController

                controller = RecoveryController(
                    context.db,
                    event_callback=lambda event_type, payload: self._emit(
                        context, event_type, payload
                    ),
                )
                if result.status == ToolStatus.SUCCEEDED:
                    controller.record_tool_success(execution_id=execution_id)
                else:
                    controller.record_tool_failure(
                        execution_id=execution_id,
                        result=result,
                        metadata=context.resolved_metadata,
                        node_run_id=getattr(
                            context.run_context, "node_run_id", None
                        ),
                        workspace_id=getattr(
                            context.workspace_context, "workspace_id", None
                        ),
                    )
            except Exception as exc:
                self._emit(context, "recovery_audit_failed", {
                    "execution_id": execution_id,
                    "error_type": type(exc).__name__,
                })
        return replace(result, execution_id=execution_id)

    def execute(self, tool_call: dict) -> str:
        return self.execute_detailed(tool_call).model_output

    def get_names(self) -> set[str]:
        return set(self._registry.keys())

    def has(self, name: str) -> bool:
        return name in self._registry

    def reset(self):
        self._registry.clear()

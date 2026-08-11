"""
Agent 核心层：对话循环。

设计参考 hermes run_agent.py 的 run_conversation() + while 主循环：
  1. 构建完整 messages（system + history + user）
  2. while 循环驱动：LLM 调用 → 工具执行 → 继续 → 直到 stop 或预算耗尽
  3. IterationBudget 防止无限循环
  4. 上下文压缩：两处检查（调 LLM 前估算 + 响应后精确 usage）
  5. 返回更新后的对话历史，供下一轮使用
"""

import json
import os
from dataclasses import dataclass
from typing import Optional

import tools as tool_registry
from tools.memory import get_store as get_memory_store
from provider import Provider, StreamResult
from prompt import build_system_prompt
from renderer import StreamRenderer, print_budget_warning
from renderer.renderer import render_diff, _cprint, _DIM, _RST
from session import SessionDB
from context.compressor import ContextCompressor
from context import ConversationContext
import config as cfg
from approval import ApprovalEngine, ApprovalMode
from tools.registry import (
    ToolAccessPolicy,
    ToolExecutionContext,
    ToolStatus,
    resolve_tool_access_policy,
)


@dataclass
class ConversationResult:
    """run_conversation() 的返回值。"""
    final_response: str          # 最终文本回复
    reasoning: str               # 思考过程（可能为空）
    messages: list[dict]         # 更新后的完整对话历史（不含 system）
    session_id: str = ""         # 当前 session_id（压缩后可能改变）
    compressed: bool = False     # 本轮是否发生了压缩
    completion_reason: str = "completed"
    iterations_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0


class AgentRunAborted(Exception):
    """不可恢复的 Agent 失败，同时携带已闭合的部分对话历史。"""

    def __init__(self, error_code: str, safe_message: str,
                 partial_result: ConversationResult,
                 completion_reason: str = "internal_error"):
        super().__init__(safe_message)
        self.error_code = error_code
        self.safe_message = safe_message
        self.partial_result = partial_result
        self.completion_reason = completion_reason


class Agent:
    def __init__(
        self,
        provider: Provider,
        db: SessionDB = None,
        clarify_callback=None,
        approval_callback=None,
        auto_approve: bool | None = None,
        tool_filter: dict | None = None,
        tool_policy: ToolAccessPolicy | dict | None = None,
        agent_kind: str = "main_turn",
        approval_mode: ApprovalMode | str | None = None,
        approval_engine: ApprovalEngine | None = None,
        tool_db: SessionDB | None = None,
        system_prompt_override: str | None = None,
        max_iterations_override: int | None = None,
        runtime=None,
    ):
        self.provider = provider
        self.db = db
        self.clarify_callback = clarify_callback
        self.approval_callback = approval_callback
        self.auto_approve = auto_approve
        self.agent_kind = agent_kind
        raw_policy = tool_policy if tool_policy is not None else tool_filter
        self.tool_policy = resolve_tool_access_policy(
            raw_policy,
            tool_registry.get_tool_manager().get_names(),
            kind=agent_kind,
        )
        self.approval_mode = ApprovalMode.coerce(
            approval_mode,
            auto_approve=auto_approve,
        )
        self._approval = approval_engine or ApprovalEngine()
        self.tool_db = tool_db or db
        self.runtime = runtime

        # system prompt：支持外部覆盖（子 Agent 使用精简 prompt）
        if system_prompt_override is not None:
            self.system_prompt = system_prompt_override
        else:
            self.reload_system_prompt()

        # 设置最大迭代次数
        self.max_iterations = (
            max_iterations_override
            or cfg.get_model_config().get("max_iterations", 100)
        )
        # 上下文压缩器
        self._compressor = ContextCompressor(provider)
        # 对话状态容器（token 追踪、预算、压缩触发、进化计数器）
        tools_json = json.dumps(self._get_tool_schemas())
        self._ctx = ConversationContext(
            max_iterations=self.max_iterations,
            system_prompt=self.system_prompt,
            tools_schema_json=tools_json,
        )
        # 中断请求
        self._interrupt_requested = False
        self._active_run_context = None
        self._active_session_id = None
        self._delegate_batch_results: dict[str, str] | None = None

    def _get_tool_schemas(self) -> list[dict]:
        """返回经过过滤的工具 schema 列表。"""
        return tool_registry.get_schemas(policy=self.tool_policy)

    def interrupt(self):
        """外部请求中断当前对话循环。"""
        self._interrupt_requested = True

    # ── 公开 API（供 CLI 层调用）─────────────────────────────

    @property
    def last_prompt_tokens(self) -> int:
        """上次 LLM prompt 的真实 token 数（状态栏百分比用）。"""
        return self._ctx.last_prompt_tokens

    @property
    def budget_used(self) -> int:
        """当前轮已使用的 LLM 调用次数。"""
        return self._ctx.budget_used

    @property
    def iters_since_skill(self) -> int:
        """自上次技能使用以来的 LLM 迭代次数（进化系统用）。"""
        return self._ctx.iters_since_skill

    @iters_since_skill.setter
    def iters_since_skill(self, value: int):
        self._ctx.iters_since_skill = value

    @property
    def turns_since_memory(self) -> int:
        """自上次记忆使用以来的对话轮次（进化系统用）。"""
        return self._ctx.turns_since_memory

    @turns_since_memory.setter
    def turns_since_memory(self, value: int):
        self._ctx.turns_since_memory = value

    def request_compress(self):
        """设置强制压缩标志，下次 LLM 调用前触发压缩（/compress 命令）。"""
        self._ctx.force_compress = True

    def reset_token_tracking(self):
        """重置 token 追踪状态（/setup、/clear 后调用）。"""
        self._ctx.reset_token_tracking()

    def reload_system_prompt(self, memory_store=None,
                             tool_names=None, cwd=None):
        """重建系统提示并重新计算 token 开销。

        /init 创建 minihermes.md 后调用，使新的上下文文件立即生效。
        """
        memory_store = memory_store or get_memory_store()
        if tool_names is None:
            policy = getattr(self, "tool_policy", None)
            tool_names = (
                policy.effective_tools
                if policy is not None
                else tool_registry.get_tool_manager().get_names()
            )
        cwd = cwd or os.getcwd()
        self.system_prompt = build_system_prompt(
            model_name=self.provider.model,
            memory_store=memory_store,
            cwd=cwd,
            tool_names=tool_names,
        )

    def _execute_tool(self, tool_name: str, tool_call: dict, args: dict) -> str:
        """
        执行单个工具调用。

        Args:
            tool_name: 工具名，例如 "clarify"。
            tool_call: OpenAI tool_call 原始字典。
            args: 已解析的工具参数，例如 {"question": "..."}。

        Returns:
            工具返回的字符串结果。
        """
        if tool_name == "clarify":
            from tools.clarify import clarify as clarify_tool

            return clarify_tool(
                question=args.get("question", ""),
                callback=self.clarify_callback,
                choices=args.get("choices"),
                run_context=self._active_run_context,
            )

        if tool_name == "delegate_task":
            cached = self._delegate_batch_results
            if cached is not None and tool_call.get("id") in cached:
                return cached[tool_call["id"]]

            from agent.delegate import (
                DelegationRequest,
                build_delegate_request,
                build_delegate_renderer,
                build_delegate_spec,
            )

            request = DelegationRequest(
                task=args.get("task", ""),
                context=args.get("context", ""),
                tools=(
                    None
                    if args.get("tools") is None
                    else set(args.get("tools") or [])
                ),
            )
            run_context = self._active_run_context
            if self.runtime is None or run_context is None:
                return "Error: Delegation failed: runtime context is unavailable"
            spec = build_delegate_spec(request)
            payload = build_delegate_request(request, model=self.provider.model)
            outcome = self.runtime.run_ephemeral(
                spec=spec,
                request=payload,
                conversation_id=run_context.conversation_id,
                session_id=self._active_session_id,
                parent_task_id=run_context.task_id,
                parent_run_id=run_context.run_id,
                parent_run_context=run_context,
                renderer=build_delegate_renderer(request),
            )
            if outcome.status.value == "SUCCEEDED" and outcome.result:
                return outcome.result.final_response or "(subagent produced no response)"
            return f"Error: Delegation failed: {outcome.error_message or outcome.completion_reason}"

        if tool_name == "session_search" and self.tool_db is not None:
            from tools.session_search import _list_recent, _search

            limit = min(max(int(args.get("limit", 5)), 1), 10)
            query = args.get("query")
            return (
                _search(self.tool_db, query, limit)
                if query
                else _list_recent(self.tool_db, limit)
            )

        return f"Error: no special executor is configured for tool '{tool_name}'"

    def _run_delegate_batch(
        self,
        tool_calls: list[dict],
        run_context,
        session_id: str | None,
    ) -> dict[str, str] | None:
        """为纯 delegate_task 响应构造批次；无资格时返回 None 走旧串行路径。"""
        if self.runtime is None or run_context is None:
            return None

        from agent.delegate import (
            DelegationRequest,
            build_delegate_renderer,
            build_delegate_request,
            build_delegate_spec,
        )
        from agent.runtime import DelegateBatchItem, RunStatus

        policy = getattr(run_context, "tool_policy", None) or self.tool_policy
        items = []
        for tool_call in tool_calls:
            try:
                raw_args = tool_call["function"].get("arguments", "{}")
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (json.JSONDecodeError, TypeError, ValueError):
                return None
            if not isinstance(args, dict):
                return None
            task = args.get("task", "")
            requested_tools = args.get("tools")
            if (
                not isinstance(task, str)
                or not isinstance(args.get("context", ""), str)
                or (
                    requested_tools is not None
                    and (
                        not isinstance(requested_tools, list)
                        or not all(isinstance(name, str) for name in requested_tools)
                    )
                )
            ):
                return None
            allowed, _ = policy.allows("delegate_task", args)
            if not allowed:
                return None
            request = DelegationRequest(
                task=task,
                context=args.get("context", ""),
                tools=None if requested_tools is None else set(requested_tools),
            )
            items.append(DelegateBatchItem(
                spec=build_delegate_spec(request),
                request=build_delegate_request(request, model=self.provider.model),
                renderer=build_delegate_renderer(request),
            ))

        batch = self.runtime.run_delegate_batch(
            items=items,
            conversation_id=run_context.conversation_id,
            session_id=session_id,
            parent_task_id=run_context.task_id,
            parent_run_id=run_context.run_id,
            parent_run_context=run_context,
            parent_tool_policy=policy,
        )
        results: dict[str, str] = {}
        for tool_call, outcome in zip(tool_calls, batch.outcomes):
            if outcome.status == RunStatus.SUCCEEDED and outcome.result:
                results[tool_call["id"]] = (
                    outcome.result.final_response
                    or "(subagent produced no response)"
                )
            elif outcome.status == RunStatus.TIMED_OUT:
                results[tool_call["id"]] = (
                    "TIMED_OUT: Delegate batch deadline was reached."
                )
            else:
                results[tool_call["id"]] = (
                    "Error: Delegation failed: "
                    f"{outcome.error_message or outcome.completion_reason}"
                )
        return results

    def _process_tool_call(self, tc: dict, result: StreamResult,
                           messages: list[dict], working_history: list[dict],
                           renderer, session_id: str,
                           agent_run_id: str | None = None,
                           run_context=None) -> None:
        """处理单个 tool_call 的完整生命周期。

        JSON 解析 → 审批检查 → 工具执行 → 计数器更新 →
        diff 渲染 → 结果消息追加到 messages/working_history/DB。
        """
        tool_name = tc["function"]["name"]
        raw_args = tc["function"].get("arguments", "{}")

        policy = (
            getattr(run_context, "tool_policy", None)
            or self.tool_policy
        )

        # diff 快照只在参数有效、策略允许且不属于硬拦截时读取。
        args = None
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(args, dict):
                args = None
        except (json.JSONDecodeError, TypeError, ValueError):
            args = None

        old_content = None
        if args is not None:
            allowed, _ = policy.allows(tool_name, args)
            if allowed:
                check_result = self._approval.check(
                    tool_name,
                    args,
                    conversation_id=getattr(run_context, "conversation_id", ""),
                )
                old_content = self._snapshot_old_content(
                    tool_name, args, check_result.action
                )

        previous_context = self._active_run_context
        previous_session_id = self._active_session_id
        self._active_run_context = run_context
        self._active_session_id = session_id
        try:
            execution = tool_registry.execute_detailed(
                tc,
                ToolExecutionContext(
                    policy=policy,
                    approval_engine=self._approval,
                    approval_mode=self.approval_mode,
                    approval_callback=self.approval_callback,
                    run_context=run_context,
                    db=self.tool_db,
                    special_executor=self._execute_tool,
                    special_tool_names=frozenset({
                        "clarify", "delegate_task", "session_search",
                    }),
                    cancel_check=(
                        run_context.is_cancelled
                        if run_context and hasattr(run_context, "is_cancelled")
                        else lambda: self._interrupt_requested
                    ),
                ),
            )
        finally:
            self._active_run_context = previous_context
            self._active_session_id = previous_session_id

        tool_result = execution.model_output

        # ── 4. 进化计数器重置 ────────────────────────────────────
        if execution.status == ToolStatus.SUCCEEDED and tool_name == "skill_manage":
            self._ctx.reset_skill_iter()
        elif execution.status == ToolStatus.SUCCEEDED and tool_name == "memory":
            self._ctx.reset_memory_turn()

        # ── 5. inline diff ───────────────────────────────────────
        if (
            execution.status == ToolStatus.SUCCEEDED
            and tool_name == "write_file"
            and old_content is not None
            and renderer
            and args is not None
        ):
            new_content = args.get("content", "")
            if old_content != new_content:
                render_diff(old_content, new_content, args.get("path", ""))

        # ── 6. 构建结果消息并追加 ────────────────────────────────
        self._append_tool_result(
            tc, tool_name, tool_result, messages, working_history,
            renderer, session_id, agent_run_id,
        )

    def _append_tool_result(self, tc: dict, tool_name: str, tool_result: str,
                            messages: list[dict], working_history: list[dict],
                            renderer, session_id: str | None,
                            agent_run_id: str | None = None):
        """追加一个完整的 tool result 到内存历史和 SessionDB。"""
        if renderer:
            renderer.on_tool_result(tool_name, tool_result)
        result_msg = self.provider.build_tool_result_message(
            tool_call_id=tc["id"], result=tool_result,
        )
        result_msg["_token_count"] = len(tool_result) // 4
        result_msg["tool_name"] = tool_name
        messages.append(result_msg)
        working_history.append(result_msg)
        if self.db and session_id:
            self.db.append_message(
                session_id, role="tool", content=tool_result,
                tool_call_id=tc["id"], tool_name=tool_name,
                token_count=len(tool_result) // 4,
                agent_run_id=agent_run_id,
            )

    def _append_runtime_status(self, content: str, finish_reason: str,
                               messages: list[dict], working_history: list[dict],
                               session_id: str | None,
                               agent_run_id: str | None = None):
        """用普通 assistant 状态消息闭合失败或中断的历史边界。"""
        status_msg = {
            "role": "assistant",
            "content": content,
            "finish_reason": finish_reason,
            "_msg_type": "runtime_status",
        }
        messages.append(status_msg)
        working_history.append(status_msg)
        if self.db and session_id:
            self.db.append_message(
                session_id,
                role="assistant",
                content=content,
                finish_reason=finish_reason,
                msg_type="runtime_status",
                agent_run_id=agent_run_id,
            )

    _MAX_SNAPSHOT_LINES = 20
    _MAX_SNAPSHOT_LINE_CHARS = 2000

    @staticmethod
    def _snapshot_old_content(tool_name: str, args: dict, action: str) -> str | None:
        """write_file 执行前读取旧文件前 N 行，用于 diff 渲染。"""
        if tool_name != "write_file" or action == "block":
            return None
        write_path = args.get("path", "")
        try:
            if not os.path.isfile(write_path):
                return None
            lines: list[str] = []
            with open(write_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if len(line) > Agent._MAX_SNAPSHOT_LINE_CHARS:
                        line = line[:Agent._MAX_SNAPSHOT_LINE_CHARS] + "......\n"
                    lines.append(line)
                    if len(lines) >= Agent._MAX_SNAPSHOT_LINES:
                        lines.append("......\n")
                        break
            return "".join(lines)
        except (OSError, UnicodeDecodeError):
            return None

    def run_conversation(
        self,
        user_message: str,
        history: list[dict],
        renderer: Optional[StreamRenderer] = None,
        session_id: Optional[str] = None,
        run_context=None,
    ) -> ConversationResult:
        """
        执行一次完整的对话轮次。agent运行的核心方法

        Args:
            user_message: 用户本轮输入
            history:      历史消息列表（不含 system，由调用方维护）
            renderer:     流式渲染器
            session_id:   当前会话 ID（用于压缩时写入 DB）

        Returns:
            ConversationResult
        """
        compressed = False
        self._interrupt_requested = False
        self._ctx.start_run()
        agent_run_id = getattr(run_context, "run_id", None)

        def cancel_requested() -> bool:
            if self._interrupt_requested:
                return True
            if not run_context:
                return False
            if hasattr(run_context, "is_cancelled"):
                return run_context.is_cancelled()
            return bool(getattr(run_context, "cancel_event", None)
                        and run_context.cancel_event.is_set())

        # 构建 API 所需的完整 messages（system + history + 本轮用户消息）
        messages = [{"role": "system", "content": self.system_prompt}]
        messages += history
        messages.append({"role": "user", "content": user_message})

        # 工作副本（不含 system，用于返回给调用方）
        working_history = list(history)
        working_history.append({"role": "user", "content": user_message})

        # 实时写入 user 消息
        if self.db and session_id:
            self.db.append_message(session_id, "user", user_message,
                                   token_count=len(user_message) // 4,
                                   agent_run_id=agent_run_id)

        final_response = ""
        final_reasoning = ""
        completion_reason = "completed"
        prompt_tokens = 0
        completion_tokens = 0
        reasoning_tokens = 0

        # ══ 主循环 ══════════════════════════════════════════════════════════
        while True:
            if cancel_requested():
                self._append_runtime_status(
                    "[Interrupted before response]",
                    "interrupted_before_response",
                    messages,
                    working_history,
                    session_id,
                    agent_run_id,
                )
                completion_reason = "user_interrupt"
                break
            # 检查最大轮数的预算
            if not self._ctx.consume_budget():
                print_budget_warning(self._ctx.budget_used, self.max_iterations)
                self._append_runtime_status(
                    "[Agent stopped: iteration budget exhausted]",
                    "budget_exhausted",
                    messages,
                    working_history,
                    session_id,
                    agent_run_id,
                )
                completion_reason = "budget_exhausted"
                break

            # ── 检查点 1：调 LLM 前，估算 token 是否超限 ────────────────────
            current_tokens = self._ctx.estimate_tokens(messages)
            should = self._compressor.should_compress(current_tokens) or self._ctx.force_compress
            if should:
                self._ctx.force_compress = False
                _cprint(f"\n{_DIM}⟳ compacting context...{_RST}")
                working_history, new_sid = self._compressor.compress(
                    working_history, self.db, session_id,
                    agent_run_id=agent_run_id,
                    run_context=run_context,
                )
                if new_sid != session_id:
                    session_id = new_sid
                messages = [{"role": "system", "content": self.system_prompt}] + working_history
                self._ctx.reset_token_tracking()
                compressed = True
                if cancel_requested():
                    self._append_runtime_status(
                        "[Run stopped after context compression]",
                        "interrupted_after_compression",
                        messages,
                        working_history,
                        session_id,
                        agent_run_id,
                    )
                    completion_reason = (
                        run_context.abort_reason()
                        if run_context and hasattr(run_context, "abort_reason")
                        else "user_interrupt"
                    ) or "user_interrupt"
                    break

            if renderer:
                renderer.reset()

            # 调用 LLM（流式）
            attempts_before = getattr(run_context, "provider_attempts", 0)
            try:
                result: StreamResult = self.provider.stream(
                    messages=messages,
                    tools=self._get_tool_schemas(),
                    on_delta=renderer.on_delta if renderer else None,
                    on_thinking=renderer.on_thinking if renderer else None,
                    on_tool_start=renderer.on_tool_start if renderer else None,
                    interrupt_check=cancel_requested,
                    renderer=renderer,
                    run_context=run_context,
                )
            except KeyboardInterrupt:
                if (
                    run_context
                    and getattr(run_context, "provider_attempts", 0) == attempts_before
                ):
                    run_context.record_provider_attempt()
                self._append_runtime_status(
                    "[Interrupted before response completed]",
                    "interrupted",
                    messages,
                    working_history,
                    session_id,
                    agent_run_id,
                )
                completion_reason = (
                    run_context.abort_reason()
                    if run_context and hasattr(run_context, "abort_reason")
                    else None
                ) or "user_interrupt"
                break
            except Exception as exc:
                if (
                    run_context
                    and getattr(run_context, "provider_attempts", 0) == attempts_before
                ):
                    run_context.record_provider_attempt()
                if cancel_requested():
                    completion_reason = (
                        run_context.abort_reason()
                        if run_context and hasattr(run_context, "abort_reason")
                        else None
                    ) or "user_interrupt"
                    self._append_runtime_status(
                        "[Agent run stopped before a response was completed]",
                        "run_controlled_stop",
                        messages,
                        working_history,
                        session_id,
                        agent_run_id,
                    )
                    break
                self._append_runtime_status(
                    "[Agent run failed before a response was completed]",
                    "provider_error",
                    messages,
                    working_history,
                    session_id,
                    agent_run_id,
                )
                partial = ConversationResult(
                    final_response=final_response,
                    reasoning=final_reasoning,
                    messages=working_history,
                    session_id=session_id,
                    compressed=compressed,
                    completion_reason="provider_error",
                    iterations_used=self._ctx.budget_used,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    reasoning_tokens=reasoning_tokens,
                )
                raise AgentRunAborted(
                    error_code="provider_error",
                    safe_message=f"{type(exc).__name__}: {exc}",
                    partial_result=partial,
                    completion_reason="provider_error",
                ) from exc

            if (
                run_context
                and getattr(run_context, "provider_attempts", 0) == attempts_before
            ):
                # 测试 Provider 或第三方兼容 Provider 可能尚未接入 attempt 记账。
                run_context.record_provider_attempt()

            # 渲染器收尾
            if renderer:
                renderer.finalize()

            prompt_tokens += int(result.prompt_tokens or 0)
            completion_tokens += int(result.completion_tokens or 0)
            result_reasoning_tokens = int(
                getattr(result, "reasoning_tokens", 0) or 0
            )
            reasoning_tokens += result_reasoning_tokens
            if run_context:
                run_context.record_provider_usage(
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    reasoning_tokens=result_reasoning_tokens,
                )

            # ── 中断处理：Ctrl+C 终止了流式输出 ────────────────────────────
            if result.interrupted:
                assistant_msg = self.provider.build_assistant_message(result)
                messages.append(assistant_msg)
                working_history.append(assistant_msg)
                if self.db and session_id:
                    self.db.append_message(
                        session_id, role="assistant", content=result.content,
                        finish_reason="interrupted",
                        agent_run_id=agent_run_id,
                    )
                final_response = result.content or ""
                completion_reason = (
                    run_context.abort_reason()
                    if run_context and hasattr(run_context, "abort_reason")
                    else None
                ) or "user_interrupt"
                break

            # ── 检查点 2：用真实 usage 更新 token 追踪 ──────────────────────
            if result.prompt_tokens:
                self._ctx.update_from_usage(result.prompt_tokens, len(messages))

            # 构建 assistant 消息并追加到历史
            assistant_msg = self.provider.build_assistant_message(result)
            if result.completion_tokens:
                assistant_msg["_token_count"] = result.completion_tokens
            messages.append(assistant_msg)
            working_history.append(assistant_msg)

            # 实时写入 assistant 消息
            if self.db and session_id:
                self.db.append_message(
                    session_id, role="assistant", content=result.content,
                    tool_calls=result.tool_calls or None,
                    reasoning=result.reasoning or None,
                    token_count=result.completion_tokens,
                    finish_reason=result.finish_reason,
                    agent_run_id=agent_run_id,
                )

            final_reasoning = result.reasoning

            # ── 进化计数器：每次 LLM API 调用 +1 ───────────────────────────
            if tool_registry.get_tool_manager().has("skill_manage"):
                self._ctx.increment_skill_iter()

            # ── 无工具调用：最终回复，退出循环 ────────────────────────────
            if not result.has_tool_calls:
                final_response = result.content
                completion_reason = result.finish_reason or "completed"
                break

            # ── 有工具调用：纯 Delegate 批次可受控并行，混合调用保持串行 ──
            delegate_batch_results = None
            if all(
                tc.get("function", {}).get("name") == "delegate_task"
                for tc in result.tool_calls
            ):
                delegate_batch_results = self._run_delegate_batch(
                    result.tool_calls, run_context, session_id
                )

            cancelled_during_tools = False
            controlled_stop_reason = "user_interrupt"
            previous_delegate_batch = self._delegate_batch_results
            self._delegate_batch_results = delegate_batch_results
            try:
                for index, tc in enumerate(result.tool_calls):
                    if cancel_requested():
                        cancelled_during_tools = True
                        for pending in result.tool_calls[index:]:
                            self._append_tool_result(
                                pending,
                                pending["function"]["name"],
                                "CANCELLED: Agent run was interrupted before this tool started.",
                                messages,
                                working_history,
                                renderer,
                                session_id,
                                agent_run_id,
                            )
                        break
                    try:
                        self._process_tool_call(
                            tc, result, messages, working_history,
                            renderer, session_id, agent_run_id, run_context,
                        )
                    except Exception as exc:
                        if getattr(exc, "is_run_control", False):
                            controlled_stop_reason = getattr(
                                exc, "completion_reason", "user_interrupt"
                            )
                            cancelled_during_tools = True
                            message = (
                                "TIMED_OUT: Agent run deadline was reached before this tool started."
                                if controlled_stop_reason == "deadline_exceeded"
                                else "CANCELLED: Agent run was interrupted before this tool started."
                            )
                            for pending in result.tool_calls[index:]:
                                self._append_tool_result(
                                    pending,
                                    pending["function"]["name"],
                                    message,
                                    messages,
                                    working_history,
                                    renderer,
                                    session_id,
                                    agent_run_id,
                                )
                            break
                        for pending_index, pending in enumerate(result.tool_calls[index:]):
                            prefix = "FAILED" if pending_index == 0 else "CANCELLED"
                            self._append_tool_result(
                                pending,
                                pending["function"]["name"],
                                f"{prefix}: Agent run aborted during tool processing.",
                                messages,
                                working_history,
                                renderer,
                                session_id,
                                agent_run_id,
                            )
                        self._append_runtime_status(
                            "[Agent run failed during tool processing]",
                            "tool_internal_error",
                            messages,
                            working_history,
                            session_id,
                            agent_run_id,
                        )
                        partial = ConversationResult(
                            final_response=final_response,
                            reasoning=final_reasoning,
                            messages=working_history,
                            session_id=session_id,
                            compressed=compressed,
                            completion_reason="tool_internal_error",
                            iterations_used=self._ctx.budget_used,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            reasoning_tokens=reasoning_tokens,
                        )
                        raise AgentRunAborted(
                            error_code="tool_internal_error",
                            safe_message=f"{type(exc).__name__}: {exc}",
                            partial_result=partial,
                            completion_reason="tool_internal_error",
                        ) from exc
            finally:
                self._delegate_batch_results = previous_delegate_batch

            if cancelled_during_tools:
                self._append_runtime_status(
                    "[Interrupted during tool execution]",
                    "interrupted",
                    messages,
                    working_history,
                    session_id,
                    agent_run_id,
                )
                completion_reason = controlled_stop_reason
                break

        return ConversationResult(
            final_response=final_response,
            reasoning=final_reasoning,
            messages=working_history,
            session_id=session_id,
            compressed=compressed,
            completion_reason=completion_reason,
            iterations_used=self._ctx.budget_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
        )

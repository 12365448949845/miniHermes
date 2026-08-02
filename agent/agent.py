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


@dataclass
class ConversationResult:
    """run_conversation() 的返回值。"""
    final_response: str          # 最终文本回复
    reasoning: str               # 思考过程（可能为空）
    messages: list[dict]         # 更新后的完整对话历史（不含 system）
    session_id: str = ""         # 当前 session_id（压缩后可能改变）
    compressed: bool = False     # 本轮是否发生了压缩


class Agent:
    def __init__(
        self,
        provider: Provider,
        db: SessionDB = None,
        clarify_callback=None,
        approval_callback=None,
        auto_approve: bool = False,
        tool_filter: dict | None = None,
        system_prompt_override: str | None = None,
        max_iterations_override: int | None = None,
    ):
        self.provider = provider
        self.db = db
        self.clarify_callback = clarify_callback
        self.approval_callback = approval_callback
        self.auto_approve = auto_approve
        self._tool_filter = tool_filter or {}

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
        # 安全审批引擎
        from approval import ApprovalEngine
        self._approval = ApprovalEngine()
        # 对话状态容器（token 追踪、预算、压缩触发、进化计数器）
        tools_json = json.dumps(self._get_tool_schemas())
        self._ctx = ConversationContext(
            max_iterations=self.max_iterations,
            system_prompt=self.system_prompt,
            tools_schema_json=tools_json,
        )
        # 中断请求
        self._interrupt_requested = False

    def _get_tool_schemas(self) -> list[dict]:
        """返回经过过滤的工具 schema 列表。"""
        return tool_registry.get_schemas(
            include=self._tool_filter.get("include"),
            exclude=self._tool_filter.get("exclude"),
        )

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
        tool_names = tool_names or tool_registry.get_tool_manager().get_names()
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
            )

        if tool_name == "delegate_task":
            from agent.delegate import run_delegate, DelegationRequest

            request = DelegationRequest(
                task=args.get("task", ""),
                context=args.get("context", ""),
            )
            result = run_delegate(request, self.provider)
            if result.success:
                return result.response
            return f"[Delegation failed: {result.error}]"

        return tool_registry.execute(tool_call)

    def _process_tool_call(self, tc: dict, result: StreamResult,
                           messages: list[dict], working_history: list[dict],
                           renderer, session_id: str) -> None:
        """处理单个 tool_call 的完整生命周期。

        JSON 解析 → 审批检查 → 工具执行 → 计数器更新 →
        diff 渲染 → 结果消息追加到 messages/working_history/DB。
        """
        tool_name = tc["function"]["name"]
        raw_args = tc["function"].get("arguments", "{}")

        # ── 1. 解析参数 ──────────────────────────────────────────
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError as e:
            self._handle_json_error(tc, tool_name, raw_args, e, result,
                                    messages, working_history, renderer, session_id)
            return

        # ── 2. 审批检查 ──────────────────────────────────────────
        check_result = self._approval.check(tool_name, args)

        # diff 快照（write_file 执行前读取旧内容）
        old_content = self._snapshot_old_content(tool_name, args, check_result.action)

        # 解析审批结果
        blocked_msg = self._approval.resolve(
            check_result, tool_name=tool_name, args=args,
            auto_approve=self.auto_approve,
            approval_callback=self.approval_callback,
        )

        # ── 3. 执行工具 ──────────────────────────────────────────
        if blocked_msg is not None:
            tool_result = blocked_msg
        else:
            tool_result = self._execute_tool(tool_name, tc, args)

        if renderer:
            renderer.on_tool_result(tool_name, tool_result)

        # ── 4. 进化计数器重置 ────────────────────────────────────
        if tool_name == "skill_manage":
            self._ctx.reset_skill_iter()
        elif tool_name == "memory":
            self._ctx.reset_memory_turn()

        # ── 5. inline diff ───────────────────────────────────────
        if tool_name == "write_file" and old_content is not None and renderer:
            new_content = args.get("content", "")
            if old_content != new_content:
                render_diff(old_content, new_content, args.get("path", ""))

        # ── 6. 构建结果消息并追加 ────────────────────────────────
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
            )

    def _handle_json_error(self, tc: dict, tool_name: str, raw_args: str,
                           error: json.JSONDecodeError, result: StreamResult,
                           messages: list[dict], working_history: list[dict],
                           renderer, session_id: str) -> None:
        """处理 tool_call arguments 的 JSON 解析错误。

        把错误回填给 LLM 让它重发，避免崩整轮对话。
        """
        preview = raw_args if len(raw_args) <= 300 else raw_args[:150] + " ... " + raw_args[-150:]
        _cprint(
            f"\n{_DIM}[agent] tool args JSONDecodeError (tool={tool_name},"
            f" finish_reason={result.finish_reason}, len={len(raw_args)}): {error}{_RST}\n"
            f"{_DIM}raw: {preview}{_RST}"
        )
        err_msg = (
            f"ERROR: tool arguments JSON malformed or truncated "
            f"(JSONDecodeError: {error}). The previous arguments were not valid JSON; "
            f"please re-issue the tool call with shorter / well-formed arguments."
        )
        err_result_msg = self.provider.build_tool_result_message(
            tool_call_id=tc["id"], result=err_msg,
        )
        err_result_msg["_token_count"] = len(err_msg) // 4
        err_result_msg["tool_name"] = tool_name
        messages.append(err_result_msg)
        working_history.append(err_result_msg)

        if self.db and session_id:
            self.db.append_message(
                session_id, role="tool", content=err_msg,
                tool_call_id=tc["id"], tool_name=tool_name,
                token_count=len(err_msg) // 4,
            )
        if renderer:
            renderer.on_tool_result(tool_name, err_msg)

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
                                   token_count=len(user_message) // 4)

        final_response = ""
        final_reasoning = ""

        # ══ 主循环 ══════════════════════════════════════════════════════════
        while True:
            if self._interrupt_requested:
                # 早期中断（流式响应之前）：写入占位 assistant 消息，
                # 避免 DB 中出现孤立 user 消息导致下次连续 user 异常
                if self.db and session_id:
                    self.db.append_message(
                        session_id, role="assistant", content="[Interrupted before response]",
                        finish_reason="interrupted_before_response",
                    )
                break
            # 检查最大轮数的预算
            if not self._ctx.consume_budget():
                print_budget_warning(self._ctx.budget_used, self.max_iterations)
                break

            # ── 检查点 1：调 LLM 前，估算 token 是否超限 ────────────────────
            current_tokens = self._ctx.estimate_tokens(messages)
            should = self._compressor.should_compress(current_tokens) or self._ctx.force_compress
            if should:
                self._ctx.force_compress = False
                _cprint(f"\n{_DIM}⟳ compacting context...{_RST}")
                working_history, new_sid = self._compressor.compress(
                    working_history, self.db, session_id
                )
                if new_sid != session_id:
                    session_id = new_sid
                messages = [{"role": "system", "content": self.system_prompt}] + working_history
                self._ctx.reset_token_tracking()
                compressed = True

            if renderer:
                renderer.reset()

            # 调用 LLM（流式）
            result: StreamResult = self.provider.stream(
                messages=messages,
                tools=self._get_tool_schemas(),
                on_delta=renderer.on_delta if renderer else None,
                on_thinking=renderer.on_thinking if renderer else None,
                on_tool_start=renderer.on_tool_start if renderer else None,
                interrupt_check=lambda: self._interrupt_requested,
                renderer=renderer,
            )

            # 渲染器收尾
            if renderer:
                renderer.finalize()

            # ── 中断处理：Ctrl+C 终止了流式输出 ────────────────────────────
            if result.interrupted:
                assistant_msg = self.provider.build_assistant_message(result)
                messages.append(assistant_msg)
                working_history.append(assistant_msg)
                if self.db and session_id:
                    self.db.append_message(
                        session_id, role="assistant", content=result.content,
                        finish_reason="interrupted",
                    )
                final_response = result.content or ""
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
                )

            final_reasoning = result.reasoning

            # ── 进化计数器：每次 LLM API 调用 +1 ───────────────────────────
            if tool_registry.get_tool_manager().has("skill_manage"):
                self._ctx.increment_skill_iter()

            # ── 无工具调用：最终回复，退出循环 ────────────────────────────
            if not result.has_tool_calls:
                final_response = result.content
                break

            # ── 有工具调用：逐个处理 ──────────────────────────────
            for tc in result.tool_calls:
                if self._interrupt_requested:
                    break
                self._process_tool_call(
                    tc, result, messages, working_history,
                    renderer, session_id,
                )

        return ConversationResult(
            final_response=final_response,
            reasoning=final_reasoning,
            messages=working_history,
            session_id=session_id,
            compressed=compressed,
        )

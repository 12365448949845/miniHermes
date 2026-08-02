# Agent 对话循环

> 核心调度器，编排 LLM 调用与工具执行 | `agent/agent.py`

---

## 1. Agent 初始化

```python
class Agent:
    def __init__(self, provider, db,
                 clarify_callback=None,
                 approval_callback=None,
                 auto_approve=False,
                 tool_filter=None,
                 system_prompt_override=None,
                 max_iterations_override=None):
        self.provider = provider
        self.db = db

        # 记忆
        self.memory_store = get_memory_store()

        # System prompt
        self.system_prompt = system_prompt_override or build_system_prompt(...)

        # 工具过滤
        self.tool_filter = tool_filter

        # 回调
        self.clarify_callback = clarify_callback
        self.approval_callback = approval_callback
        self.auto_approve = auto_approve

        # 预算
        self.max_iterations = max_iterations_override or config.get("max_iterations", 30)

        # 压缩
        self.compressor = ContextCompressor(provider)

        # 进化计数器
        self._turns_since_memory = 0
        self._iters_since_skill = 0

        # Token 追踪
        self._fixed_overhead = self._estimate_tokens(
            [{"role": "system", "content": self.system_prompt}]
        ) + len(json.dumps(self._get_tool_schemas())) // 4
```

---

## 2. IterationBudget

```python
class IterationBudget:
    def __init__(self, max_iterations):
        self.max_total = max_iterations
        self._used = 0

    def consume(self) -> bool:
        if self._used >= self.max_total:
            return False
        self._used += 1
        return True

    @property
    def used(self):      return self._used
    @property
    def remaining(self): return self.max_total - self._used
```

默认 30 次。Plan mode 子 Agent 覆盖为 50。父 Agent 可为 100。

---

## 3. run_conversation() 主循环

```python
def run_conversation(self, user_message, history, renderer, session_id):
    # 构建初始消息
    messages = [
        {"role": "system", "content": self.system_prompt}
    ] + history + [
        {"role": "user", "content": user_message}
    ]

    # 写入 user 消息到 DB
    self.db.append_message(session_id, user_msg)

    budget = IterationBudget(self.max_iterations)
    compressed = False
    final_session_id = session_id

    while True:
        # 中断检查
        if self._interrupted:
            messages.append(_interrupted_placeholder())
            self.db.append_message(...)
            break

        # 预算消耗
        if not budget.consume():
            print_budget_warning(budget.used, self.max_iterations)
            break

        # 检查点 1: 压缩
        estimated = self._estimate_tokens(messages)
        if self.compressor.should_compress(estimated):
            messages, final_session_id = self.compressor.compress(
                messages, self.db, final_session_id
            )
            compressed = True

        # LLM 调用
        stream_result = self.provider.stream(
            messages=messages,
            tools=self._get_tool_schemas(),
            on_delta=renderer.on_delta,
            on_thinking=renderer.on_thinking,
            on_tool_start=renderer.on_tool_start,
            interrupt_check=self._interrupt_check,
            renderer=renderer,
        )

        # 流式中断
        if stream_result.interrupted:
            if stream_result.content:
                messages.append(...)  # 保留已接收内容
            break

        # 检查点 2: 真实 usage
        self._last_prompt_tokens = stream_result.prompt_tokens

        # 构建 assistant 消息
        assistant_msg = self.provider.build_assistant_message(stream_result)
        messages.append(assistant_msg)
        self.db.append_message(final_session_id, assistant_msg)

        # 进化计数器
        self._iters_since_skill += 1

        # 无 tool_calls → 最终响应
        if not stream_result.has_tool_calls:
            final_response = stream_result.content
            break

        # ── 工具执行循环 ──
        for tc in stream_result.tool_calls:
            tool_name = tc["function"]["name"]

            # JSON 解析
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                tool_result = f"JSONDecodeError: ..."
                messages.append(build_tool_result_message(tc["id"], tool_result))
                continue

            # 审批 gate
            if not self.auto_approve:
                if tool_name == "bash":
                    action, desc = check_command(args.get("command", ""))
                    if action == "block":
                        tool_result = "BLOCKED: ..."
                        ...
                    elif action == "confirm":
                        decision = self.approval_callback(tool_name, args, desc)
                        if decision != "allow":
                            tool_result = f"DENIED: ..."
                            ...

                elif tool_name == "write_file":
                    action, desc = check_write_file(args.get("path", ""),
                                                     args.get("content", ""))
                    ...

            # 执行工具
            tool_result = self._execute_tool(tool_name, tc, args)

            # 进化计数器重置
            if tool_name == "memory":
                self._turns_since_memory = 0
            elif tool_name == "skill_manage":
                self._iters_since_skill = 0

            # 写入结果
            tool_msg = build_tool_result_message(tc["id"], tool_result)
            messages.append(tool_msg)
            self.db.append_message(final_session_id, tool_msg)

        # 工具循环结束 → 回到 while 顶部

    return ConversationResult(
        final_response=final_response,
        reasoning=stream_result.reasoning,
        messages=messages,
        session_id=final_session_id,
        compressed=compressed,
    )
```

---

## 4. _execute_tool()

```python
def _execute_tool(self, tool_name, tool_call, args):
    # 特殊处理: clarify（需要 callback）
    if tool_name == "clarify":
        if self.clarify_callback is None:
            return "Error: clarify not available in sub-agent context"
        return clarify(args["question"], args.get("choices"), self.clarify_callback)

    # 特殊处理: delegate_task（走 run_delegate）
    if tool_name == "delegate_task":
        return run_delegate(
            DelegationRequest(task=args["task"], context=args.get("context", "")),
            self.provider
        ).response or "(subagent produced no response)"

    # 其他：走注册表
    return tool_registry.execute({
        "function": {"name": tool_name, "arguments": json.dumps(args)}
    })
```

---

## 5. _estimate_tokens()

```python
def _estimate_tokens(self, messages):
    # 首次：固定开销 + 每条消息 content/4
    # 后续：_last_prompt_tokens + 新增消息的 content/4
    if self._last_prompt_tokens is None:
        total = self._fixed_overhead
        for msg in messages[self._fixed_msg_count:]:
            total += len(msg.get("content", "")) // 4
            for tc in msg.get("tool_calls", []):
                total += len(json.dumps(tc)) // 4
        return total
    else:
        # 增量模式
        return self._last_prompt_tokens + ...
```

---

## 6. interrupt()

```python
def interrupt(self):
    self._interrupted = True
```

由 CLI 线程在 Ctrl+C 时调用。Agent 在下一个检查点响应中断。

---

## 7. JSONDecodeError 容错

```
LLM 输出半截 JSON（max_tokens 截断或流式中断）:

1. json.loads() 抛 JSONDecodeError
2. 打印诊断（tool名、raw_args preview、字节长度）
3. 构造错误 tool_result: "JSONDecodeError: ... please re-issue the tool call with complete arguments"
4. 写入 messages → LLM 下一轮看到错误 → 重发完整 tool call
```

Provider 层有对应诊断：
- 检测 malformed tool_calls（在 streaming 完成后，逐个检查 arguments 完整性）
- 打印精细诊断（字节长度、字面 `\n` 统计、控制字符统计）
- 落盘 `~/.minihermes/logs/malformed_tool_call_XXXX.json`
- Agent 层 + Provider 层双重覆盖

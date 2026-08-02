# CLI 与线程模型

> 双线程架构、斜杠命令、审批/澄清交互 | `cli/`

---

## 1. 双线程架构设计

```
┌─ 主线程 ──────────────────────────┐
│  prompt_toolkit Application        │
│  ├── Keyboard 事件处理             │
│  ├── input_queue.put(user_input)   │
│  ├── 审批/澄清 渲染                │
│  ├── StreamRenderer 流式输出       │
│  └── app.run() event loop          │
└────────────────────────────────────┘
         │ input_queue (Queue)
┌─ Daemon 线程 ──────────────────────┐
│  conversation_loop()               │
│  ├── input_queue.get() 阻塞等待    │
│  ├── 斜杠命令处理                  │
│  ├── Agent.run_conversation()      │
│  ├── 后处理 + nudge                │
│  └── state 状态更新                │
└────────────────────────────────────┘
```

- 主线程永不阻塞（UI 始终响应）
- daemon 线程通过 Queue 等待用户输入
- GIL 保护 `AppState` 的读/写（Python 天然原子性）
- daemon=True → 主线程退出时自动结束

---

## 2. AppState 共享状态

```python
@dataclass
class AppState:
    input_queue: Queue
    agent: Agent | None = None
    session_id: str = ""
    conversation_history: list[dict] = field(default_factory=list)
    plan_mode: bool = False
    renderer: StreamRenderer | None = None
    # ...
```

两个线程都可读写，GIL 保证单字段赋值原子性。

---

## 3. conversation_loop() 详细流程

```
conversation_loop(state):
  while True:
    user_input = state.input_queue.get()  # 阻塞

    # 1. 斜杠命令
    cmd, args = parse_slash(user_input)
    if cmd:
        handle_slash_command(cmd, args, ...)
        continue

    # 2. Plan mode?
    if state.plan_mode:
        plan_agent = Agent(..., max_iterations_override=50)
        result = plan_agent.run_conversation(user_input, ...)
        # 写方案 → 审批 UI → execute/cancel
        continue

    # 3. @file 预处理
    user_input = preprocess_refs(user_input, ...)

    # 4. Agent 执行
    result = state.agent.run_conversation(
        user_input, state.conversation_history, state.renderer, state.session_id
    )

    # 5. 后处理
    state.conversation_history = result.messages
    state.session_id = result.session_id  # 压缩后可能变化

    # 6. Nudge
    _try_nudge(state, ...)
```

---

## 4. 斜杠命令系统

`cli/commands.py` 定义所有 `/` 命令：

| 命令 | 功能 |
|------|------|
| /exit, /quit, /q | 退出 |
| /clear | 开启新会话 |
| /compress | 手动触发压缩 |
| /plan | 进入 Plan Mode |
| /init | 生成 minihermes.md |
| /history | 显示当前会话消息列表 |
| /sessions | 列出历史会话 |
| /resume [id] | 恢复历史会话 |
| /title <text> | 设置会话标题 |
| /sysprompt | 显示完整 system prompt |
| /help | 帮助信息 |
| /setup | 打开配置 UI |

技能也会自动注册为斜杠命令（`/<skill-name>`）。

---

## 5. 审批交互流

```
Agent._execute_tool() 检测到需审批的操作
  → 调用 approval_callback(tool_name, args, description)
    → cli/approval.py: 暂停主线程 UI
    → 渲染审批弹窗:
       ┌─ Approval Required ─────────────┐
       │ bash: rm -rf /tmp/build         │
       │ This will delete files          │
       │ [o] Allow once                  │
       │ [s] Allow for this session      │
       │ [d] Deny                        │
       └─────────────────────────────────┘
    → 用户选择 → 返回 "allow" / "deny"
  → Agent 根据返回值继续/跳过
```

- once: 仅此次允许
- session: 加入 `_session_approved` 集合，本次 session 内同类操作免审批
- deny: 返回 DENIED 错误给 LLM

---

## 6. 澄清交互流

```
Agent._execute_tool("clarify", ...)
  → 调用 clarify_callback(question, choices)
    → cli/clarify.py: 暂停主线程 UI
    → 渲染澄清弹窗（多选或开放模式）:
       ┌─ Question ───────────────────────┐
       │ Which library should we use?     │
       │ 1. requests                      │
       │ 2. httpx                         │
       │ 3. aiohttp                       │
       │ > _                              │
       └──────────────────────────────────┘
    → 用户输入 → 返回 JSON
  → Agent 获得答案 → 作为 tool result 返回给 LLM
```

- 多选模式：choices 参数（最多 4 个选项）
- 开放模式：无 choices，自由文本输入
- 超时 120s → 返回超时错误
- 子 Agent 中 clarify 被禁用（callback=None）

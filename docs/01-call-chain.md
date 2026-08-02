# MiniHermes 启动与调用链路

> 逐行追踪从启动到响应的完整流程 | 基于 2026-06-26 代码

---

## 1. main() 启动全流程

`main.py:main()` 按以下顺序初始化所有组件：

### 1.1 配置加载

```python
cfg.load()  # 读 ~/.minihermes/config.yaml，缺键从 config/config.yaml 补齐
```

首次运行时用户配置不存在 → `_ensure_config()` 触发 setup wizard → 交互式填写 API 信息 → 写入 `~/.minihermes/config.yaml`。

### 1.2 核心组件初始化

```python
SessionDB()       # 打开 ~/.minihermes/state.db，WAL 模式，自动建表+migration
Provider()        # 创建 OpenAI client，max_retries=0
StreamRenderer()  # 流式输出渲染器，管理每轮响应的终端展示
AppState()        # CLI/后台线程共享状态容器
```

### 1.3 Agent 构建

```python
Agent(provider, db,
      clarify_callback=make_clarify_callback(...),
      approval_callback=make_approval_callback(...),
      auto_approve=False,
      tool_filter=None,
      system_prompt_override=None,
      max_iterations_override=None)
```

Agent.__init__ 内：
1. `get_memory_store()` → MemoryStore 加载 MEMORY.md / USER.md 快照
2. `build_system_prompt()` → 多层组装 system prompt
3. 计算 `_fixed_overhead` = system_prompt + tool schemas 的 token 估算
4. 初始化 `ContextCompressor`
5. 初始化进化计数器 `_turns_since_memory = 0`, `_iters_since_skill = 0`

### 1.4 会话与技能

```python
session_id = generate_session_id()     # "20260626_143022_a1b2c3"
db.create_session(session_id, ...)     # INSERT OR IGNORE（幂等）
sync_builtin_skills()                  # 同步 _builtin_skills/ 到 ~/.minihermes/skills/
register_skill_commands()              # 为每个技能注册斜杠命令
```

### 1.5 UI 启动

```python
app = build_app(state)
thread = Thread(target=conversation_loop, daemon=True)
thread.start()
app.run()  # 主线程进入 UI 事件循环
```

### 1.6 退出清理

```
用户 /exit /quit 或 Ctrl+C
  → app.exit()
  → 主线程退出 event loop
  → daemon 线程退出
  → maybe_run_curator(provider) → 技能库维护
  → db.end_session(session_id, end_reason="user_exit")
```

---

## 2. 单轮对话端到端调用链路

### 2.1 用户输入 → 进入对话队列

```
主线程: Keyboard 事件 (Enter 键)
  → keybindings.py: Enter handler
  → state.input_queue.put(user_input)
  → 返回 UI 等待
```

### 2.2 Daemon 线程消费

```
conversation_loop():
  input_queue.get() → user_input

  Step 1: 斜杠命令检测
    匹配 SLASH_COMMANDS → 处理 → continue

  Step 2: Plan mode 检测
    以 __PLAN_MODE__: 开头 → 进入 plan 分支

  Step 3: @file 引用预处理
    context_ref.preprocess_refs(user_input, most_recent_tool_results)

  Step 4: 进入 Agent 主循环
    result = agent.run_conversation(user_input, history, renderer, session_id)
```

### 2.3 Agent.run_conversation() 详细流程

```
run_conversation(user_message, history, renderer, session_id):

  1. 构建消息列表
     new_messages = [system] + history + [user_msg]

  2. 写入 DB: db.append_message(session_id, user msg)

  3. ──── while 主循环 ────

     a. 中断检查 (_interrupted → 写入占位 → 退出)
     b. 预算消耗 (budget.consume() → False → 退出 + 警告)
     c. 检查点 1: token 估算
        if compressor.should_compress(estimated):
            → 五阶段压缩
            → messages 替换为 [summary] + tail
            → session 分裂
     d. LLM 调用
        stream_result = provider.stream(messages, tools, callbacks)
     e. 流式中断检查 (Ctrl+C 期间)
     f. 检查点 2: 更新真实 usage
     g. 构建 assistant 消息 → append to messages + DB
     h. 进化计数器 +1

     i. 无 tool_calls → break

     j. ── 工具执行循环 ──
        for tool_call in stream_result.tool_calls:
            i.   JSON 解析 (失败 → JSONDecodeError 容错)
            ii.  审批 gate (bash/write_file)
            iii. 执行 _execute_tool()
            iv.  写 tool result → messages + DB

        # 回到 while 顶部继续

  4. 返回 ConversationResult
```

### 2.4 对话后处理

```
conversation_loop() 继续:
  1. /init 完成 → agent.rebuild_system_prompt()
  2. 状态栏更新
  3. state.conversation_history = result.messages
  4. state.session_id = result.session_id  # 压缩后可能变化
  5. _try_nudge() → 进化触发检查
  6. 每 20 轮提示 /clear
```

---

## 3. Plan Mode 分支

```
"/plan 创建 REST API"
  → handle_slash_command("plan", ...)
  → 进入 Plan Mode，提示输入需求

conversation_loop plan 分支:
  1. Plan Agent (只读工具, max_iter=50)
  2. plan_agent.run_conversation() → 方案
  3. 写 .minihermes/plans/<timestamp>.md
  4. 审批 UI → Execute / Cancel
  5. Execute → 主 Agent 执行方案
```

---

## 4. 关键代码位置索引

| 组件 | 文件 | 关键函数/类 |
|------|------|-----------|
| 入口 | main.py | main() |
| 配置加载 | config/config.py | load(), _ensure_config() |
| Provider 初始化 | provider/provider.py | Provider.__init__() |
| Agent 构建 | agent/agent.py | Agent.__init__() |
| System Prompt | prompt/builder.py | build_system_prompt() |
| 对话主循环 | agent/agent.py | run_conversation() |
| LLM 流式调用 | provider/provider.py | stream() |
| 单次流式 | provider/provider.py | _stream_once() |
| 工具调度 | tools/__init__.py | execute() |
| 工具重试 | tools/retry.py | execute_with_retry() |
| 审批检查 | tools/approval.py | check_command(), check_write_file() |
| 上下文压缩 | context/compressor.py | compress() |
| Plan Mode | cli/plan.py | generate_plan_path() |
| 子 Agent 委派 | agent/delegate.py | run_delegate() |
| 进化 Nudge | evolution/nudge.py | spawn_nudge() |
| Curator | evolution/curator.py | run_curator() |
| Session 分裂 | session/db.py | create_child_session() |
| Session 恢复 | session/db.py | resolve_resume_session_id() |
| 流式渲染 | renderer/renderer.py | StreamRenderer |
| @file 预处理 | cli/context_ref.py | preprocess_refs() |
| 技能发现 | skills/manager.py | discover_skills() |

---

## 5. Agent 整体流程图

### 5.1 启动到响应数据流

```
main()
  ├── config.load()         ─── 配置
  ├── SessionDB()           ─── 持久化
  ├── Provider()            ─── LLM 通信
  ├── StreamRenderer()      ─── 终端渲染
  ├── AppState()            ─── 共享状态
  ├── Agent(provider, db, callbacks)
  │     ├── MemoryStore     ─── 记忆
  │     ├── SystemPrompt    ─── 上下文
  │     └── Compressor      ─── 压缩
  ├── db.create_session()
  ├── build_app()           ─── UI
  └── ┌─ 主线程: app.run()
       └─ Daemon: conversation_loop()
```

### 5.2 对话主循环

```
entry: messages = [system] + history + [user_msg]

┌─────────────────────────────┐
│  while budget.consume():    │
│  ┌───────────────────────┐  │
│  │ 1. 压缩检查           │  │
│  │ 2. provider.stream()  │  │
│  │ 3. 写入 assistant     │  │
│  │ 4. 无 tools → break   │  │
│  │ 5. for tool in tools: │  │
│  │    ├─ 审批 gate       │  │
│  │    ├─ 执行            │  │
│  │    └─ 写 tool result  │  │
│  └───────────────────────┘  │
│  return ConversationResult  │
└─────────────────────────────┘
```

### 5.3 上下文压缩子流程

```
should_compress(tokens)
  ├── tokens < threshold → skip
  ├── cooldown active → skip
  └── ineffective >= 2 → skip

compress(history, db, session_id):
  Phase 1: split → head + middle + tail
  Phase 2: prune tool outputs >500 chars
  Phase 3: LLM summary (非流式, temp=0.3)
  Phase 4: sanitize tool pairs
  Phase 5: assemble + create_child_session
```

### 5.4 Plan Mode 流程

```
"/plan ..."
  ├── Plan Agent (只读, max_iter=50)
  ├── 写 .minihermes/plans/<ts>.md
  ├── 审批 UI
  ├── Execute → 主 Agent 执行
  └── Cancel  → 删除方案文件
```

### 5.5 子 Agent 委派流程

```
Agent._execute_tool("delegate_task", ...)
  └── run_delegate(request, provider)
        ├── Child Agent
        │     ├── 固定简化 system prompt
        │     ├── 禁用 delegate_task + clarify
        │     ├── db=None (无持久化)
        │     ├── auto_approve=True
        │     └── max_iterations=15
        ├── child.run_conversation(task, [], renderer)
        └── return DelegationResult
```

### 5.6 进化系统触发流程

```
conversation_loop 每次循环结束:
  _try_nudge(state, provider):

  Memory Nudge:
    if _turns_since_memory >= 10:
      spawn_nudge(provider, messages, "memory")
      _turns_since_memory = 0

  Skill Nudge:
    if _iters_since_skill >= 10:
      spawn_nudge(provider, messages, "skill")
      _iters_since_skill = 0

session 退出:
  maybe_run_curator(provider):
    lifecycle_transitions()  # stale/archive
    consolidate(provider)    # LLM 合并
```

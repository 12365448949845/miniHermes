# 05 — 工具系统

代码位置: `tools/`

---

## 0. 工具系统设计背景

### Function Calling 协议发展

2023 年 OpenAI 推出 Function Calling 后，工具调用成为 AI Agent 的标准模式。目前主流协议：

| 协议 | 推出方 | 特点 | 生态 |
|------|--------|------|------|
| **OpenAI Function Calling** | OpenAI | JSON Schema 定义参数，单轮 tool_calls | 最广泛 |
| **Anthropic Tool Use** | Anthropic | 类似 OpenAI，支持 tool_choice 更精细 | Claude 生态 |
| **MCP (Model Context Protocol)** | Anthropic | 客户端-服务器架构，JSON-RPC 传输 | 跨模型、跨工具 |
| **Google Function Calling** | Google | Gemini API，与 OpenAI 格式兼容 | Gemini 生态 |

**MiniHermes 选择 OpenAI Function Calling 协议**：
- 最广泛的兼容性（DeepSeek、Qwen、Ollama 均支持）
- Schema 标准化，工具可跨模型复用
- 2025 年后 MCP 成熟可考虑迁移

### 工具系统设计模式

```
┌─────────────────────────────────────────────────────┐
│                    Tool Registry                     │
│  ┌─────────┐  ┌─────────┐  ┌──────────┐            │
│  │  Schema  │  │ Execute │  │  Filter  │            │
│  │ (JSON)   │  │ (dispatch)│ │(include/ │            │
│  │          │  │         │  │ exclude) │            │
│  └─────────┘  └─────────┘  └──────────┘            │
│       ↑              ↓                              │
│  @register()    ToolResult                          │
│       ↑              ↓                              │
│  ┌──────────────────────────────┐                   │
│  │ bash │ files │ search │ ...  │  ← Tool Modules   │
│  └──────────────────────────────┘                   │
│       ↑              ↓                              │
│  ┌──────────────────────────────┐                   │
│  │ retry │ truncate │ approval  │  ← Cross-cutting  │
│  └──────────────────────────────┘                   │
└─────────────────────────────────────────────────────┘
```

**横切关注点分离**：
- **注册**（`tools/registry.py`）：装饰器模式，声明式注册
- **执行**（`tools/retry.py`）：统一的重试、错误分类、参数调整
- **安全**（`tools/approval.py` + `approval/engine.py`）：硬拦截 / 软拦截 / session 白名单
- **输出控制**（`tools/__init__.py`）：全局截断 + 各工具独立截断

### 与 MCP 协议的关系

MCP（Model Context Protocol）是 Anthropic 2024 年底推出的工具协议标准。对比：

| 维度 | MiniHermes (OpenAI FC) | MCP |
|------|------------------------|-----|
| 工具发现 | 静态注册 (`@register`) | 动态 `tools/list` |
| 传输 | 进程内调用 | JSON-RPC (stdio/SSE) |
| 工具隔离 | 无（共享 Python 进程） | 独立进程，沙箱隔离 |
| 扩展性 | 需修改代码 | 即插即用 |
| 复杂度 | 低 | 高 |

MiniHermes 当前选择进程内注册有其合理性（性能、简单），未来可考虑支持 MCP 作为外部工具源。

---

## 1. 基础设施

### 1.1 注册机制

```python
# tools/__init__.py
_REGISTRY: dict[str, dict] = {}

def register(schema: dict):
    """装饰器：将函数注册为 LLM 可调用工具"""
    def decorator(func):
        name = schema["function"]["name"]
        _REGISTRY[name] = {"schema": schema, "func": func}
        return func
    return decorator
```

每个工具文件在模块底部定义 schema + 注册函数，`__init__.py` 末尾 import 触发注册。

### 1.2 输出截断

```python
MAX_OUTPUT_CHARS = 50_000

def truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head_chars = int(max_chars * 0.40)
    tail_chars = int(max_chars * 0.60)
    omitted = len(text) - max_chars
    return (text[:head_chars] +
            f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted out of {len(text)} total] ...\n\n" +
            text[-tail_chars:])
```

全局 50K chars，head 40% + tail 60%。部分工具有独立的更严格限制。

### 1.3 工具重试

`tools/retry.py` 统一处理工具层面的重试：


| 错误类型      | 策略                      |
| --------- | ----------------------- |
| TIMEOUT   | 超时 ×2 重试（max_retries=2） |
| TRANSIENT | 延迟 2s 重试                |
| PERMANENT | 不重试，直接返回错误              |


### 1.4 审批系统

两层防线（`tools/approval.py`）：

**硬拦截（HARDLINE）— 绝对不执行：**
`rm /`, `mkfs`, `dd of=/dev/`, fork bomb, `shutdown`, `kill -9 1`

**软拦截（DANGEROUS）— 弹出审批 UI：**
`rm`, `chmod 777`, `git reset --hard`, `git push --force`, `curl | sh`, `sudo`, `DROP TABLE`

**write_file 独立检查：**

- 敏感路径：`.env`、`.ssh/`、`/etc/`、`credentials`、`secrets`、`.gitconfig`、shell config
- 敏感内容：`API_KEY`/`SECRET`/`PASSWORD`/`TOKEN` 嵌入

---

## 2. 工具清单（16 个）


| 工具              | 文件                      | 功能           |
| --------------- | ----------------------- | ------------ |
| bash            | tools/bash.py           | 本地 shell 执行  |
| read_file       | tools/files.py          | 读取文件（含行号）    |
| write_file      | tools/files.py          | 写入文件（支持追加）   |
| list_dir        | tools/files.py          | 列出目录内容       |
| web_search      | tools/search.py         | web 搜索       |
| web_extract     | tools/web_extract.py    | 网页内容提取       |
| execute_code    | tools/code_execution.py | 云沙箱代码执行      |
| memory          | tools/memory.py         | 持久化记忆 CRUD   |
| delegate_task   | tools/delegate.py       | 子 Agent 委派   |
| clarify         | tools/clarify.py        | 主动向用户提问      |
| todo            | tools/todo.py           | 任务列表管理       |
| process         | tools/process_tool.py   | 进程列表查看       |
| session_search  | tools/session_search.py | 历史会话 FTS5 搜索 |
| skill_view      | tools/skills_tool.py    | 加载技能详情       |
| skill_manage    | tools/skill_manage.py   | 技能 CRUD      |
| recognize_image | tools/vision.py         | 多模态图片识别      |
| generate_image  | tools/image_gen.py      | AI 文生图       |


---

## 3. 关键设计决策

- **装饰器注册**：工具和 schema 紧耦合在同一位置，新增工具只需在模块中 `@register` 即可
- **全局截断**：防止工具输出污染上下文，保证 LLM 看见完整格式
- **审批分层**：硬拦截永不执行，软拦截可授权 session 级别
- **重试隔离**：重试与执行分离，不影响 Agent 循环逻辑


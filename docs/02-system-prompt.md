# System Prompt Engineering

> 多层组装、前缀缓存、注入检测 | `prompt/builder.py:build_system_prompt()`

---

## 0. Prompt Engineering 背景

### System Prompt 的演进

| 阶段 | 代表 | 特点 |
|------|------|------|
| **1.0 自然语言指令**（2023） | ChatGPT System Message | 一段话描述角色和行为约束 |
| **2.0 结构化分层**（2024） | Claude 3 System Prompt, MiniHermes | 分层注入：身份、记忆、工具引导、环境 各自独立 |
| **3.0 动态 + 缓存感知**（2025） | Claude 4 Prompt Caching, Gemini Context Caching | 利用前缀缓存优化，静态部分固定在前 N tokens |

### 前缀缓存（Prompt Caching）的核心原理

Anthropic 的 Prompt Caching 和 Google 的 Context Caching 都基于同一原理：

```
Request 1: [Static Part 10K tokens] + [Dynamic Part 2K tokens]
Request 2: [Static Part 10K tokens] + [Dynamic Part 3K tokens]
            ↑ cache hit, 免费或低价              ↑ cache miss, 正常计费
```

**分层组装与缓存的关系**：
- Layer 1-9（身份、记忆、工具引导、环境等）在一次 session 内固定不变
- 放在 system prompt 前端 → 每次请求前缀完全匹配 → 缓存命中
- Layer 10-12（时间戳、CLI 提示）可能变化 → 放在末尾

### MiniHermes 的分层设计哲学

```
┌──────────────────────────────────────────┐
│  Layer 1:  身份 (SOUL.md)                │  ← 缓存命中区
│  Layer 2-6: 工具行为引导                 │     (session 内不变)
│  Layer 7:  记忆快照                      │
│  Layer 8:  技能索引                      │
│  Layer 9:  上下文文件                    │
├──────────────────────────────────────────┤
│  Layer 10: 时间戳                        │  ← 缓存失效区
│  Layer 11: 环境提示                      │     (可能变化)
│  Layer 12: 平台提示                      │
└──────────────────────────────────────────┘
```

**关键设计决策**：
1. 记忆使用"冻结快照"而非实时注入 → 保证缓存命中
2. 工具引导按注册工具条件注入 → 动态但 session 内确定
3. 上下文文件优先级链 → 用户可按需选择
4. 两层身份（SOUL.md + DEFAULT_IDENTITY）→ 可定制 + 兜底

### 注入检测（Injection Detection）

MiniHermes 的 system prompt 中包含对用户输入的安全检测，这是对 **间接注入攻击** 的基本防护：

```
攻击模式: 
  用户上传文件包含 "Ignore all previous instructions..."
  → @file 展开后注入 LLM 上下文
  → LLM 被劫持

防御:
  _scan_for_injection(content) 检测:
  - 零宽度字符 (zero-width chars)
  - 威胁模式匹配 (ignore previous instructions / system prompt revealed 等)
  - 检测到 → 返回 BLOCKED 占位符
```

---

## 1. 多层组装流程

`build_system_prompt(model_name, memory_store, cwd, tool_names)` 在每次 session 启动时调用一次，之后 session 内不变。

### Layer 1: 身份定义

```python
soul = load_soul_md()  # 从 ~/.minihermes/SOUL.md 读取
if not soul:
    soul = DEFAULT_IDENTITY  # 内置默认身份
    # 写入 SOUL.md，下次启动直接读取
```

`DEFAULT_IDENTITY` 定义 Agent 角色：一个运行在终端中的 AI 编程助手。

### 工具行为引导（条件注入）

根据 `tool_names` 中实际注册的工具，条件注入对应的行为引导块：

| 引导块 | 触发条件 | 内容要点 |
|--------|---------|---------|
| MEMORY_GUIDANCE | "memory" in tools | 记忆在下个 session 才生效，不要重复已记录的事实 |
| CLARIFY_GUIDANCE | "clarify" in tools | 何时主动问用户，不要猜测前提 |
| TODO_GUIDANCE | "todo" in tools | 管理多步骤任务进度 |
| CODE_EXECUTION_GUIDANCE | "execute_code" in tools | 沙箱特点：临时环境，不保留文件 |
| DELEGATE_GUIDANCE | "delegate_task" in tools | 委派自包含子任务，子 Agent 无记忆 |
| SKILL_MANAGE_GUIDANCE | "skill_manage" in tools | 何时创建新技能，命名和描述规范 |

### Layer 7: 记忆快照

```python
memory_text = memory_store.format_for_system_prompt()
```

从 MemoryStore 的冻结快照中获取 MEMORY.md 和 USER.md 内容。快照在 session 启动时创建，整个 session 内不变。

### Layer 9: 项目上下文文件

```python
context_text = build_context_files_prompt(cwd)
```

按优先级发现上下文文件（首个匹配即生效，互斥）：

```
1. minihermes.md     ← 从 cwd 向上搜索到 git root
2. .hermes.md / HERMES.md  ← 向上搜索到 git root
3. AGENTS.md / agents.md   ← 仅 cwd
4. CLAUDE.md / claude.md   ← 仅 cwd
5. .cursorrules / .cursor/rules/*.mdc ← 仅 cwd
```

每个文件经过：`注入检测 → 截断(20K chars) → 包装为 # Project Context 块`。

### Skills 索引

```python
skills_index = build_skills_index()  # 扫描全局+项目技能目录
```

每行一个技能：`- skill-name: one-line description`。Agent 调用 `skill_view` 时再加载完整内容。

### Layer 10: 时间戳

```python
timestamp_block = f"# currentDate\nToday's date is {datetime.now():%Y-%m-%d}."
```

### Layer 10.5: 环境事实块

```python
env_block = build_env_block(cwd)
# → <env>
#     cwd: /path/to/project
#     platform: darwin
#     OS Version: Darwin 24.x
#   </env>
```

### Layer 11: 环境提示

```python
env_hint = build_env_hint()  # 动态检测 WSL / macOS / Linux / Windows
```

4 种环境各有专门提示：WSL（Windows .exe 调用）、macOS（open 命令）、Linux（systemd 等）、Windows（PowerShell 优先）。

### Layer 12: CLI 平台提示

```python
platform_hint = CLI_PLATFORM_HINT  # 固定字符串
```

固定指引：用 markdown 链接格式引用文件、不要用 HTML 标签、用 `len()` 计算宽度、ANSI 仅在打印时使用。

### 最终组装

```
[Layer 1: 身份]
[工具行为引导]
[Layer 7: 记忆快照]
[Layer 9: 项目上下文]
[Skills 索引]
[Layer 10: 时间戳]
[Layer 10.5: 环境事实]
[Layer 11: 环境提示]
[Layer 12: CLI 平台提示]
```

---

## 2. 前缀缓存策略

system_prompt 在 session 内不变：
- **首次 LLM 调用**：全量 prompt 发送（无缓存命中）
- **后续 LLM 调用**：system_prompt 前缀命中缓存，仅增量部分计费
- **唯一例外**：`/init` 命令完成后重建（注入新生成的 minihermes.md），中断缓存

冻结快照记忆机制进一步保证：session 内 memory tool 操作修改磁盘但不更新快照，system_prompt 完全不变。

---

## 3. 上下文文件发现链

`build_context_files_prompt(cwd)`:

```python
def build_context_files_prompt(cwd):
    git_root = _find_git_root(cwd)

    candidates = [
        ("minihermes.md",      "up_to_git_root"),
        (".hermes.md",         "up_to_git_root"),
        ("AGENTS.md",          "cwd_only"),
        ("CLAUDE.md",          "cwd_only"),
        (".cursorrules",       "cwd_only"),
    ]

    for filename, strategy in candidates:
        path = resolve(filename, strategy, git_root, cwd)
        if path and path.exists():
            content = _load_file(path, filename)  # 读 + 检测 + 截断
            if content:
                break  # 第一个匹配即停止

    return f"# Project Context\n\n{content}"
```

`_load_file()` 内部：
```
读取 → _scan_for_injection() → _truncate_content() → 返回安全内容
```

---

## 4. 注入检测

`_scan_for_injection(content, filename)`:

### 不可见字符检测

```python
_INVISIBLE_CHARS = {'​', '‌', '‍', '⁠', '﻿'}
# Zero-width space, Zero-width non-joiner, Zero-width joiner,
# Word joiner, BOM
```

### 威胁模式匹配

```python
_THREAT_PATTERNS = [
    (r'(?im)^\s*#\s*(?:system|override)\s*$',     "system_override"),
    (r'(?i)<system-reminder>',                       "sys_reminder_xml"),
    (r'(?im)^\s*<function_calls>',                  "function_call_xml"),
    (r'(?i)\[system\].*?(?:ignore|forget)\b',        "sys_ignore"),
    (r'(?im)^\s*You are now\b',                      "identity_switch"),
    (r'(?i)Disregard\s+(?:all|previous)\s+instructions', "disregard"),
]
```

命中 → `BLOCKED: {filename} contains potential prompt injection`

---

## 5. 截断策略

`_truncate_content(content, filename, max_chars=20_000)`:

```
if len(content) <= max_chars:
    return content

head_chars = int(max_chars * 0.70)  # 保留头 70%
tail_chars = int(max_chars * 0.20)  # 保留尾 20%

return f"{head}\n\n... [{omitted} chars omitted] ...\n\n{tail}"
```

---

## 6. 伪代码

```python
def build_system_prompt(model_name, memory_store, cwd, tool_names):
    parts = []

    # Layer 1
    soul = load_soul_md()
    parts.append(soul or DEFAULT_IDENTITY)

    # 工具行为引导（条件注入）
    if "memory" in tool_names:
        parts.append(MEMORY_GUIDANCE)
    if "clarify" in tool_names:
        parts.append(CLARIFY_GUIDANCE)
    if "todo" in tool_names:
        parts.append(TODO_GUIDANCE)
    if "execute_code" in tool_names:
        parts.append(CODE_EXECUTION_GUIDANCE)
    if "delegate_task" in tool_names:
        parts.append(DELEGATE_GUIDANCE)
    if "skill_manage" in tool_names:
        parts.append(SKILL_MANAGE_GUIDANCE)

    # Layer 7
    if memory_store:
        mem = memory_store.format_for_system_prompt()
        if mem:
            parts.append(mem)

    # Layer 9
    context = build_context_files_prompt(cwd)
    if context:
        parts.append(context)

    # Skills
    skills = build_skills_index()
    if skills:
        parts.append(skills)

    # Layers 10-12
    parts.append(f"# currentDate\nToday's date is {datetime.now():%Y-%m-%d}.")
    parts.append(build_env_block(cwd))
    parts.append(build_env_hint())
    parts.append(CLI_PLATFORM_HINT)

    return "\n\n".join(parts)
```

# 10 — 进化系统

> Nudge 复盘 + Curator 维护 + Telemetry 遥测 | `evolution/`

---

## 1. 系统概览

三个子系统协同工作：

| 子系统 | 触发时机 | 功能 |
|--------|---------|------|
| Nudge | 会话中（每 N 轮） | 复盘对话，更新记忆/创建技能 |
| Curator | 会话结束时 | 技能库生命周期维护 |
| Telemetry | 技能使用时 | 记录使用统计 |

---

## 2. Nudge 系统

### 触发机制

`conversation_loop` 每次循环结束调用 `_try_nudge()`：

```python
# 两个独立计数器
_turns_since_memory   # 累积用户对话轮数
_iters_since_skill    # 累积 LLM API 调用次数

def _try_nudge(state, provider):
    agent = state.agent

    # Memory Nudge: 每 10 轮用户对话
    if agent._turns_since_memory >= 10:
        spawn_nudge(provider, state.conversation_history, "memory")
        agent._turns_since_memory = 0

    # Skill Nudge: 每 10 次 LLM 调用
    if agent._iters_since_skill >= 10:
        spawn_nudge(provider, state.conversation_history, "skill")
        agent._iters_since_skill = 0
```

**反弹保护：**
- memory 工具调用 → `_turns_since_memory = 0`
- skill_manage 工具调用 → `_iters_since_skill = 0`

防止刚操作完就又被触发。

### Nudge Agent

在独立 daemon 线程运行，不阻塞用户：

```python
def spawn_nudge(provider, messages, nudge_type="both"):
    thread = Thread(target=_run_nudge, args=(provider, messages, nudge_type))
    thread.daemon = True
    thread.start()
```

**Memory Nudge：**
- Prompt: 分析对话，识别应持久化的事实（环境、约定、偏好）
- 工具: 仅 `memory`
- 最大迭代: 10

**Skill Nudge：**
- Prompt: 分析对话，识别可复用的操作模式
- 工具: `skill_manage`, `skill_view`, `read_file`
- 最大迭代: 10

---

## 3. Curator 系统

### 触发

会话结束时：

```python
def maybe_run_curator(provider):
    if should_run_curator():
        thread = Thread(target=run_curator, args=(provider,))
        thread.daemon = True
        thread.start()
```

`should_run_curator()`: 距上次运行 ≥ 7 天。

### Phase 1: 生命周期转换（确定性）

扫描 `~/.minihermes/skills/` 中所有 auto 技能：

```
active ──(7天未使用)──→ stale ──(30天未使用)──→ archived
```

```python
def lifecycle_transitions():
    stats = {"marked_stale": 0, "archived": 0, "checked": 0}
    for skill in get_auto_skills():
        days_inactive = _days_since(skill["last_used"])
        if days_inactive >= archive_days and skill["status"] == "stale":
            _update_skill_status(skill["path"], "archived")
            stats["archived"] += 1
        elif days_inactive >= stale_days and skill["status"] == "active":
            _update_skill_status(skill["path"], "stale")
            stats["marked_stale"] += 1
    return stats
```

状态通过修改 SKILL.md 的 YAML frontmatter `status:` 字段实现。

### Phase 2: LLM 合并（AI 驱动）

当 auto 技能 ≥ 5 个时触发：

```python
def consolidate(provider):
    skills = get_auto_skills()
    if len(skills) < 5:
        return None  # 不够触发阈值

    # 构建合并 Agent
    agent = Agent(
        provider, db=None,
        auto_approve=True,
        max_iterations_override=10,
        system_prompt_override=CONSOLIDATION_PROMPT,
    )
    result = agent.run_conversation(
        "Review and consolidate overlapping skills...",
        [], renderer=None,
    )
    return result.final_response
```

### 状态持久化

```python
STATE_FILE = USER_SKILLS_DIR / ".curator_state"

{
    "last_run": "2026-06-26T10:00:00",
    "last_stats": {"checked": 12, "marked_stale": 2, "archived": 1}
}
```

---

## 4. Telemetry 系统

### 数据结构

每个技能目录下有 `.usage.json`：

```json
{
    "total_uses": 15,
    "first_used": "2026-01-15T10:30:00",
    "last_used": "2026-06-20T14:22:00"
}
```

### API

```python
def init_usage(skill_name):      # skill_manage create 时调用
def record_usage(skill_name):    # skill_view 每次调用时 total_uses++
def get_usage(skill_name):       # 查询单个技能统计
def list_all_usage():            # 查询所有技能统计
```

### 用途

- Curator 依赖 `last_used` 判断 stale/archive
- 用户可通过工具查看技能使用频率

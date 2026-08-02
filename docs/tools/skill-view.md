# skill_view — 技能加载

代码: `tools/skills_tool.py` (35行)

---

## Schema 设计

```python
{
    "name": "skill_view",
    "parameters": {
        "name": {"type": "string", "description": "Name of the skill to load."}
    },
    "required": ["name"]
}
```

## 实现

```python
def skill_view(name: str) -> str:
    content = load_skill(name)  # 从磁盘加载 SKILL.md
    if content is None:
        available = [s["name"] for s in discover_skills()]
        return f"Error: skill '{name}' not found. Available: {', '.join(available)}"

    record_usage(name)  # telemetry: total_uses++
    return content
```

## 关键设计点

### 延迟加载

System prompt 中只包含技能索引（一行一个技能名+简述），不注入完整的 SKILL.md 内容。Agent 需要时才调用 `skill_view` 加载，节省 system prompt token。

### Telemetry 记录

每次 `skill_view` 调用都通过 `record_usage(name)` 更新 `.usage.json`（total_uses++），为 Curator 的 stale/archive 决策提供数据。

### 友好错误消息

技能不存在时列出所有可用技能名，方便 LLM 选择正确的名称。

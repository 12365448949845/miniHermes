# clarify — 主动提问

代码: `tools/clarify.py` (110行) + `cli/clarify.py`（UI 层）

---

## Schema 设计

```python
{
    "name": "clarify",
    "parameters": {
        "question": {"type": "string", "description": "The question to ask the user."},
        "choices": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional choices for multi-choice mode (max 4)."
        }
    },
    "required": ["question"]
}
```

## 实现

```python
def clarify(question: str, choices: list[str] = None, callback=None) -> str:
    if callback is None:
        return json.dumps({"error": "not available", ...})

    choices = _normalize_choices(choices)
    response = callback(question, choices)  # 阻塞等待用户输入
    return json.dumps({
        "question": question,
        "choices_offered": choices,
        "user_response": response,
    })
```

## 关键设计点

### 回调注入

工具函数本身不做 UI——通过 `callback` 参数注入。Agent 构建时从 CLI 层传入 `clarify_callback`。子 Agent 中 callback=None → 工具返回错误（防止递归等待）。

### 两种模式

- **多选模式**: choices 参数提供最多 4 个选项（UX 约束，非技术限制）
- **开放模式**: 无 choices，用户自由文本输入

### 标准化 JSON 返回

返回结构化 JSON，LLM 可直接解析 `user_response` 字段获取答案。

### 子 Agent 禁用

`CHILD_BLOCKED_TOOLS = frozenset({"delegate_task", "clarify"})` — 子 Agent 中 clarify 不可用，避免递归反问用户。

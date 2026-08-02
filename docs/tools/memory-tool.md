# memory — 持久化记忆工具

代码: `tools/memory.py` (工具注册部分)

---

## Schema 设计

```python
{
    "name": "memory",
    "parameters": {
        "action": {"enum": ["add", "update", "delete", "view"]},
        "target": {"enum": ["memory", "user"]},
        "entry": {"type": "string", "description": "For add/update."},
        "index": {"type": "integer", "description": "For update/delete."}
    },
    "required": ["action", "target"]
}
```

Schema 描述中明确告知 LLM：**修改在下个 session 的 system prompt 中生效**。

## 实现

```python
def memory_fn(action: str, target: str, entry: str = None, index: int = None) -> str:
    store = get_store()  # 模块级单例

    if action == "add":
        return store.add(target, entry)
    elif action == "update":
        return store.update(target, index, entry)
    elif action == "delete":
        return store.delete(target, index)
    elif action == "view":
        return store.view(target)
```

适配器模式——工具函数只做参数校验和 dispatch，实际操作委托给 `MemoryStore`。

## 关键设计点

### 两个目标轨道

- `memory`: Agent 笔记（MEMORY.md，上限 2200 chars）
- `user`: 用户画像（USER.md，上限 1375 chars）

### 操作立即持久化

add/update/delete 立即写盘，不等待 session 结束。但快照不变，system prompt 在本 session 内不变。

### 容量检查

每次 add/update 前检查新条目是否超限，超过则返回错误并提示当前使用量。

### 模块级单例

`get_store()` 维护全局唯一的 `MemoryStore` 实例，确保工具调用和 system prompt 使用同一份数据。

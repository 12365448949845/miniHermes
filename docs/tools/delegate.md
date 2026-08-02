# delegate_task — 子 Agent 委派

代码: `tools/delegate.py` (50行) + `agent/delegate.py` (148行)

---

## Schema 设计

```python
{
    "name": "delegate_task",
    "parameters": {
        "task": {"type": "string", "description": "Self-contained task description."},
        "context": {"type": "string", "description": "Optional additional context."}
    },
    "required": ["task"]
}
```

## 实现

### 工具层（占位）

```python
def delegate_task(task: str, context: str = "") -> str:
    return "Error: delegate_task must be executed within an Agent context."
```

工具函数永远返回错误——真正的执行在 `Agent._execute_tool()` 中拦截：

```python
if tool_name == "delegate_task":
    return run_delegate(
        DelegationRequest(task=args["task"], context=args.get("context", "")),
        self.provider
    ).response
```

### Agent 层（真正执行）

`run_delegate()` 创建完全隔离的子 Agent：

```python
def run_delegate(request, parent_provider):
    child_agent = Agent(
        provider=parent_provider,
        db=None,                       # 无持久化
        auto_approve=True,             # 免审批
        tool_filter={"exclude": ["delegate_task", "clarify"]},
        system_prompt_override=_CHILD_SYSTEM_PROMPT,
        max_iterations_override=15,    # 低预算
    )
    result = child_agent.run_conversation(request.task, [], renderer)
    return DelegationResult(
        success=True,
        response=result.final_response,
        iterations_used=budget.used,
        duration_seconds=elapsed,
    )
```

## 关键设计点

### 子 Agent 隔离

| 维度 | 限制 |
|------|------|
| 上下文 | 固定简化的 system prompt（无记忆/技能/项目上下文） |
| 工具 | 始终禁用 delegate_task + clarify |
| 持久化 | db=None，不写 DB |
| 审批 | auto_approve=True |
| 预算 | max_iterations=15（低于父的 30） |
| 历史 | 空列表（零上下文，除非通过 context 参数传入） |

### 同步阻塞

子 Agent 同步执行，父 Agent 等待完成。设计选择：简单 > 并发。

### 独立渲染

`SubagentRenderer` 提供缩进输出，在终端中视觉区分父子 Agent 的输出。

### 禁止递归委派

CHILD_BLOCKED_TOOLS 包含 `delegate_task`，防止子 Agent 再委派孙子 Agent。

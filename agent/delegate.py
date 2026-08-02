"""
子 Agent 委派引擎。

创建隔离的子 Agent 执行聚焦任务，同步阻塞直到完成，返回结果给父 Agent。
子 Agent 不持久化（无 DB 写入），纯内存执行完即丢弃。

扩展点（Phase 2+）：
  - run_delegate_batch(): 并行执行多个 DelegationRequest（ThreadPoolExecutor）
  - DelegationRequest.allow_nesting: 允许子 Agent 继续委派
  - DelegationRequest.model_override: 子 Agent 使用不同模型
  - DelegationRequest.timeout: 硬超时（threading + Event）
"""

import time
from dataclasses import dataclass, field
from typing import Optional

from provider import Provider
from renderer import SubagentRenderer


# ── 数据结构 ─────────────────────────────────────────────────────────────────

@dataclass
class DelegationRequest:
    """父 Agent 对子 Agent 的任务描述。"""

    task: str
    context: str = ""
    tools_exclude: Optional[set[str]] = None
    max_iterations: Optional[int] = None

    # Phase 2+ 扩展字段（预留，当前不使用）
    # tools_include: Optional[set[str]] = None
    # model_override: Optional[str] = None
    # timeout: Optional[float] = None
    # allow_nesting: bool = False


@dataclass
class DelegationResult:
    """子 Agent 的执行结果。"""

    success: bool
    response: str
    error: Optional[str] = None
    iterations_used: int = 0
    duration_seconds: float = 0.0


# ── 常量 ─────────────────────────────────────────────────────────────────────

CHILD_BLOCKED_TOOLS: frozenset[str] = frozenset({"delegate_task", "clarify"})

_CHILD_SYSTEM_PROMPT = (
    "You are a focused task executor. Complete the given task thoroughly and concisely.\n"
    "Rules:\n"
    "- Do NOT ask clarifying questions — work with what you have.\n"
    "- Do NOT attempt to delegate to other agents.\n"
    "- Use tools proactively to gather information and complete the task.\n"
    "- When done, provide a clear and complete answer summarizing your findings or actions."
)


# ── 核心执行 ─────────────────────────────────────────────────────────────────

def run_delegate(
    request: DelegationRequest,
    parent_provider: Provider,
) -> DelegationResult:
    """同步执行一个委派任务。

    创建子 Agent（隔离上下文、受限工具、无 DB），运行到完成并返回结果。
    子 Agent 复用父 Provider（相同模型和 API），auto_approve=True。

    Args:
        request: 委派请求描述
        parent_provider: 父 Agent 的 Provider 实例（复用）

    Returns:
        DelegationResult 包含成功状态和响应文本
    """
    from agent.agent import Agent

    start_time = time.time()

    # 构建工具过滤（合并默认 block + 额外排除）
    exclude = set(CHILD_BLOCKED_TOOLS)
    if request.tools_exclude:
        exclude |= request.tools_exclude

    # 子 Agent 迭代预算（硬编码，防止失控）
    max_iter = request.max_iterations or 50

    tool_filter = {"exclude": exclude}

    # 创建子 Agent（不持久化、不交互）
    child_agent = Agent(
        provider=parent_provider,
        db=None,
        clarify_callback=None,
        auto_approve=True,
        tool_filter=tool_filter,
        system_prompt_override=_CHILD_SYSTEM_PROMPT,
        max_iterations_override=max_iter,
    )

    # 构建用户消息
    if request.context:
        user_message = f"## Context\n\n{request.context}\n\n## Task\n\n{request.task}"
    else:
        user_message = request.task

    # 子 Agent 渲染器：展示工具调用过程
    renderer = SubagentRenderer(task_preview=request.task)

    try:
        result = child_agent.run_conversation(
            user_message=user_message,
            history=[],
            renderer=renderer,
            session_id=None,
        )

        duration = time.time() - start_time

        return DelegationResult(
            success=True,
            response=result.final_response or "(subagent produced no response)",
            iterations_used=child_agent._ctx.budget_used,
            duration_seconds=duration,
        )

    except Exception as e:
        duration = time.time() - start_time
        return DelegationResult(
            success=False,
            response="",
            error=f"{type(e).__name__}: {e}",
            duration_seconds=duration,
        )

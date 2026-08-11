"""Delegate 请求的规格与展示辅助。

Delegate 的生命周期由 :class:`AgentRuntimeManager` 管理。本模块只负责
描述委派请求、构造临时 Agent 的固定规格和结果数据结构，不能
直接创建 Agent 或绕过 Runtime 执行。
"""

from dataclasses import dataclass
from typing import Optional

from renderer import SubagentRenderer


@dataclass
class DelegationRequest:
    """父 Agent 对子 Agent 的任务描述。"""

    task: str
    context: str = ""
    tools: Optional[set[str]] = None
    tools_exclude: Optional[set[str]] = None
    max_iterations: Optional[int] = None


@dataclass
class DelegationResult:
    """兼容层数据结构；新的执行路径使用 AgentRunOutcome。"""

    success: bool
    response: str
    error: Optional[str] = None
    iterations_used: int = 0
    duration_seconds: float = 0.0


CHILD_BLOCKED_TOOLS: frozenset[str] = frozenset({"delegate_task", "clarify"})

_CHILD_SYSTEM_PROMPT = (
    "You are a focused task executor. Complete the given task thoroughly and concisely.\n"
    "Rules:\n"
    "- Do NOT ask clarifying questions — work with what you have.\n"
    "- Do NOT attempt to delegate to other agents.\n"
    "- Use tools proactively to gather information and complete the task.\n"
    "- When done, provide a clear and complete answer summarizing your findings or actions."
)


def build_delegate_spec(request: DelegationRequest):
    """构造 Delegate 的不可变 Runtime 规格。"""
    from agent.runtime import AgentSpec
    from approval import ApprovalMode

    excluded = set(CHILD_BLOCKED_TOOLS)
    if request.tools_exclude:
        excluded.update(request.tools_exclude)
    policy = {"exclude": excluded}
    if request.tools is not None:
        policy["include"] = set(request.tools)
    return AgentSpec(
        kind="delegate",
        system_prompt=_CHILD_SYSTEM_PROMPT,
        tool_policy=policy,
        approval_mode=ApprovalMode.INTERACTIVE,
        max_iterations=request.max_iterations or 50,
        persist_messages=False,
    )


def build_delegate_request(request: DelegationRequest, *, model: str = "") -> dict:
    """构造交给 Runtime 的临时请求，不保存完整正文到数据库。"""
    if request.context:
        user_message = (
            f"## Context\n\n{request.context}\n\n"
            f"## Task\n\n{request.task}"
        )
    else:
        user_message = request.task
    return {
        "task": request.task,
        "context": request.context,
        "user_message": user_message,
        "model": model,
    }


def build_delegate_renderer(request: DelegationRequest):
    """构造子 Agent 的轻量工具调用渲染器。"""
    return SubagentRenderer(task_preview=request.task)

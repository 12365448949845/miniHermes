"""Delegate 请求的规格与展示辅助。

Delegate 的生命周期由 :class:`AgentRuntimeManager` 管理。本模块只负责
描述委派请求、构造临时 Agent 的固定规格和结果数据结构，不能
直接创建 Agent 或绕过 Runtime 执行。
"""

from dataclasses import dataclass
from typing import Optional

from renderer import SubagentRenderer
from tools.registry import PARALLEL_SAFE_DELEGATE_TOOLS

from agent.worktree import (
    WORKTREE_MUTATING_DELEGATE_TOOLS,
    WORKTREE_WRITE_TOOLS,
)


@dataclass
class DelegationRequest:
    """父 Agent 对子 Agent 的任务描述。"""

    task: str
    context: str = ""
    tools: Optional[set[str]] = None
    tools_exclude: Optional[set[str]] = None
    max_iterations: Optional[int] = None
    execution_mode: Optional[str] = None
    write_scope: Optional[tuple[str, ...]] = None
    verification_hint: str = ""

    def __post_init__(self):
        if self.execution_mode not in {None, "read_only", "worktree_write"}:
            raise ValueError("execution_mode must be read_only or worktree_write")
        if self.execution_mode == "worktree_write":
            if not self.write_scope:
                raise ValueError("worktree_write requires a non-empty write_scope")
            if self.tools is not None and not self.tools <= WORKTREE_WRITE_TOOLS:
                raise ValueError("worktree_write requested a tool outside its fixed allowlist")
        elif self.write_scope:
            raise ValueError("write_scope is only valid for worktree_write")
        if (
            self.execution_mode != "worktree_write"
            and self.tools is not None
            and self.tools & WORKTREE_MUTATING_DELEGATE_TOOLS
        ):
            raise ValueError(
                "write_file and bash require execution_mode=worktree_write"
            )
        if self.execution_mode == "read_only" and self.tools is not None:
            if not self.tools <= PARALLEL_SAFE_DELEGATE_TOOLS:
                raise ValueError("read_only requested a tool with side effects")


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

_WORKTREE_SYSTEM_PROMPT = _CHILD_SYSTEM_PROMPT + (
    "\n\nExecution environment:\n"
    "- You are editing an isolated Git Worktree candidate.\n"
    "- Shell commands run in a Linux/POSIX Docker container at /workspace.\n"
    "- The container has no network and Git metadata is intentionally hidden.\n"
    "- Use relative workspace paths only. Do not run Git commands.\n"
    "- Only the frozen write scope may change; every command is audited.\n"
    "- The candidate is not merged into the user's main branch when you finish."
)


def build_delegate_spec(request: DelegationRequest):
    """构造 Delegate 的不可变 Runtime 规格。"""
    from agent.runtime import AgentSpec
    from approval import ApprovalMode

    excluded = set(CHILD_BLOCKED_TOOLS)
    if request.execution_mode != "worktree_write":
        # 省略 tools 时也必须 fail closed，不能让普通 Delegate 继承宿主写能力。
        excluded.update(WORKTREE_MUTATING_DELEGATE_TOOLS)
    if request.tools_exclude:
        excluded.update(request.tools_exclude)
    policy = {"exclude": excluded}
    approval_mode = ApprovalMode.INTERACTIVE
    system_prompt = _CHILD_SYSTEM_PROMPT
    if request.execution_mode == "worktree_write":
        policy["include"] = set(WORKTREE_WRITE_TOOLS)
        approval_mode = ApprovalMode.DENY_SENSITIVE
        system_prompt = _WORKTREE_SYSTEM_PROMPT
    elif request.execution_mode == "read_only":
        policy["include"] = set(
            request.tools
            if request.tools is not None
            else PARALLEL_SAFE_DELEGATE_TOOLS
        )
    elif request.tools is not None:
        policy["include"] = set(request.tools)
    return AgentSpec(
        kind="delegate",
        system_prompt=system_prompt,
        tool_policy=policy,
        approval_mode=approval_mode,
        max_iterations=request.max_iterations or 50,
        persist_messages=False,
    )


def build_delegate_request(request: DelegationRequest, *, model: str = "") -> dict:
    """构造交给 Runtime 的临时请求，不保存完整正文到数据库。"""
    sections = []
    if request.context:
        sections.append(f"## Context\n\n{request.context}")
    sections.append(f"## Task\n\n{request.task}")
    if request.verification_hint:
        sections.append(
            "## Suggested verification\n\n"
            f"{request.verification_hint}\n\n"
            "Treat this as a suggestion; only run it if it is valid in the isolated environment."
        )
    user_message = "\n\n".join(sections)
    return {
        "task": request.task,
        "context": request.context,
        "user_message": user_message,
        "model": model,
        "execution_mode": request.execution_mode,
        "write_scope": list(request.write_scope or ()),
        "verification_hint": request.verification_hint,
    }


def build_delegate_renderer(request: DelegationRequest):
    """构造子 Agent 的轻量工具调用渲染器。"""
    return SubagentRenderer(task_preview=request.task)

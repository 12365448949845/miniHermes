"""
后台 Nudge 系统：定期复盘对话，自动更新记忆和技能。

双独立计数器设计（参考 hermes run_agent.py）：
  - Memory nudge：每 N 轮用户对话触发，复盘用户偏好和环境事实
  - Skill nudge：每 N 次工具迭代触发，识别可复用的操作模式

Nudge agent 在后台 daemon 线程中运行，永不阻塞用户交互。
"""

import logging
from approval import ApprovalMode

logger = logging.getLogger(__name__)

MEMORY_NUDGE_PROMPT = """\
You are a background review agent. Your job is to analyze the conversation below \
and identify durable facts worth persisting to memory.

Focus on:
1. User preferences and working style (response length, language, formatting)
2. User background (role, tech stack, expertise level)
3. Environment facts (OS quirks, tool configurations, project conventions)
4. Corrections the user made to your approach

Use the memory tool to persist findings. Be ACTIVE — most sessions produce at least \
one memory update. A pass that does nothing is a missed learning opportunity.

Rules:
- Write memories as declarative facts: 'User prefers pytest over unittest' ✓
- Do NOT save task-specific or temporary information
- Do NOT save information already in memory (check with view first)
- Consolidate related facts into one entry rather than multiple

Conversation excerpt (last {n} messages):
{conversation_text}
"""

SKILL_NUDGE_PROMPT = """\
You are a background skill review agent. Your job is to analyze the conversation below \
and identify reusable patterns worth preserving as skills.

Signals that indicate a skill opportunity:
1. User corrected style/tone/format/workflow → propose a patch or a new skill
2. A non-trivial technique, fix, workaround, or debugging path emerged (5+ tool calls)
3. A loaded skill turned out wrong, incomplete, or outdated → describe the needed change
4. A pattern appeared that would clearly recur across projects

You may use skill_manage(action=list), skill_view, and read_file to inspect the
current library. Do not create, edit, patch, archive, restore, or write files.
Return a concrete proposal for a later reviewed workflow to apply.

Rules:
- Create CLASS-LEVEL umbrella skills, not narrow one-offs
- Prefer proposing an update to an existing skill over proposing an overlap
- Check existing skills with 'list' first to avoid duplicates
- Skills can have supporting files under references/, templates/, scripts/, assets/
- Include: When to Use, Procedure, Pitfalls, Verification sections
- Skills should be general enough to apply across projects

Conversation excerpt (last {n} messages):
{conversation_text}
"""


def _format_messages(messages: list[dict], n: int = 20) -> str:
    """格式化最近 N 条消息为可读文本。"""
    recent = messages[-n:] if len(messages) > n else messages
    lines = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if not content:
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                names = [tc.get("function", {}).get("name", "?") for tc in tool_calls]
                content = f"[called tools: {', '.join(names)}]"
            else:
                continue
        if isinstance(content, str) and len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"[{role}] {content}")
    return "\n\n".join(lines)


def build_nudge_specs(
    conversation_history: list[dict],
    nudge_type: str = "both",
    *,
    model: str = "",
) -> list[tuple[object, dict]]:
    """构造由 Runtime 提交的低优先级 Nudge 规格，不直接创建 Agent。"""
    from agent.runtime import AgentSpec

    text = _format_messages(conversation_history, n=20)
    if not text.strip():
        return []

    specs = []

    if nudge_type in ("memory", "both"):
        prompt = MEMORY_NUDGE_PROMPT.format(n=20, conversation_text=text)
        specs.append((
            AgentSpec(
                kind="memory_nudge",
                system_prompt=prompt,
                tool_policy={"include": {"memory"}},
                approval_mode=ApprovalMode.DENY_SENSITIVE,
                max_iterations=10,
                persist_messages=False,
                background=True,
            ),
            {
                "task": "Review durable user and environment memories.",
                "user_message": "Review the conversation and take action.",
                "model": model,
            },
        ))

    if nudge_type in ("skill", "both"):
        prompt = SKILL_NUDGE_PROMPT.format(n=20, conversation_text=text)
        specs.append((
            AgentSpec(
                kind="skill_nudge",
                system_prompt=prompt,
                tool_policy={"include": {"skill_manage", "skill_view", "read_file"}},
                approval_mode=ApprovalMode.DENY_SENSITIVE,
                max_iterations=10,
                persist_messages=False,
                background=True,
            ),
            {
                "task": "Review reusable skill opportunities.",
                "user_message": "Review the conversation and return a concrete proposal.",
                "model": model,
            },
        ))
    return specs


def submit_nudge(
    runtime,
    conversation_history: list[dict],
    nudge_type: str = "both",
    *,
    conversation_id: str | None = None,
    session_id: str | None = None,
    model: str = "",
    parent_tool_policy=None,
) -> list:
    """把 Nudge 登记到 Runtime 单 worker 队列。"""
    outcomes = []
    for spec, request in build_nudge_specs(
        conversation_history, nudge_type, model=model
    ):
        outcomes.append(runtime.submit_background(
            spec=spec,
            request=request,
            conversation_id=conversation_id,
            session_id=session_id,
            parent_tool_policy=parent_tool_policy,
        )
        )
    return outcomes


def spawn_nudge(runtime, conversation_history: list[dict], nudge_type: str = "both",
                **kwargs):
    """兼容入口：现在由 Runtime 管理，而非自行创建 daemon 线程。"""
    return submit_nudge(runtime, conversation_history, nudge_type, **kwargs)

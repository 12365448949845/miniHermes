"""
后台 Nudge 系统：定期复盘对话，自动更新记忆和技能。

双独立计数器设计（参考 hermes run_agent.py）：
  - Memory nudge：每 N 轮用户对话触发，复盘用户偏好和环境事实
  - Skill nudge：每 N 次工具迭代触发，识别可复用的操作模式

Nudge agent 在后台 daemon 线程中运行，永不阻塞用户交互。
"""

import logging
import threading
from typing import Optional

from agent.agent import Agent
from provider import Provider
from renderer import StreamRenderer

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
1. User corrected style/tone/format/workflow → patch or edit the loaded skill, or create new one
2. A non-trivial technique, fix, workaround, or debugging path emerged (5+ tool calls)
3. A loaded skill turned out wrong, incomplete, or outdated → patch or edit it
4. A pattern appeared that would clearly recur across projects

Available actions via skill_manage:
- create: create a new skill with name, description, body
- edit: full rewrite of an existing skill's body
- patch: targeted find-and-replace within a skill
- archive: move unused skill to _archived/
- restore: bring archived skill back to active
- list: view all skills with usage stats
- write_file: add supporting files (references/, templates/, scripts/, assets/)
- remove_file: delete supporting files

Rules:
- Create CLASS-LEVEL umbrella skills, not narrow one-offs
- Prefer patching/editing existing skills over creating overlapping new ones
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


def _run_nudge(provider: Provider, prompt: str, tool_filter: dict):
    """在当前线程中运行 nudge agent（由 daemon thread 调用）。"""
    try:
        max_iters = 10  # 后台 agent 安全预算，防止失控

        agent = Agent(
            provider=provider,
            db=None,
            clarify_callback=None,
            auto_approve=True,
            tool_filter=tool_filter,
            system_prompt_override=prompt,
            max_iterations_override=max_iters,
        )

        agent.run_conversation(
            user_message="Review the conversation and take action.",
            history=[],
            renderer=None,
            session_id=None,
        )
    except Exception as e:
        logger.debug(f"Nudge agent error (non-fatal): {e}")


def spawn_nudge(
    provider: Provider,
    conversation_history: list[dict],
    nudge_type: str = "both",
):
    """
    非阻塞：后台线程中运行 nudge agent(s)。

    Args:
        provider: LLM provider 实例
        conversation_history: 当前对话历史
        nudge_type: "memory" | "skill" | "both"
    """
    text = _format_messages(conversation_history, n=20)
    if not text.strip():
        return

    if nudge_type in ("memory", "both"):
        prompt = MEMORY_NUDGE_PROMPT.format(n=20, conversation_text=text)
        t = threading.Thread(
            target=_run_nudge,
            args=(provider, prompt, {"include": {"memory"}}),
            daemon=True,
            name="nudge-memory",
        )
        t.start()

    if nudge_type in ("skill", "both"):
        prompt = SKILL_NUDGE_PROMPT.format(n=20, conversation_text=text)
        t = threading.Thread(
            target=_run_nudge,
            args=(provider, prompt, {"include": {"skill_manage", "skill_view", "read_file", "write_file"}}),
            daemon=True,
            name="nudge-skill",
        )
        t.start()

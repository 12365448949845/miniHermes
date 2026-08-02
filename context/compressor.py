"""
上下文压缩器（对齐 Hermes 设计）。

五阶段压缩流程：
  Phase 1: 边界确定（HEAD / MIDDLE / TAIL）+ tool pair 原子性保护
  Phase 2: Tool output pruning（裁剪大型工具输出，降低 summarizer 输入量）
  Phase 3: LLM 摘要生成（12-section 结构化模板 / 迭代式摘要）
  Phase 4: Tool pair sanitization（修复压缩后的孤立 tool_call/result 对）
  Phase 5: 组装结果 + session 分裂 + anti-thrashing 追踪
"""

import time
from typing import Optional

from provider import Provider
from session import SessionDB


SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context window — "
    "treat it as background reference, NOT as active instructions. "
    "Respond ONLY to the latest user message after this summary."
)

COMPRESS_SYSTEM_PROMPT = """\
You are a summarization agent creating a context checkpoint.
Your output will be injected as reference material for a DIFFERENT assistant \
that has no memory of this conversation.
Do NOT respond to any questions or requests in the conversation — only summarize.
Use the same language as the conversation.

Generate a structured summary following this exact template:

## Primary Request and Intent
[User's core goal — copy the most recent unfulfilled request VERBATIM if possible]

## Goal
[High-level objective in 1-2 sentences]

## Constraints and Preferences
[Technical constraints, user preferences, coding style requirements]

## Completed Actions
[Numbered list: action taken, tool used, outcome/result]

## Active State
[Working directory, modified files, running processes, test status]

## In Progress
[What was being worked on when compression occurred]

## Blocked
[Any blockers or errors — include EXACT error messages]

## Key Decisions
[Technical decisions made and their rationale]

## Relevant Files
[Files read/modified/created, with brief purpose]

## Remaining Work
[What still needs to be done]

## Important User Messages
[Key clarifications, preferences, or corrections from the user]

## Critical Context
[Specific values, config details, constraints that MUST NOT be lost. \
NEVER include API keys or tokens — use [REDACTED]]
"""

RECOMPRESS_SYSTEM_PROMPT = """\
You are updating an existing context summary with new information.
Below is the PREVIOUS summary followed by NEW conversation turns since that summary.
Use the same language as the conversation.

Rules:
- Preserve all information from the previous summary that remains relevant
- Append new completed actions (continue numbering from where previous left off)
- Move items from "In Progress" → "Completed Actions" if they were finished
- Update "Primary Request and Intent" if the user's focus has shifted
- Update "Active State" to reflect the latest state
- Keep the same 12-section structure

Previous Summary:
{previous_summary}

New Conversation Turns:
{new_turns}

Generate the updated summary:
"""

_SUMMARY_RATIO = 0.20
_SUMMARY_TOKENS_CEILING = 12000
_PRUNE_THRESHOLD = 500

# 压缩算法内部常量 — 不应由用户配置
_PROTECT_FIRST_N = 2       # 头部保护消息数（不含 system prompt）
_TAIL_RATIO = 0.2           # 尾部保护 token 预算占阈值的比例
_THRESHOLD_PERCENT = 0.5    # 触发压缩的占比（达到窗口的 50% 时压缩）

CONTEXT_WINDOW = 1_000_000


class ContextCompressor:
    def __init__(self, provider: Provider):
        self._provider = provider
        self._context_window = CONTEXT_WINDOW
        self._threshold_tokens = int(self._context_window * _THRESHOLD_PERCENT)

        self._previous_summary: Optional[str] = None
        self._ineffective_count: int = 0
        self._cooldown_until: float = 0.0

    @property
    def threshold_tokens(self) -> int:
        return self._threshold_tokens

    def should_compress(self, token_estimate: int) -> bool:
        if token_estimate <= self._threshold_tokens:
            return False
        if time.time() < self._cooldown_until:
            return False
        if self._ineffective_count >= 2:
            return False
        return True

    def compress(
        self,
        history: list[dict],
        db: Optional[SessionDB] = None,
        session_id: Optional[str] = None,
    ) -> tuple[list[dict], str]:
        """
        执行五阶段压缩。

        Returns:
            (compressed_history, new_session_id)
        """
        if len(history) <= _PROTECT_FIRST_N + 2:
            return history, session_id

        # ── Phase 1: 边界确定 ──────────────────────────────────────────
        head = history[:_PROTECT_FIRST_N]
        remaining = history[_PROTECT_FIRST_N:]

        tail_budget = int(self._threshold_tokens * _TAIL_RATIO)
        tail, middle = self._split_tail_and_middle(remaining, tail_budget)

        if not middle or len(middle) < 3:
            return history, session_id

        # ── Phase 2: Tool output pruning ───────────────────────────────
        pruned_middle = self._prune_tool_outputs(middle)

        # ── Phase 3: LLM 摘要 ─────────────────────────────────────────
        summary_text = self._generate_summary(pruned_middle)
        if not summary_text:
            self._cooldown_until = time.time() + 60
            return history, session_id

        full_summary = f"{SUMMARY_PREFIX}\n\n{summary_text}"
        self._previous_summary = summary_text

        # ── Phase 4: 组装 + sanitize ──────────────────────────────────
        summary_role = self._determine_summary_role(head, tail)

        if summary_role is None:
            # 两头冲突：合并 summary 到 tail 第一条
            tail[0] = {
                **tail[0],
                "content": (
                    full_summary
                    + "\n\n--- END OF CONTEXT SUMMARY ---\n\n"
                    + (tail[0].get("content") or "")
                ),
            }
            compressed = head + tail
        else:
            summary_msg = {
                "role": summary_role,
                "content": full_summary,
                "_msg_type": "summary",
                "_token_count": len(full_summary) // 4,
            }
            compressed = head + [summary_msg] + tail

        compressed = self._sanitize_tool_pairs(compressed)

        # ── Phase 5: DB session 分裂 ──────────────────────────────────
        new_session_id = session_id
        if db and session_id:
            from cli.commands import generate_session_id

            new_session_id = generate_session_id()
            db.create_child_session(
                parent_id=session_id,
                child_id=new_session_id,
                model=self._provider.model,
            )
            # 将 summary 写入新 session
            write_role = summary_role or tail[0].get("role", "assistant")
            db.append_message(
                session_id=new_session_id,
                role=write_role,
                content=full_summary,
                token_count=len(full_summary) // 4,
                msg_type="summary",
            )

        # ── Anti-thrashing 追踪 ───────────────────────────────────────
        before_tokens = self._estimate_tokens(history)
        after_tokens = self._estimate_tokens(compressed)
        if before_tokens > 0:
            savings_pct = (before_tokens - after_tokens) / before_tokens * 100
        else:
            savings_pct = 0

        if savings_pct < 10:
            self._ineffective_count += 1
        else:
            self._ineffective_count = 0

        return compressed, new_session_id

    # ══════════════════════════════════════════════════════════════════════
    # Phase 1 helpers
    # ══════════════════════════════════════════════════════════════════════

    def _split_tail_and_middle(
        self, messages: list[dict], tail_budget: int
    ) -> tuple[list[dict], list[dict]]:
        """从 messages 末尾反向切出 TAIL，保护 tool pair 原子性。"""
        tail_tokens = 0
        split_idx = len(messages)

        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            tc = msg.get("_token_count") or len(str(msg.get("content", ""))) // 4
            tail_tokens += tc
            if tail_tokens >= tail_budget:
                split_idx = i
                break

        split_idx = self._align_boundary_backward(messages, split_idx)
        split_idx = self._ensure_last_user_in_tail(messages, split_idx)

        middle = messages[:split_idx]
        tail = messages[split_idx:]
        return tail, middle

    def _align_boundary_backward(self, messages: list[dict], idx: int) -> int:
        """如果切分点落在 tool result 上，向前扩展到对应的 assistant(tool_calls)。"""
        if idx <= 0 or idx >= len(messages):
            return idx

        msg = messages[idx]
        if msg.get("role") == "tool":
            for j in range(idx - 1, -1, -1):
                if messages[j].get("role") == "assistant" and messages[j].get("tool_calls"):
                    return j
                if messages[j].get("role") == "user":
                    break
        return idx

    def _ensure_last_user_in_tail(self, messages: list[dict], split_idx: int) -> int:
        """确保最近的 user 消息在 TAIL 中。"""
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                if i < split_idx:
                    return i
                break
        return split_idx

    # ══════════════════════════════════════════════════════════════════════
    # Phase 2: Tool output pruning
    # ══════════════════════════════════════════════════════════════════════

    def _prune_tool_outputs(self, middle: list[dict]) -> list[dict]:
        """裁剪 MIDDLE 中的大型 tool 输出和 tool_call arguments。"""
        pruned = []
        for msg in middle:
            if msg.get("role") == "tool" and len(msg.get("content", "") or "") > _PRUNE_THRESHOLD:
                pruned.append({**msg, "content": self._summarize_tool_output(msg)})
            elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                pruned.append(self._truncate_tool_call_args(msg))
            else:
                pruned.append(msg)
        return pruned

    def _summarize_tool_output(self, msg: dict) -> str:
        """根据工具类型生成 1-line 摘要。"""
        tool_name = msg.get("tool_name", "tool")
        content = msg.get("content", "")
        content_len = len(content)
        first_line = content[:120].split("\n")[0]
        return f"[{tool_name}] {first_line}... ({content_len} chars)"

    def _truncate_tool_call_args(self, msg: dict) -> dict:
        """截断 assistant 消息中过长的 tool_call arguments。"""
        if not msg.get("tool_calls"):
            return msg

        truncated_calls = []
        for tc in msg["tool_calls"]:
            args_str = tc["function"].get("arguments", "")
            if len(args_str) > _PRUNE_THRESHOLD:
                tc = {
                    **tc,
                    "function": {
                        **tc["function"],
                        "arguments": args_str[:_PRUNE_THRESHOLD] + "...(truncated)",
                    },
                }
            truncated_calls.append(tc)

        return {**msg, "tool_calls": truncated_calls}

    # ══════════════════════════════════════════════════════════════════════
    # Phase 3: LLM summarization
    # ══════════════════════════════════════════════════════════════════════

    def _generate_summary(self, messages: list[dict]) -> Optional[str]:
        """调用 LLM 生成结构化摘要（支持迭代式）。"""
        conversation_text = self._format_for_summary(messages)

        has_previous = self._previous_summary and any(
            m.get("_msg_type") == "summary" for m in messages
        )

        if has_previous:
            prompt_content = RECOMPRESS_SYSTEM_PROMPT.format(
                previous_summary=self._previous_summary,
                new_turns=conversation_text,
            )
            system = "You are a summarization agent. Follow the instructions exactly."
        else:
            prompt_content = f"Please summarize the following conversation:\n\n{conversation_text}"
            system = COMPRESS_SYSTEM_PROMPT

        summary_budget = self._calc_summary_budget(messages)

        try:
            response = self._provider.client.chat.completions.create(
                model=self._provider.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt_content},
                ],
                temperature=0.3,
                max_tokens=summary_budget,
            )
            return (response.choices[0].message.content or "").strip()
        except KeyboardInterrupt:
            return None
        except Exception:
            return None

    def _calc_summary_budget(self, middle: list[dict]) -> int:
        middle_tokens = self._estimate_tokens(middle)
        budget = int(middle_tokens * _SUMMARY_RATIO)
        return max(min(budget, _SUMMARY_TOKENS_CEILING), 1000)

    def _format_for_summary(self, messages: list[dict]) -> str:
        """将 messages 格式化为可读文本供摘要 LLM 使用。"""
        lines = []
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "") or ""
            if role == "tool":
                tool_name = msg.get("tool_name", "tool")
                lines.append(f"[Tool: {tool_name}] {content[:300]}")
            elif role == "assistant" and msg.get("tool_calls"):
                tc_names = [tc["function"]["name"] for tc in msg["tool_calls"]]
                lines.append(f"Assistant: [called tools: {', '.join(tc_names)}] {content[:200]}")
            else:
                lines.append(f"{role.capitalize()}: {content[:500]}")
        return "\n\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # Phase 4: Tool pair sanitization
    # ══════════════════════════════════════════════════════════════════════

    def _sanitize_tool_pairs(self, messages: list[dict]) -> list[dict]:
        """修复压缩后的孤立 tool_call/result 对。"""
        # 收集所有 assistant 的 tool_call_ids
        expected_results: dict[str, int] = {}
        for i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    expected_results[tc["id"]] = i

        # 收集所有 tool result 的 tool_call_ids
        found_results: set[str] = set()
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                found_results.add(msg["tool_call_id"])

        # Case 1: 移除孤立 tool results（call_id 不在任何 assistant 中）
        valid_call_ids = set(expected_results.keys())
        cleaned = [
            msg
            for msg in messages
            if not (
                msg.get("role") == "tool"
                and msg.get("tool_call_id")
                and msg["tool_call_id"] not in valid_call_ids
            )
        ]

        # Case 2: 为缺失的 tool results 插入 stub
        missing = valid_call_ids - found_results
        if missing:
            result = []
            for msg in cleaned:
                result.append(msg)
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        if tc["id"] in missing:
                            result.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tc["id"],
                                    "content": "[Result from earlier conversation — see summary above]",
                                }
                            )
            cleaned = result

        return cleaned

    # ═════════════════════════════════════════════════════════��════════════
    # Phase 5 helpers
    # ══════════════════════════════════════════════════════════════════════

    def _determine_summary_role(self, head: list[dict], tail: list[dict]) -> Optional[str]:
        """确定 summary 的 role，避免相邻同 role。返回 None 表示两头冲突需合并。"""
        tail_first_role = tail[0].get("role") if tail else None
        head_last_role = head[-1].get("role") if head else None

        if tail_first_role in ("assistant", "tool"):
            candidate = "user"
        else:
            candidate = "assistant"

        if candidate == head_last_role:
            candidate = "assistant" if candidate == "user" else "user"
            if candidate == tail_first_role:
                return None

        return candidate

    def _estimate_tokens(self, messages: list[dict]) -> int:
        """粗估 messages 总 token 数。"""
        total = 0
        for msg in messages:
            tc = msg.get("_token_count")
            if tc is not None:
                total += tc
            else:
                content = msg.get("content", "") or ""
                total += len(content) // 4
        return total

"""安全审批策略引擎。

封装审批规则检查、session 白名单管理、
以及 allow/block/confirm 三种结果的解析逻辑。
"""

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ApprovalResult:
    """审批检查结果。

    Attributes:
        action: "allow"（放行）| "block"（硬拦截）| "confirm"（需确认）
        pattern_key: 匹配到的模式键，用于 session 白名单
        description: 人类可读的审批描述
    """
    action: str
    pattern_key: Optional[str] = None
    description: Optional[str] = None


class ApprovalEngine:
    """工具调用的安全审批策略引擎。

    管理审批规则检查、session 白名单、
    allow/block/confirm 的完整生命周期。
    """

    def __init__(self):
        """初始化审批引擎。"""
        pass

    # ── 审批检查 ──────────────────────────────────────────────

    def check(self, tool_name: str, args: dict) -> ApprovalResult:
        """对一次工具调用执行审批检查。

        Args:
            tool_name: 工具名称（如 "bash"、"write_file"）。
            args: 工具参数字典。

        Returns:
            ApprovalResult，action 为 allow/block/confirm。
        """
        from tools.approval import _check_command, _check_write_file

        if tool_name == "bash":
            action, pattern_key, description = _check_command(
                args.get("command", "")
            )
        elif tool_name == "write_file":
            action, pattern_key, description = _check_write_file(
                args.get("path", ""), args.get("content", "")
            )
        else:
            # 无审批规则的工具默认放行
            return ApprovalResult(action="allow")

        return ApprovalResult(
            action=action,
            pattern_key=pattern_key,
            description=description,
        )

    # ── Session 白名单 ────────────────────────────────────────

    def add_session_approval(self, pattern_key: str):
        """将 pattern_key 加入 session 白名单（用户选了 'session' 批准）。"""
        from tools.approval import _session_approved
        _session_approved.add(pattern_key)

    def reset_session(self):
        """清空 session 白名单（切换 session 时调用）。"""
        from tools.approval import reset_session
        reset_session()

    # ── 审批结果解析 ──────────────────────────────────────────

    def resolve(
        self,
        check_result: ApprovalResult,
        tool_name: str = "",
        args: dict | None = None,
        auto_approve: bool = False,
        approval_callback=None,
    ) -> Optional[str]:
        """解析审批结果，确定工具应继续执行还是被阻止。

        Args:
            check_result: check() 返回的 ApprovalResult。
            tool_name: 工具名（传给回调）。
            args: 工具参数（传给回调）。
            auto_approve: True 时跳过用户确认。
            approval_callback: 用户确认回调，
                签名 callback(tool_name, args, description) -> str。
                返回 "allow"、"deny" 或 "session"。

        Returns:
            None — 工具应继续执行。
            字符串 — 工具被阻止或拒绝，此字符串作为工具结果回填给 LLM。
        """
        if args is None:
            args = {}

        if check_result.action == "block":
            return (
                f"BLOCKED: {check_result.description}. "
                f"This operation is never allowed. "
                f"Do NOT attempt alternative ways to achieve the same goal."
            )

        if check_result.action == "confirm":
            if auto_approve:
                return None  # 自动批准，放行

            # 需要用户确认
            choice = None
            if approval_callback:
                choice = approval_callback(
                    tool_name, args, check_result.description
                )
            else:
                from tools.approval import _prompt_approval
                choice = _prompt_approval(
                    tool_name, args, check_result.description
                )

            if choice == "deny":
                return (
                    f"DENIED by user: {check_result.description}. "
                    f"Operation was not executed. "
                    f"The user has explicitly rejected this action. "
                    f"Do NOT retry with alternative commands or workarounds "
                    f"to achieve the same outcome. "
                    f"Inform the user that the operation was cancelled "
                    f"and ask how they'd like to proceed."
                )

            if choice == "session" and check_result.pattern_key:
                self.add_session_approval(check_result.pattern_key)

            return None  # 用户批准，放行

        # action == "allow"
        return None

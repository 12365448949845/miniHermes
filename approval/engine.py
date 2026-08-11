"""安全审批策略与按逻辑会话隔离的授权状态。"""

from dataclasses import dataclass
from enum import Enum
import inspect
from typing import Optional
import warnings


class ApprovalMode(str, Enum):
    INTERACTIVE = "interactive"
    DENY_SENSITIVE = "deny_sensitive"
    TRUSTED = "trusted"

    @classmethod
    def coerce(cls, value=None, *, auto_approve: bool | None = None):
        if auto_approve is not None:
            warnings.warn(
                "auto_approve is deprecated; use ApprovalMode explicitly",
                DeprecationWarning,
                stacklevel=2,
            )
            return cls.DENY_SENSITIVE if auto_approve else cls.INTERACTIVE
        if value is None:
            return cls.INTERACTIVE
        if isinstance(value, cls):
            return value
        coerced = cls(str(value))
        if coerced == cls.TRUSTED:
            raise ValueError(
                "ApprovalMode.TRUSTED must be passed as an explicit enum value"
            )
        return coerced


@dataclass(frozen=True)
class ApprovalResult:
    action: str
    pattern_key: Optional[str] = None
    description: Optional[str] = None


@dataclass(frozen=True)
class ApprovalResolution:
    allowed: bool
    model_output: str = ""
    error_code: str | None = None
    description: str | None = None


class ApprovalEngine:
    """HARDLINE 永久阻止，confirm 按 ApprovalMode 解析。"""

    def __init__(self):
        self._conversation_approved: dict[str, set[str]] = {}

    def _approved_for(self, conversation_id: str) -> set[str]:
        return self._conversation_approved.setdefault(conversation_id or "", set())

    def check(
        self,
        tool_name: str,
        args: dict,
        *,
        conversation_id: str = "",
    ) -> ApprovalResult:
        from tools.approval import _check_command, _check_write_file

        approved = self._approved_for(conversation_id)
        if tool_name == "bash":
            action, pattern_key, description = _check_command(
                args.get("command", ""), approved=approved
            )
        elif tool_name == "write_file":
            action, pattern_key, description = _check_write_file(
                args.get("path", ""),
                args.get("content", ""),
                approved=approved,
            )
        else:
            return ApprovalResult(action="allow")
        return ApprovalResult(action, pattern_key, description)

    def add_session_approval(self, conversation_id: str, pattern_key: str):
        self._approved_for(conversation_id).add(pattern_key)

    def reset_session(self, conversation_id: str | None = None):
        if conversation_id is None:
            self._conversation_approved.clear()
        else:
            self._conversation_approved.pop(conversation_id, None)

    def resolve(
        self,
        check_result: ApprovalResult,
        tool_name: str = "",
        args: dict | None = None,
        *,
        mode: ApprovalMode | str | None = None,
        approval_callback=None,
        conversation_id: str = "",
        auto_approve: bool | None = None,
        run_context=None,
    ) -> ApprovalResolution:
        args = args or {}
        approval_mode = ApprovalMode.coerce(mode, auto_approve=auto_approve)

        if check_result.action == "block":
            description = check_result.description or "operation is permanently blocked"
            return ApprovalResolution(
                allowed=False,
                model_output=(
                    f"BLOCKED: {description}. This operation is never allowed. "
                    "Do NOT attempt alternative ways to achieve the same goal."
                ),
                error_code="tool_not_allowed",
                description=description,
            )

        if check_result.action != "confirm":
            return ApprovalResolution(allowed=True)

        description = check_result.description or "sensitive operation"
        if approval_mode == ApprovalMode.TRUSTED:
            return ApprovalResolution(allowed=True)

        if approval_mode == ApprovalMode.DENY_SENSITIVE:
            return ApprovalResolution(
                allowed=False,
                model_output=(
                    f"DENIED by approval policy: {description}. "
                    "Operation was not executed because this Agent cannot request "
                    "interactive approval."
                ),
                error_code="approval_denied",
                description=description,
            )

        if approval_callback:
            try:
                inspect.signature(approval_callback).bind(
                    tool_name, args, description, run_context=run_context
                )
            except (TypeError, ValueError):
                choice = approval_callback(tool_name, args, description)
            else:
                choice = approval_callback(
                    tool_name, args, description, run_context=run_context
                )
        else:
            from tools.approval import _prompt_approval
            choice = _prompt_approval(tool_name, args, description)

        if choice == "deny":
            return ApprovalResolution(
                allowed=False,
                model_output=(
                    f"DENIED by user: {description}. Operation was not executed. "
                    "The user explicitly rejected this action. Do NOT retry through "
                    "alternative commands or workarounds."
                ),
                error_code="approval_denied",
                description=description,
            )

        if choice not in ("once", "session"):
            return ApprovalResolution(
                allowed=False,
                model_output=(
                    f"DENIED by user: {description}. Operation was not executed "
                    "because approval was not granted."
                ),
                error_code="approval_denied",
                description=description,
            )

        if choice == "session" and check_result.pattern_key:
            self.add_session_approval(conversation_id, check_result.pattern_key)
        return ApprovalResolution(allowed=True)

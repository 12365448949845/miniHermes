"""结构化工具重试。

重试只依赖明确的工具适配器和 ToolMetadata，不再扫描任意业务正文猜测错误。
"""

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional


RETRY_MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 2.0


class ErrorClass:
    """旧调用方兼容常量。"""

    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class ToolAttemptResult:
    status: str
    output: str
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


@dataclass(frozen=True)
class RetryOutcome:
    status: str
    output: str
    model_output: str
    error_code: str | None
    error_message: str | None
    retryable: bool
    attempts: int
    duration_seconds: float
    side_effects_possible: bool


_BASH_TIMEOUT = re.compile(r"^Error: command timed out after \d+(?:\.\d+)?s$")
_WEB_EXTRACT_HTTP = re.compile(r"^Error: HTTP (\d{3})\b")
_LEGACY_ERROR_PREFIXES = (
    "Error:",
    "ERROR:",
    "Error executing tool",
    "BLOCKED:",
    "DENIED",
)


def _typed_adapter(tool_name: str, output: str) -> ToolAttemptResult:
    """仅对三个已迁移工具按其受控返回格式分类。"""
    if tool_name == "bash":
        if _BASH_TIMEOUT.match(output):
            return ToolAttemptResult(
                "FAILED", output, "timeout", output, retryable=False
            )
        if output.startswith("Error:"):
            return ToolAttemptResult(
                "FAILED", output, "permanent_failure", output
            )
        return ToolAttemptResult("SUCCEEDED", output)

    if tool_name == "web_search":
        lowered = output.lower()
        if output.startswith("Error: Exa API rate limit"):
            return ToolAttemptResult(
                "FAILED", output, "rate_limited", output, retryable=True
            )
        if output.startswith("Error: Exa search failed:") and any(
            marker in lowered
            for marker in (
                "timeout", "timed out", "connection", "temporarily unavailable",
                "502", "503", "504",
            )
        ):
            return ToolAttemptResult(
                "FAILED", output, "network_transient", output, retryable=True
            )
        if output.startswith("Error:"):
            return ToolAttemptResult(
                "FAILED", output, "permanent_failure", output
            )
        return ToolAttemptResult("SUCCEEDED", output)

    if tool_name == "web_extract":
        lowered = output.lower()
        if output.startswith("Error: request timed out"):
            return ToolAttemptResult(
                "FAILED", output, "timeout", output, retryable=True
            )
        match = _WEB_EXTRACT_HTTP.match(output)
        if match:
            status_code = int(match.group(1))
            if status_code == 429:
                return ToolAttemptResult(
                    "FAILED", output, "rate_limited", output, retryable=True
                )
            if status_code >= 500:
                return ToolAttemptResult(
                    "FAILED", output, "network_transient", output, retryable=True
                )
            return ToolAttemptResult(
                "FAILED", output, "permanent_failure", output
            )
        if output.startswith("Error:"):
            retryable = any(
                marker in lowered
                for marker in ("connection", "temporarily unavailable", "reset")
            )
            return ToolAttemptResult(
                "FAILED",
                output,
                "network_transient" if retryable else "permanent_failure",
                output,
                retryable=retryable,
            )
        return ToolAttemptResult("SUCCEEDED", output)

    if output.startswith(_LEGACY_ERROR_PREFIXES):
        return ToolAttemptResult(
            "FAILED",
            output,
            "legacy_reported_error",
            output,
            retryable=False,
        )
    return ToolAttemptResult("SUCCEEDED", output)


def _execute_once(fn, args: dict, tool_name: str) -> ToolAttemptResult:
    from tools import truncate_output

    try:
        value = fn(**args)
        output = truncate_output(str(value) if value is not None else "(no output)")
    except Exception as exc:
        if getattr(exc, "is_run_control", False):
            raise
        if isinstance(exc, TypeError):
            output = f"Error: invalid arguments: {exc}"
            return ToolAttemptResult(
                "FAILED", output, "invalid_arguments", str(exc), retryable=False
            )
        output = f"Error: {type(exc).__name__}: {exc}"
        return ToolAttemptResult(
            "FAILED", output, "internal_error", output, retryable=False
        )
    return _typed_adapter(tool_name, output)


def _interruptible_sleep(
    seconds: float,
    cancel_check: Optional[Callable[[], bool]],
) -> bool:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if cancel_check and cancel_check():
            return True
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    return False


def execute_with_retry_detailed(
    fn,
    args: dict,
    tool_name: str,
    *,
    retry_policy: str = "never",
    side_effect: str = "unknown",
    cancel_check: Optional[Callable[[], bool]] = None,
    on_retry: Optional[Callable[[int, str, float], None]] = None,
    max_retries: int = RETRY_MAX_RETRIES,
) -> RetryOutcome:
    """执行工具，并依据结构化错误和元数据决定是否重试。"""
    started = time.monotonic()
    attempts = 0
    last = ToolAttemptResult(
        "FAILED", "Error: tool was not executed", "internal_error"
    )

    max_attempts = max(1, max_retries + 1)
    for attempt in range(1, max_attempts + 1):
        if cancel_check and cancel_check():
            output = "CANCELLED: Agent run was interrupted before the tool started."
            return RetryOutcome(
                "CANCELLED", output, output, "cancelled", output, False,
                attempts, time.monotonic() - started, False,
            )

        attempts = attempt
        last = _execute_once(fn, args, tool_name)
        if last.status == "SUCCEEDED":
            model_output = last.output
            if attempts > 1:
                model_output = (
                    f"[Retried: succeeded on attempt {attempts}]\n\n{model_output}"
                )
            return RetryOutcome(
                "SUCCEEDED",
                last.output,
                model_output,
                None,
                None,
                False,
                attempts,
                time.monotonic() - started,
                False,
            )

        can_retry = (
            last.retryable
            and retry_policy in ("transient", "idempotent")
            and side_effect == "none"
            and attempt < max_attempts
        )
        if not can_retry:
            break

        delay = RETRY_DELAY_SECONDS
        if on_retry:
            on_retry(attempt + 1, last.error_code or "unknown", delay)
        if _interruptible_sleep(delay, cancel_check):
            output = "CANCELLED: Agent run was interrupted during tool retry wait."
            return RetryOutcome(
                "CANCELLED", output, output, "cancelled", output, False,
                attempts, time.monotonic() - started, False,
            )

    model_output = last.output
    if attempts > 1:
        model_output = f"[Retried {attempts - 1} times, all failed]\n{model_output}"
    return RetryOutcome(
        last.status,
        last.output,
        model_output,
        last.error_code,
        last.error_message,
        last.retryable,
        attempts,
        time.monotonic() - started,
        side_effect != "none",
    )


def execute_with_retry(fn, args: dict, tool_name: str) -> str:
    """旧字符串接口；新 Agent 路径使用 execute_with_retry_detailed。"""
    retry_policy = "transient" if tool_name in {"web_search", "web_extract"} else "never"
    side_effect = "none" if tool_name in {"web_search", "web_extract"} else "unknown"
    return execute_with_retry_detailed(
        fn,
        args,
        tool_name,
        retry_policy=retry_policy,
        side_effect=side_effect,
    ).model_output


def register_retry_modifier(tool_name: str, modifier: callable):
    """旧扩展点已停用；保留函数避免第三方导入失败。"""
    return None

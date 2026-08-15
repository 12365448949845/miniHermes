"""结构化工具重试。

重试只依赖明确的工具适配器和 ToolMetadata，不再扫描任意业务正文猜测错误。
"""

import math
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional


RETRY_MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 2.0
RETRY_MAX_DELAY_SECONDS = 60.0
RETRY_TOTAL_BUDGET_SECONDS = 120.0


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
    retry_after_seconds: float | None = None


class TypedToolOutput(str):
    """兼容字符串返回值的受控工具适配器结果。"""

    def __new__(
        cls,
        output: str,
        *,
        status: str = "FAILED",
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ):
        value = super().__new__(cls, output)
        value.status = status
        value.error_code = error_code
        value.error_message = error_message
        value.retryable = bool(retryable)
        value.retry_after_seconds = retry_after_seconds
        return value


def parse_retry_after(value, *, now: datetime | None = None) -> float | None:
    """只解析可信响应头中的 Retry-After 秒数或 HTTP 日期。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(text)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            if current.tzinfo is None:
                current = current.replace(tzinfo=timezone.utc)
            seconds = (target - current).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return min(seconds, RETRY_MAX_DELAY_SECONDS)


def trusted_tool_failure(
    output: str,
    error_code: str,
    *,
    retryable: bool = False,
    retry_after=None,
) -> TypedToolOutput:
    """供内置工具适配器返回稳定错误码，同时保持 str 兼容。"""
    return TypedToolOutput(
        output,
        error_code=error_code,
        error_message=output,
        retryable=retryable,
        retry_after_seconds=parse_retry_after(retry_after),
    )


def trusted_tool_cancelled(output: str) -> TypedToolOutput:
    return TypedToolOutput(
        output,
        status="CANCELLED",
        error_code="cancelled",
        error_message=output,
    )


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
_BASH_WORKSPACE_ERROR = re.compile(
    r"^Error: (scope_violation|candidate_audit_failed|runner_failed|"
    r"docker_unavailable|docker_spawn_failed|workspace_mount_unavailable):"
)
_BASH_NONZERO_EXIT = re.compile(r"(?:^|\n)\[exit code: (-?\d+)\]\s*$")
_WEB_EXTRACT_HTTP = re.compile(r"^Error: HTTP (\d{3})\b")
_LEGACY_ERROR_PREFIXES = (
    "Error:",
    "ERROR:",
    "Error executing tool",
    "BLOCKED:",
    "DENIED",
)
_RETRYABLE_ERROR_CODES = frozenset({
    "network_transient", "rate_limited", "timeout",
    "resource_busy", "lock_conflict",
})


def _typed_adapter(tool_name: str, output: str) -> ToolAttemptResult:
    """仅对三个已迁移工具按其受控返回格式分类。"""
    if tool_name == "bash":
        if _BASH_TIMEOUT.match(output):
            return ToolAttemptResult(
                "FAILED", output, "timeout", output, retryable=False
            )
        workspace_error = _BASH_WORKSPACE_ERROR.match(output)
        if workspace_error:
            return ToolAttemptResult(
                "FAILED", output, workspace_error.group(1), output, retryable=False
            )
        nonzero_exit = _BASH_NONZERO_EXIT.search(output)
        if nonzero_exit and int(nonzero_exit.group(1)) != 0:
            return ToolAttemptResult(
                "FAILED", output, "nonzero_exit", output, retryable=False
            )
        if output.startswith("Error:"):
            return ToolAttemptResult(
                "FAILED", output, "permanent_failure", output
            )
        return ToolAttemptResult("SUCCEEDED", output)

    if tool_name == "web_search":
        lowered = output.lower()
        if output.startswith("Error: Exa API key not configured"):
            return ToolAttemptResult(
                "FAILED", output, "missing_configuration", output
            )
        if output.startswith("Error: Exa API key is invalid"):
            return ToolAttemptResult(
                "FAILED", output, "authentication_failed", output
            )
        if output.startswith("Error: Exa API quota exceeded"):
            return ToolAttemptResult(
                "FAILED", output, "quota_exceeded", output
            )
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
        if output.startswith("Error: request cancelled"):
            return ToolAttemptResult(
                "CANCELLED", output, "cancelled", output
            )
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
            error_code = {
                401: "authentication_failed",
                403: "permission_denied",
            }.get(status_code, "permanent_failure")
            return ToolAttemptResult("FAILED", output, error_code, output)
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
        if isinstance(value, TypedToolOutput):
            output = truncate_output(str(value))
            return ToolAttemptResult(
                value.status,
                output,
                value.error_code,
                value.error_message,
                value.retryable,
                value.retry_after_seconds,
            )
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


def _retry_delay(retry_number: int, retry_after_seconds: float | None) -> float:
    if retry_after_seconds is not None:
        return max(0.0, min(retry_after_seconds, RETRY_MAX_DELAY_SECONDS))
    base = min(
        RETRY_DELAY_SECONDS * (2 ** max(0, retry_number - 1)),
        RETRY_MAX_DELAY_SECONDS,
    )
    if base <= 0:
        return 0.0
    return min(
        RETRY_MAX_DELAY_SECONDS,
        base + random.uniform(0.0, min(1.0, base * 0.25)),
    )


def _guard_outcome(
    error_code: str,
    attempts: int,
    started: float,
) -> RetryOutcome:
    control_codes = {
        "cancelled", "user_interrupt", "parent_cancelled",
        "deadline_exceeded", "timed_out", "runtime_shutdown",
    }
    if error_code in control_codes:
        status = "CANCELLED"
        output = "CANCELLED: Agent run stopped before the retry started."
    elif error_code in {"tool_not_allowed", "policy_denied"}:
        status = "DENIED"
        output = "DENIED: tool permission no longer allows this retry."
    else:
        status = "FAILED"
        error_code = "internal_error"
        output = "Error: retry eligibility recheck failed."
    return RetryOutcome(
        status,
        output,
        output,
        error_code,
        output,
        False,
        attempts,
        time.monotonic() - started,
        False,
    )


def execute_with_retry_detailed(
    fn,
    args: dict,
    tool_name: str,
    *,
    retry_policy: str = "never",
    side_effect: str = "unknown",
    idempotency: str = "unknown",
    cancel_check: Optional[Callable[[], bool]] = None,
    before_attempt: Optional[Callable[[int], None]] = None,
    on_attempt_started: Optional[Callable[[int], None]] = None,
    on_attempt_finished: Optional[
        Callable[[int, ToolAttemptResult, float], None]
    ] = None,
    on_retry: Optional[Callable[[int, str, float], None]] = None,
    on_retry_wait_finished: Optional[
        Callable[[int, str, float], None]
    ] = None,
    retry_guard: Optional[Callable[[int], str | None]] = None,
    max_retries: int = RETRY_MAX_RETRIES,
    retry_time_budget_seconds: float = RETRY_TOTAL_BUDGET_SECONDS,
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

        if attempt > 1 and retry_guard:
            try:
                guard_error = retry_guard(attempt)
            except Exception:
                guard_error = "internal_error"
            if guard_error:
                return _guard_outcome(guard_error, attempts, started)

        attempts = attempt
        if before_attempt:
            before_attempt(attempt)
        if on_attempt_started:
            on_attempt_started(attempt)
        attempt_started = time.monotonic()
        try:
            last = _execute_once(fn, args, tool_name)
        except Exception as exc:
            if on_attempt_finished:
                reason = getattr(exc, "completion_reason", "cancelled")
                on_attempt_finished(
                    attempt,
                    ToolAttemptResult(
                        "CANCELLED",
                        "CANCELLED: tool execution was interrupted.",
                        reason,
                        "tool execution was interrupted",
                    ),
                    time.monotonic() - attempt_started,
                )
            raise
        attempt_duration = time.monotonic() - attempt_started
        if on_attempt_finished:
            on_attempt_finished(attempt, last, attempt_duration)
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
            and last.error_code in _RETRYABLE_ERROR_CODES
            and retry_policy in ("transient", "idempotent")
            and side_effect == "none"
            and idempotency == "idempotent"
            and attempt < max_attempts
        )
        if not can_retry:
            break

        if retry_guard:
            try:
                guard_error = retry_guard(attempt + 1)
            except Exception:
                guard_error = "internal_error"
            if guard_error:
                return _guard_outcome(guard_error, attempts, started)

        delay = _retry_delay(attempt, last.retry_after_seconds)
        if time.monotonic() - started + delay > max(
            0.0, retry_time_budget_seconds
        ):
            break
        if on_retry:
            on_retry(attempt + 1, last.error_code or "unknown", delay)
        wait_started = time.monotonic()
        if _interruptible_sleep(delay, cancel_check):
            if on_retry_wait_finished:
                on_retry_wait_finished(
                    attempt + 1,
                    "CANCELLED",
                    time.monotonic() - wait_started,
                )
            output = "CANCELLED: Agent run was interrupted during tool retry wait."
            return RetryOutcome(
                "CANCELLED", output, output, "cancelled", output, False,
                attempts, time.monotonic() - started, False,
            )
        if on_retry_wait_finished:
            on_retry_wait_finished(
                attempt + 1,
                "COMPLETED",
                time.monotonic() - wait_started,
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
    idempotency = (
        "idempotent" if tool_name in {"web_search", "web_extract"} else "unknown"
    )
    return execute_with_retry_detailed(
        fn,
        args,
        tool_name,
        retry_policy=retry_policy,
        side_effect=side_effect,
        idempotency=idempotency,
    ).model_output


def register_retry_modifier(tool_name: str, modifier: callable):
    """旧扩展点已停用；保留函数避免第三方导入失败。"""
    return None

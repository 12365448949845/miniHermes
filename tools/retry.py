"""
工具执行重试机制。
对超时、瞬时网络错误等可恢复故障进行自动重试，对 agent loop 完全透明。
"""

import re
import time

from renderer.renderer import _cprint, _DIM, _RST, _AMBER

_INDENT = "    "

RETRY_MAX_RETRIES = 2
RETRY_TOOLS = ["bash", "web_extract", "web_search"]


class ErrorClass:
    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


_TIMEOUT_PATTERN = re.compile(r"Error: command timed out after (\d+)s")
_TRANSIENT_PATTERNS = [
    re.compile(r"request timed out", re.IGNORECASE),
    re.compile(r"HTTP [5]\d{2}"),
    re.compile(r"429"),
    re.compile(r"rate limit", re.IGNORECASE),
    re.compile(r"connection (refused|reset|timed out)", re.IGNORECASE),
]
_PERMANENT_PATTERNS = [
    re.compile(r"Permission denied", re.IGNORECASE),
    re.compile(r"No such file", re.IGNORECASE),
    re.compile(r"not found", re.IGNORECASE),
    re.compile(r"^BLOCKED:", re.MULTILINE),
    re.compile(r"^DENIED", re.MULTILINE),
]


def _looks_like_error(result: str) -> bool:
    """判断工具结果是否为错误（以 Error: 开头或匹配已知错误模式）。"""
    if result.startswith("Error:") or result.startswith("Error executing tool"):
        return True
    if result.startswith("BLOCKED:") or result.startswith("DENIED"):
        return True
    return False


def classify_error(tool_name: str, result: str, args: dict) -> str:
    """基于工具结果字符串分类错误类型。"""
    for pat in _PERMANENT_PATTERNS:
        if pat.search(result):
            return ErrorClass.PERMANENT

    if _TIMEOUT_PATTERN.search(result):
        return ErrorClass.TIMEOUT

    for pat in _TRANSIENT_PATTERNS:
        if pat.search(result):
            return ErrorClass.TRANSIENT

    return ErrorClass.PERMANENT


# ── 工具特定重试参数修改器 ────────────────────────────────────────────
_RETRY_MODIFIERS: dict[str, callable] = {}


def register_retry_modifier(tool_name: str, modifier: callable):
    """注册工具特定的重试参数修改器。

    modifier(args, attempt, error_class) -> dict
    """
    _RETRY_MODIFIERS[tool_name] = modifier


def _modify_args_for_retry(
    tool_name: str, args: dict, attempt: int, error_class: str
) -> dict:
    """根据注册的修改器调整重试参数。"""
    modifier = _RETRY_MODIFIERS.get(tool_name)
    if modifier:
        return modifier(dict(args), attempt, error_class)
    return dict(args)


def _format_retry_result(
    result: str, attempts: int, success: bool, first_timeout: int | None = None, final_timeout: int | None = None
) -> str:
    """组装带重试信息的最终返回字符串。"""
    if success:
        if first_timeout is not None and final_timeout is not None:
            prefix = f"[Retried: previous attempt timed out after {first_timeout}s, succeeded with {final_timeout}s timeout]\n\n"
        else:
            prefix = f"[Retried: succeeded on attempt {attempts}]\n\n"
        return prefix + result
    else:
        return f"[Retried {attempts - 1} times, all failed]\n{result}"


def execute_with_retry(fn, args: dict, tool_name: str) -> str:
    """执行工具函数，对可恢复错误自动重试。"""
    from tools import truncate_output

    if tool_name not in RETRY_TOOLS:
        return _execute_once(fn, args)

    max_attempts = RETRY_MAX_RETRIES + 1

    current_args = dict(args)
    first_timeout = current_args.get("timeout", 30) if tool_name == "bash" else None

    for attempt in range(1, max_attempts + 1):
        result = _execute_once(fn, current_args)

        if not _looks_like_error(result):
            if attempt > 1:
                final_timeout = current_args.get("timeout") if tool_name == "bash" else None
                return _format_retry_result(result, attempt, success=True, first_timeout=first_timeout, final_timeout=final_timeout)
            return result

        error_class = classify_error(tool_name, result, current_args)

        if error_class == ErrorClass.PERMANENT:
            return result

        if attempt >= max_attempts:
            return _format_retry_result(result, attempt, success=False)

        next_args = _modify_args_for_retry(tool_name, current_args, attempt, error_class)
        next_timeout = next_args.get("timeout", "") if tool_name == "bash" else ""
        timeout_hint = f", timeout {next_timeout}s" if next_timeout else ""

        try:
            _cprint(f"{_INDENT}{_AMBER}┊ ⟳ retrying {tool_name} (attempt {attempt + 1}/{max_attempts}{timeout_hint})...{_RST}")
        except Exception:
            pass

        if error_class == ErrorClass.TRANSIENT:
            time.sleep(2.0)  # 瞬时错误标准重试间隔（秒）

        current_args = next_args

    return result


def _execute_once(fn, args: dict) -> str:
    """单次执行工具函数，捕获所有异常转为字符串。"""
    from tools import truncate_output

    try:
        result = fn(**args)
        output = str(result) if result is not None else "(no output)"
        return truncate_output(output)
    except TypeError as e:
        return f"Error: invalid arguments: {e}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"

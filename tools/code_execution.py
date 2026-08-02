"""
云沙箱代码执行工具：通过七牛云 E2B 兼容沙箱执行代码。
"""

import json
import os
import time
from contextlib import contextmanager
from typing import Any

import config as cfg
from tools import register

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_SUPPORTED_LANGUAGES = {"python", "javascript", "typescript", "java", "r", "bash"}

CDE_ENABLED = True
CDE_BACKEND = "qiniu_e2b_sandbox"
CDE_API_URL = "https://cn-yangzhou-1-sandbox.qiniuapi.com"
CDE_SANDBOX_TIMEOUT = 300
CDE_CODE_TIMEOUT = 120
CDE_MAX_OUTPUT_CHARS = 50_000


@register({
    "type": "function",
    "function": {
        "name": "execute_code",
        "description": (
            "Execute code inside Qiniu Cloud's E2B-compatible sandbox. "
            "Use for small code snippets, data processing, algorithm checks, "
            "or generating intermediate results. The code runs in a cloud sandbox, "
            "not in the local MiniHermes process. Do not use for interactive scripts "
            "or tasks that need local files unless you explicitly create/provide them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Code to execute in the cloud sandbox.",
                },
                "language": {
                    "type": "string",
                    "enum": sorted(_SUPPORTED_LANGUAGES),
                    "description": "Programming language for the code. Defaults to python.",
                    "default": "python",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Code execution timeout in seconds. Defaults to config.",
                },
            },
            "required": ["code"],
        },
    },
})
def execute_code(code: str, language: str = "python", timeout: int = None) -> str:
    """
    在七牛云 E2B 兼容沙箱中执行代码。

    Args:
        code: 需要执行的代码，例如 "print('hello')"。
        language: 代码语言，例如 "python" 或 "bash"。
        timeout: 单次代码执行超时秒数，例如 60。

    Returns:
        JSON 字符串，包含 status、output、error、language 和 duration_seconds。
    """
    code_config = _load_code_config()
    if not code_config["enabled"]:
        return _json_error("execute_code is disabled in config.", language=language)

    if code_config["backend"] != CDE_BACKEND:
        return _json_error(
            f"Unsupported code execution backend: {code_config['backend']}",
            language=language,
        )

    if not code or not str(code).strip():
        return _json_error("No code provided.", language=language)

    language = _normalize_language(language)
    if language not in _SUPPORTED_LANGUAGES:
        return _json_error(
            f"Unsupported language '{language}'. Supported: {', '.join(sorted(_SUPPORTED_LANGUAGES))}",
            language=language,
        )

    missing_env = _missing_qiniu_env(code_config)
    if missing_env:
        return _json_error(
            "Missing Qiniu sandbox credentials: " + ", ".join(missing_env),
            language=language,
        )

    try:
        from e2b_code_interpreter import Sandbox
    except ImportError:
        return _json_error(
            "Missing dependency: install e2b-code-interpreter.",
            language=language,
        )

    effective_timeout = _coerce_positive_int(timeout, code_config["code_timeout"])
    sandbox = None
    started_at = time.monotonic()

    with _temporary_env("E2B_API_URL", code_config["api_url"]):
        with _temporary_env("E2B_API_KEY", code_config["api_key"]):
            try:
                sandbox = _create_sandbox(
                    Sandbox=Sandbox,
                    timeout=code_config["sandbox_timeout"],
                )
                output, error = _run_code_in_sandbox(
                    sandbox=sandbox,
                    code=code,
                    language=language,
                    timeout=effective_timeout,
                )
                duration = round(time.monotonic() - started_at, 2)
                output = _redact_sensitive_text(output)
                error = _redact_sensitive_text(error)
                combined_output = _truncate_output(
                    _join_output(output, error),
                    code_config["max_output_chars"],
                )

                result = {
                    "status": "success" if not error else "error",
                    "output": combined_output or "(no output)",
                    "language": language,
                    "duration_seconds": duration,
                    "sandbox_id": getattr(sandbox, "sandbox_id", None),
                }
                if error:
                    result["error"] = error
                return json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                duration = round(time.monotonic() - started_at, 2)
                return json.dumps({
                    "status": "error",
                    "error": _redact_sensitive_text(str(exc)),
                    "output": "",
                    "language": language,
                    "duration_seconds": duration,
                }, ensure_ascii=False)
            finally:
                _kill_sandbox(sandbox)


def _load_code_config() -> dict:
    """
    读取并规整 code_execution 配置（仅 api_key 来自用户配置，其余硬编码）。

    Args:
        无。

    Returns:
        包含 enabled、backend、api_url、timeout 等字段的配置字典。
    """
    raw_config = cfg.get_code_execution_config()
    return {
        "enabled": CDE_ENABLED,
        "backend": CDE_BACKEND,
        "api_key": str(raw_config.get("api_key", "")),
        "api_url": CDE_API_URL,
        "sandbox_timeout": CDE_SANDBOX_TIMEOUT,
        "code_timeout": CDE_CODE_TIMEOUT,
        "max_output_chars": CDE_MAX_OUTPUT_CHARS,
    }


def _missing_qiniu_env(code_config: dict) -> list[str]:
    """
    检查七牛云沙箱需要的环境变量。

    Args:
        code_config: code_execution 配置，例如包含 api_url。

    Returns:
        缺失项列表，例如 ["E2B_API_KEY"]。
    """
    missing = []
    if not code_config.get("api_key") and not os.getenv("E2B_API_KEY"):
        missing.append("E2B_API_KEY")

    if not code_config.get("api_url") and not os.getenv("E2B_API_URL"):
        missing.append("E2B_API_URL")

    return missing


def _create_sandbox(Sandbox, timeout: int):
    """
    创建 E2B 兼容沙箱实例。

    Args:
        Sandbox: e2b_code_interpreter.Sandbox 类。
        timeout: 沙箱生命周期超时秒数，例如 300。

    Returns:
        Sandbox 实例。
    """
    try:
        return Sandbox.create(timeout=timeout)
    except AttributeError:
        return Sandbox(timeout=timeout)


def _run_code_in_sandbox(sandbox, code: str, language: str, timeout: int) -> tuple[str, str]:
    """
    执行代码并收集 SDK 输出。

    Args:
        sandbox: e2b_code_interpreter.Sandbox 实例。
        code: 需要执行的代码，例如 "print('hello')"。
        language: 代码语言，例如 "python"。
        timeout: 单次执行超时秒数。

    Returns:
        (output, error) 元组。
    """
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    result_chunks: list[str] = []
    error_chunks: list[str] = []

    response = sandbox.run_code(
        code,
        language=language,
        timeout=timeout,
        on_stdout=lambda data: stdout_chunks.append(str(data)),
        on_stderr=lambda data: stderr_chunks.append(str(data)),
        on_result=lambda data: result_chunks.append(_stringify_sdk_value(data)),
        on_error=lambda data: error_chunks.append(_stringify_sdk_value(data)),
    )

    output = "".join(stdout_chunks)
    if result_chunks:
        output = _join_output(output, "\n".join(chunk for chunk in result_chunks if chunk))

    response_output, response_error = _extract_response_text(response)
    output = _join_output(output, response_output)
    stderr_text = "".join(stderr_chunks)
    if stderr_text:
        output = _join_output(output, "--- stderr ---\n" + stderr_text)

    error = _join_output("\n".join(error_chunks), response_error)
    return output, error


def _extract_response_text(response: Any) -> tuple[str, str]:
    """
    从 E2B SDK 返回对象中提取补充输出。

    Args:
        response: sandbox.run_code 返回值，例如 Execution 对象。

    Returns:
        (output, error) 元组。
    """
    if response is None:
        return "", ""

    output_parts: list[str] = []
    error_parts: list[str] = []
    logs = getattr(response, "logs", None)
    if logs is not None:
        output_parts.extend(_iter_text_values(getattr(logs, "stdout", None)))
        error_parts.extend(_iter_text_values(getattr(logs, "stderr", None)))

    output_parts.extend(_iter_text_values(getattr(response, "results", None)))
    error_value = getattr(response, "error", None)
    if error_value:
        error_parts.append(_stringify_sdk_value(error_value))

    return "\n".join(output_parts), "\n".join(error_parts)


def _iter_text_values(value: Any) -> list[str]:
    """
    将 SDK 字段规整为字符串列表。

    Args:
        value: 可能是字符串、列表、对象或 None。

    Returns:
        非空字符串列表。
    """
    if value is None:
        return []

    if isinstance(value, (list, tuple)):
        return [text for item in value if (text := _stringify_sdk_value(item))]

    text = _stringify_sdk_value(value)
    return [text] if text else []


def _stringify_sdk_value(value: Any) -> str:
    """
    将 SDK 输出对象转成文本。

    Args:
        value: SDK 返回的输出对象，例如 result/error/log。

    Returns:
        可展示文本。
    """
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    for attr in ("text", "value", "traceback", "message"):
        attr_value = getattr(value, attr, None)
        if attr_value:
            return str(attr_value)

    return str(value)


def _normalize_language(language: str) -> str:
    """
    规整语言名。

    Args:
        language: 用户或模型传入的语言名，例如 "Python" 或 "sh"。

    Returns:
        小写后的语言名，sh 会归一为 bash。
    """
    normalized = str(language or "python").strip().lower()
    if normalized == "sh":
        return "bash"

    return normalized


def _coerce_positive_int(value, default: int) -> int:
    """
    将配置值转成正整数。

    Args:
        value: 原始配置值，例如 "120"。
        default: 解析失败时使用的默认值。

    Returns:
        正整数。
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default

    return parsed if parsed > 0 else default


def _truncate_output(text: str, max_chars: int) -> str:
    """
    截断长输出，保留头尾。

    Args:
        text: 原始输出文本。
        max_chars: 最大字符数，例如 50000。

    Returns:
        截断后的输出文本。
    """
    if len(text) <= max_chars:
        return text

    head_chars = int(max_chars * 0.4)
    tail_chars = max_chars - head_chars
    omitted = len(text) - head_chars - tail_chars
    return (
        text[:head_chars]
        + f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted out of {len(text)} total] ...\n\n"
        + text[-tail_chars:]
    )


def _join_output(*parts: str) -> str:
    """
    合并多个输出片段。

    Args:
        parts: 输出片段，例如 stdout 和 stderr。

    Returns:
        去除空片段后的文本。
    """
    return "\n".join(str(part).strip() for part in parts if str(part).strip())


def _redact_sensitive_text(text: str) -> str:
    """
    从输出中移除已知敏感值。

    Args:
        text: 需要处理的输出文本。

    Returns:
        脱敏后的文本。
    """
    redacted = text or ""
    sensitive_values = [
        os.getenv("E2B_API_KEY", ""),
        os.getenv("E2B_API_URL", ""),
        cfg.get_model_config().get("api_key", ""),
        cfg.get_search_config().get("api_key", ""),
    ]
    for value in sensitive_values:
        if value and len(str(value)) >= 6:
            redacted = redacted.replace(str(value), "[REDACTED]")

    return redacted


def _kill_sandbox(sandbox) -> None:
    """
    清理沙箱实例。

    Args:
        sandbox: E2B Sandbox 实例或 None。

    Returns:
        None。
    """
    if sandbox is None:
        return

    try:
        sandbox.kill()
    except Exception:
        pass


def _json_error(message: str, language: str = "python") -> str:
    """
    构造统一错误 JSON。

    Args:
        message: 错误说明，例如 "No code provided."。
        language: 代码语言，例如 "python"。

    Returns:
        JSON 字符串。
    """
    return json.dumps({
        "status": "error",
        "error": message,
        "output": "",
        "language": _normalize_language(language),
        "duration_seconds": 0,
    }, ensure_ascii=False)


@contextmanager
def _temporary_env(key: str, value: str):
    """
    临时设置环境变量并在退出时恢复。

    Args:
        key: 环境变量名，例如 E2B_API_URL。
        value: 临时环境变量值，例如 https://cn-yangzhou-1-sandbox.qiniuapi.com。

    Yields:
        None。
    """
    old_value = os.environ.get(key)
    if value:
        os.environ[key] = value

    try:
        yield
    finally:
        if old_value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old_value

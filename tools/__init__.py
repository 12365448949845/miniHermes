"""
工具注册表。
设计参考 hermes 的工具注册机制，使用装饰器模式：
  @register(schema) 将函数注册为 LLM 可调用的工具。

工具 schema 采用 OpenAI function calling 格式：
  {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
"""

from typing import Any

from tools.registry import (
    ToolExecutionContext,
    ToolExecutionResult,
    ToolMetadata,
    ToolRegistry,
)

# 默认工具注册表实例（单例）
_default_registry = ToolRegistry()

# 安全网阈值：各工具内部已自行截断，这里兜底防止意外超长
MAX_OUTPUT_CHARS = 50_000


def get_tool_manager() -> ToolRegistry:
    """返回默认的 ToolRegistry 实例。"""
    return _default_registry


def truncate_output(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """超过阈值时保留 head 40% + tail 60%，中间丢弃。"""
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


def register(schema: dict, metadata: ToolMetadata | None = None):
    """装饰器：将函数注册为工具。委托给默认 ToolRegistry。"""
    return _default_registry.register(schema, metadata=metadata)


def get_schemas(
    include: set[str] | None = None,
    exclude: set[str] | None = None,
    policy=None,
) -> list[dict]:
    """返回已注册工具的 schema 列表，支持白名单/黑名单过滤。

    Args:
        include: 若提供，仅返回这些工具（白名单模式）
        exclude: 若提供，排除这些工具（黑名单模式）
    """
    return _default_registry.get_schemas(
        include=include,
        exclude=exclude,
        policy=policy,
    )


def execute(tool_call: dict) -> str:
    """执行一次工具调用。委托给默认 ToolRegistry。

    tool_call 格式（OpenAI）：
      {"id": "...", "type": "function", "function": {"name": "...", "arguments": "{...}"}}
    """
    return _default_registry.execute(tool_call)


def execute_detailed(
    tool_call: dict,
    execution_context: ToolExecutionContext | None = None,
) -> ToolExecutionResult:
    """执行工具并返回结构化结果。"""
    return _default_registry.execute_detailed(tool_call, execution_context)


# 导入所有工具模块，触发 @register 装饰器执行
# （装饰器通过 _default_registry.register() 将工具写入注册表）
from tools import bash, files, search, memory  # noqa: E402, F401
from tools import process_tool, web_extract, session_search, todo, clarify, code_execution  # noqa: E402, F401
from tools import skills_tool, delegate, skill_manage  # noqa: E402, F401
from tools import image_gen, browser  # noqa: E402, F401

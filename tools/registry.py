"""工具注册表：注册、schema 查询与执行分发。

替换原先 tools/__init__.py 中的模块级 _REGISTRY dict，
提供面向对象的接口，支持多实例隔离。
"""

import json
from typing import Callable


class ToolRegistry:
    """工具注册、schema 获取与执行。

    每个实例维护独立的注册表，支持多个隔离的工具集
    （如主 Agent 和子 Agent 使用不同的工具过滤）。
    """

    def __init__(self):
        """初始化空的工具注册表。"""
        self._registry: dict[str, dict] = {}
        # name -> {"fn": callable, "schema": dict}

    def register(self, schema: dict):
        """装饰器工厂：注册一个工具函数。

        Args:
            schema: OpenAI function calling schema dict，
                    工具名取自 schema["function"]["name"]。

        Returns:
            装饰器函数，将 decorated function 存入注册表。

        Example:
            @registry.register({
                "type": "function",
                "function": {"name": "bash", "description": "..."}
            })
            def bash(command, timeout=30):
                ...
        """

        def decorator(fn):
            name = schema["function"]["name"]
            self._registry[name] = {"fn": fn, "schema": schema}
            return fn

        return decorator

    def get_schemas(self, include: list[str] | None = None,
                    exclude: list[str] | None = None) -> list[dict]:
        """返回过滤后的工具 schema 列表，用于发送给 LLM。

        Args:
            include: 白名单，只返回列表中的工具。None 表示全部。
            exclude: 黑名单，排除列表中的工具。None 表示不排除。

        Returns:
            OpenAI function calling schema 的列表。
        """
        schemas = []
        for name, entry in self._registry.items():
            if include and name not in include:
                continue
            if exclude and name in exclude:
                continue
            schemas.append(entry["schema"])
        return schemas

    def execute(self, tool_call: dict) -> str:
        """根据 tool_call 查找并执行已注册的工具。

        Args:
            tool_call: OpenAI 格式的 tool_call dict，
                      包含 function.name 和 function.arguments。

        Returns:
            工具执行的字符串结果。若工具未注册或参数解析失败，
            返回错误描述字符串。
        """
        name = tool_call["function"]["name"]
        raw_args = tool_call["function"].get("arguments", "{}")

        if name not in self._registry:
            return f"Error: tool '{name}' is not registered."

        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError as e:
            return f"Error: failed to parse tool arguments: {e}"

        from tools.retry import execute_with_retry

        return execute_with_retry(self._registry[name]["fn"], args, name)

    def get_names(self) -> set[str]:
        """返回所有已注册工具的名称集合。"""
        return set(self._registry.keys())

    def has(self, name: str) -> bool:
        """检查指定名称的工具是否已注册。"""
        return name in self._registry

    def reset(self):
        """清空注册表（仅用于测试）。"""
        self._registry.clear()

"""
delegate_task 工具：将子任务委派给隔离的子 Agent 执行。

仅注册 schema 供 LLM 感知该工具存在，实际执行由 Agent._execute_tool() 拦截
（同 clarify 模式）。子 Agent 无法看到父对话历史，完成后结果作为 tool_result 返回。
"""

from tools import register

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_task",
        "description": (
            "Delegate a focused subtask to an independent subagent. "
            "The subagent runs in isolation with its own context and a subset of tools. "
            "Use when: a task is self-contained and can be solved without user interaction, "
            "such as research, code analysis, file operations, or multi-step tool chains. "
            "The subagent cannot ask the user questions or delegate further. "
            "Returns the subagent's final response text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": (
                        "Clear, actionable description of what the subagent should accomplish. "
                        "Be specific — the subagent starts with zero context."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Relevant background the subagent needs: file contents, requirements, "
                        "constraints, or any information not derivable from tools alone. "
                        "Optional but strongly recommended for non-trivial tasks."
                    ),
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                    "description": (
                        "Optional tool allowlist for this subagent. Omit to use the "
                        "available non-writing delegate tool set; pass an empty list for no tools. "
                        "Requesting write_file or bash requires execution_mode=worktree_write "
                        "and a non-empty write_scope. The runtime always intersects this list "
                        "with the parent policy."
                    ),
                },
                "execution_mode": {
                    "type": "string",
                    "enum": ["read_only", "worktree_write"],
                    "description": (
                        "Optional execution contract. Use read_only for side-effect-free analysis. "
                        "Use worktree_write for every subagent that may modify code or run shell "
                        "commands, even when changing one file. Omitted mode cannot receive "
                        "write_file or bash and never writes the main workspace."
                    ),
                },
                "write_scope": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "uniqueItems": True,
                    "description": (
                        "Required for worktree_write: relative files or directory prefixes "
                        "ending in '/'. Absolute paths, wildcards, '..', .git, and .minihermes "
                        "are forbidden."
                    ),
                },
                "verification_hint": {
                    "type": "string",
                    "description": (
                        "Optional test command suggestion for the child. It is not executed "
                        "automatically and cannot bypass normal tool approval."
                    ),
                },
            },
            "required": ["task"],
        },
    },
}


@register(_SCHEMA)
def delegate_task(
    task: str,
    context: str = "",
    tools: list[str] | None = None,
    execution_mode: str | None = None,
    write_scope: list[str] | None = None,
    verification_hint: str = "",
) -> str:
    """Placeholder — execution intercepted by Agent._execute_tool()."""
    return "Error: delegate_task must be executed within an Agent context."

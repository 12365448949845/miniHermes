"""
bash 工具：在本地 shell 中执行命令，返回 stdout + stderr。
超时时间默认 30 秒，防止命令挂起。
"""

import subprocess
from tools import register

_MAX_OUTPUT_CHARS = 50_000

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": (
            "Execute a shell command in the local environment. "
            "Returns stdout and stderr combined. "
            "Use for file operations, running scripts, checking system info, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30).",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
}


@register(_SCHEMA)
def bash(command: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        output = output.strip() or "(no output)"

        if len(output) > _MAX_OUTPUT_CHARS:
            head_chars = int(_MAX_OUTPUT_CHARS * 0.4)
            tail_chars = _MAX_OUTPUT_CHARS - head_chars
            omitted = len(output) - head_chars - tail_chars
            output = (
                output[:head_chars]
                + f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted out of {len(output)} total] ...\n\n"
                + output[-tail_chars:]
            )

        return output
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"

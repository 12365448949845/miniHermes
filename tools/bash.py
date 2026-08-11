"""
bash 工具：在本地 shell 中执行命令，返回 stdout + stderr。
超时时间默认 30 秒，防止命令挂起。
"""

import subprocess
import os
import signal
import tempfile
import time
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


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """终止 shell 及其子进程，避免超时后留下后台任务。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _collect_terminated_output(process: subprocess.Popen) -> None:
    """清理后只做有界回收，避免停止路径被异常进程拖住。"""
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


@register(_SCHEMA)
def bash(command: str, timeout: float = 30, _cancel_check=None) -> str:
    process = None
    stdout_file = None
    stderr_file = None
    try:
        timeout = max(0.01, float(timeout))
        # 不用 PIPE：Windows 下孙进程会继承管道句柄，communicate() 即使
        # shell 已退出也可能等待到孙进程自然结束。
        stdout_file = tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace"
        )
        stderr_file = tempfile.TemporaryFile(
            mode="w+t", encoding="utf-8", errors="replace"
        )
        process = subprocess.Popen(
            command,
            shell=True,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=(os.name != "nt"),
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
        )
        started = time.monotonic()
        while True:
            if _cancel_check and _cancel_check():
                _terminate_process_tree(process)
                _collect_terminated_output(process)
                return "Error: command cancelled before completion"
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                _terminate_process_tree(process)
                _collect_terminated_output(process)
                return f"Error: command timed out after {timeout:g}s"
            try:
                process.wait(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += stderr
        if process.returncode != 0:
            output += f"\n[exit code: {process.returncode}]"
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
    except Exception as e:
        if process is not None:
            _terminate_process_tree(process)
        return f"Error: {e}"
    finally:
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()

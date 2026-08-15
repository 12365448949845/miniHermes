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
def bash(
    command: str,
    timeout: float = 30,
    _cancel_check=None,
    _evidence_capture=None,
    _working_directory: str | None = None,
    _workspace_context=None,
) -> str:
    process = None
    stdout_file = None
    stderr_file = None
    termination_reason = "spawn_error"
    exit_code = None
    stdout = ""
    stderr = ""
    try:
        timeout = max(0.01, float(timeout))
        if _workspace_context is not None:
            result = _workspace_context.execute_command(
                command, timeout=timeout, cancel_check=_cancel_check
            )
            stdout = result.stdout
            stderr = result.stderr
            exit_code = result.exit_code
            termination_reason = result.termination_reason
            if result.error_code == "timeout":
                return f"Error: command timed out after {timeout:g}s"
            if result.error_code == "cancelled":
                return "Error: command cancelled before completion"
            if result.error_code:
                detail = stderr.strip() or result.error_code
                return f"Error: {result.error_code}: {detail}"
            output = (stdout or "") + (stderr or "")
            if exit_code not in (None, 0):
                output += f"\n[exit code: {exit_code}]"
            return _truncate_output(output.strip() or "(no output)")
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
            cwd=_working_directory or None,
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
                termination_reason = "cancelled"
                return "Error: command cancelled before completion"
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                _terminate_process_tree(process)
                _collect_terminated_output(process)
                termination_reason = "timed_out"
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
        exit_code = process.returncode
        termination_reason = "exited"
        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += stderr
        if process.returncode != 0:
            output += f"\n[exit code: {process.returncode}]"
        output = output.strip() or "(no output)"

        return _truncate_output(output)
    except Exception as e:
        if process is not None:
            _terminate_process_tree(process)
        stderr = f"{type(e).__name__}: {e}"
        return f"Error: {e}"
    finally:
        if stdout_file is not None:
            stdout_file.seek(0)
            stdout = stdout or stdout_file.read()
        if stderr_file is not None:
            stderr_file.seek(0)
            stderr = stderr or stderr_file.read()
        if _evidence_capture is not None:
            try:
                _evidence_capture.complete(
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=exit_code,
                    termination_reason=termination_reason,
                )
            except Exception as exc:
                # 证据记录是旁路能力，绝不能改变 bash 的原始返回值。
                reporter = getattr(_evidence_capture, "report_failure", None)
                if reporter is not None:
                    reporter("finalize", exc)
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()


def _truncate_output(output: str) -> str:
    if len(output) <= _MAX_OUTPUT_CHARS:
        return output
    head_chars = int(_MAX_OUTPUT_CHARS * 0.4)
    tail_chars = _MAX_OUTPUT_CHARS - head_chars
    omitted = len(output) - head_chars - tail_chars
    return (
        output[:head_chars]
        + f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted out of {len(output)} total] ...\n\n"
        + output[-tail_chars:]
    )

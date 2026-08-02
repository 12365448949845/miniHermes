"""
渲染层：逐行流式输出。

设计：token 累积到缓冲，遇到换行 flush。
  - 普通行：去除 Markdown 标记后直接打印
  - 代码块：缓冲到闭合后纯文本输出
  - Thinking：暗色逐行追加（不覆盖重绘）

所有流式输出通过 _cprint() → print_formatted_text(ANSI(...))，
兼容 prompt_toolkit patch_stdout 环境。
"""

import difflib
import io
import json
import os
import re

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# ── ANSI 颜色常量 ──────────────────────────────────────────────────────────────

_GOLD = "\033[1;38;2;255;215;0m"
_AMBER = "\033[38;2;255;191;0m"
_CREAM = "\033[38;2;255;248;220m"
_BRONZE = "\033[38;2;184;134;11m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_BOLD_RED = "\033[1;31m"
_RST = "\033[0m"

_INDENT = "    "

# ── 工具 Emoji 映射 ─────────────────────────────────────────────────────────

TOOL_EMOJI: dict[str, str] = {
    "bash": "💻", "read_file": "📖", "write_file": "✍️",
    "web_search": "🔍", "todo": "📋", "clarify": "❓",
    "memory": "🧠", "search_files": "🔎", "execute_code": "🐍",
}


# ── _cprint：核心输出函数 ───────────────────────────────────────────────────────

def _cprint(text: str = ""):
    """通过 prompt_toolkit 输出 ANSI 文本，兼容 patch_stdout。"""
    print_formatted_text(ANSI(text))


# ── Rich 适配器（Panel/Todo 等仍用 Rich 渲染，但路由到 _cprint）──────────────

class _RichAdapter:
    """Rich Console 适配器：将 Rich 输出路由到 _cprint()。"""

    def __init__(self):
        self._buf = io.StringIO()
        self._console = Console(file=self._buf, force_terminal=True, width=_term_width())

    def print(self, *args, **kwargs):
        self._buf.seek(0)
        self._buf.truncate()
        self._console.width = _term_width()
        self._console.print(*args, **kwargs)
        output = self._buf.getvalue()
        for line in output.rstrip('\n').split('\n'):
            _cprint(line)


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except (ValueError, OSError):
        return 80


console = _RichAdapter()


# ── 边框辅助 ────────────────────────────────────────────────────────────────────

def _top_border(label: str, char_l: str = "╭", char_r: str = "╮", fill: str = "─") -> str:
    w = _term_width()
    inner = f"{fill} {label} "
    right_fill = w - len(inner) - 2
    if right_fill < 1:
        right_fill = 1
    return f"{char_l}{inner}{fill * right_fill}{char_r}"


def _bottom_border(char_l: str = "╰", char_r: str = "╯", fill: str = "─") -> str:
    w = _term_width()
    return f"{char_l}{fill * (w - 2)}{char_r}"


# ── StreamRenderer ──────────────────────────────────────────────────────────────

class StreamRenderer:
    """管理一次 LLM 响应的流式输出。"""

    def __init__(self):
        self._buf = ""
        self._in_code_block = False
        self._code_lang = ""
        self._code_buf = ""
        self._thinking = False
        self._thinking_started = False
        self._response_started = False

    def reset(self):
        self._buf = ""
        self._in_code_block = False
        self._code_lang = ""
        self._code_buf = ""
        self._thinking = False
        self._thinking_started = False
        self._response_started = False

    # ── 流式阶段回调 ──────────────────────────────────────────────────────

    def on_thinking(self, text: str):
        if not self._thinking_started:
            self._thinking_started = True
            border = _top_border("Reasoning", char_l="┌", char_r="┐")
            _cprint(f"{_BRONZE}{border}{_RST}")
        self._thinking = True
        self._buf += text
        self._flush_lines(thinking=True)

    def _end_thinking(self):
        if self._thinking_started:
            border = _bottom_border(char_l="└", char_r="┘")
            _cprint(f"{_BRONZE}{border}{_RST}")
            self._thinking_started = False

    def on_delta(self, text: str):
        if self._thinking:
            self._thinking = False
            if self._buf:
                self._print_thinking_line(self._buf)
                self._buf = ""
            self._end_thinking()
            _cprint("")

        self._buf += text
        self._flush_lines(thinking=False)

    def _ensure_response_border(self):
        if not self._response_started:
            self._response_started = True
            border = _top_border("⚕ MiniHermes")
            _cprint(f"{_GOLD}{border}{_RST}")

    def on_tool_start(self, tool_name: str):
        self._flush_remaining()
        self._ensure_response_border()
        emoji = TOOL_EMOJI.get(tool_name, "⚡")
        _cprint(f"{_INDENT}{_GOLD}┊ {emoji} {tool_name}{_RST}")

    def on_tool_result(self, tool_name: str, result: str):
        if tool_name == "todo":
            _render_todo(result)
            return
        preview = result[:120].replace("\n", " ")
        if len(result) > 120:
            preview += "..."
        suffix = _detect_failure_suffix(result)
        _cprint(f"{_INDENT}{_AMBER}┊ ✓ {tool_name}:{_RST} {_DIM}{preview}{_RST}{suffix}")

    # ── 流结束 ────────────────────────────────────────────────────────────

    def finalize(self):
        self._end_thinking()
        self._flush_remaining()
        if self._response_started:
            border = _bottom_border()
            _cprint(f"{_GOLD}{border}{_RST}")
            _cprint("")

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _flush_lines(self, thinking: bool):
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if thinking:
                self._print_thinking_line(line)
            else:
                self._process_content_line(line)

    def _flush_remaining(self):
        if self._in_code_block:
            self._render_code_block()
        if self._buf.strip():
            self._process_content_line(self._buf)
        self._buf = ""

    def _print_thinking_line(self, line: str):
        if line.strip():
            _cprint(f"{_INDENT}{_BRONZE}{line}{_RST}")

    def _process_content_line(self, line: str):
        self._ensure_response_border()
        stripped = line.strip()

        if stripped.startswith("```") and not self._in_code_block:
            self._in_code_block = True
            self._code_lang = stripped[3:].strip() or "text"
            self._code_buf = ""
            return

        if stripped == "```" and self._in_code_block:
            self._in_code_block = False
            self._render_code_block()
            return

        if self._in_code_block:
            self._code_buf += line + "\n"
            return

        if not stripped:
            _cprint("")
            return

        formatted = _format_inline(line)
        _cprint(f"{_INDENT}{_CREAM}{formatted}{_RST}")

    def _render_code_block(self):
        code = self._code_buf.rstrip("\n")
        if code:
            _cprint(f"{_INDENT}{_DIM}```{self._code_lang}{_RST}")
            for line in code.split('\n'):
                _cprint(f"{_INDENT}  {_CREAM}{line}{_RST}")
            _cprint(f"{_INDENT}{_DIM}```{_RST}")
        self._code_buf = ""
        self._code_lang = ""


# ── SubagentRenderer ───────────────────────────────────────────────────────────

_SUBAGENT_INDENT = "      "
_CYAN = "\033[36m"


class SubagentRenderer:
    """轻量渲染器：显示子 Agent 的工具调用过程。

    不渲染 thinking 和文本流（子 Agent 的最终回复由父 Agent 处理），
    仅展示工具调用链，让用户知道子 Agent 在做什么。
    """

    def __init__(self, task_preview: str = ""):
        self._task_preview = task_preview[:60]
        self._tool_count = 0
        self._started = False

    def _ensure_header(self):
        if not self._started:
            self._started = True
            label = f"⚙ subagent: {self._task_preview}" if self._task_preview else "⚙ subagent"
            _cprint(f"{_SUBAGENT_INDENT}{_CYAN}┌─ {label}{_RST}")

    def reset(self):
        pass

    def on_thinking(self, text: str):
        self._ensure_header()

    def on_delta(self, text: str):
        pass

    def on_tool_start(self, tool_name: str):
        self._ensure_header()
        self._tool_count += 1
        emoji = TOOL_EMOJI.get(tool_name, "⚡")
        _cprint(f"{_SUBAGENT_INDENT}{_CYAN}│{_RST} {emoji} {tool_name}")

    def on_tool_result(self, tool_name: str, result: str):
        preview = result[:80].replace("\n", " ")
        if len(result) > 80:
            preview += "..."
        _cprint(f"{_SUBAGENT_INDENT}{_CYAN}│{_RST} {_DIM}→ {preview}{_RST}")

    def finalize(self):
        if self._started:
            _cprint(f"{_SUBAGENT_INDENT}{_CYAN}└─ done ({self._tool_count} tool calls){_RST}")


# ── 失败检测 ────────────────────────────────────────────────────────────────────

def _detect_failure_suffix(result: str) -> str:
    if not result:
        return ""
    first_line = result.split('\n', 1)[0]
    exit_match = re.search(r'\[exit (\d+)\]', first_line)
    if exit_match and exit_match.group(1) != "0":
        return f" {_BOLD_RED}[exit {exit_match.group(1)}]{_RST}"
    if result.startswith("Error:") or result.startswith("BLOCKED:") or result.startswith("DENIED"):
        return f" {_BOLD_RED}[error]{_RST}"
    return ""


# ── Diff 渲染 ──────────────────────────────────────────────────────────────────

def render_diff(old_content: str, new_content: str, path: str):
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}")
    lines = list(diff)
    if not lines:
        return

    max_lines = 40
    _cprint(f"{_INDENT}{_DIM}┊ diff:{_RST}")
    for i, line in enumerate(lines[:max_lines]):
        line = line.rstrip('\n')
        if line.startswith('+') and not line.startswith('+++'):
            _cprint(f"{_INDENT}  {_GREEN}{line}{_RST}")
        elif line.startswith('-') and not line.startswith('---'):
            _cprint(f"{_INDENT}  {_RED}{line}{_RST}")
        elif line.startswith('@@'):
            _cprint(f"{_INDENT}  {_DIM}{line}{_RST}")
        elif line.startswith('---') or line.startswith('+++'):
            _cprint(f"{_INDENT}  {_DIM}{line}{_RST}")
        else:
            _cprint(f"{_INDENT}  {_DIM}{line}{_RST}")

    if len(lines) > max_lines:
        omitted = len(lines) - max_lines
        _cprint(f"{_INDENT}  {_DIM}… {omitted} more lines omitted{_RST}")


# ── 内联 Markdown 格式化 ─────────────────────────────────────────────────────

def _format_inline(line: str) -> str:
    """去除 Markdown 语法标记，保留纯文本 + 少量 ANSI 格式。"""
    if line.startswith("### "):
        return f"{_BOLD}{line}{_RST}"
    if line.startswith("## "):
        return f"{_BOLD}{line}{_RST}"
    if line.startswith("# "):
        return f"{_BOLD}{line}{_RST}"
    if line.startswith("> "):
        return f"{_DIM}│ {line[2:]}{_RST}"
    # 去掉 ` 和 ** 标记
    line = re.sub(r'`([^`]+)`', r'\1', line)
    line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)
    line = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'\1', line)
    return line


# ── 独立工具函数 ──────────────────────────────────────────────────────────────

def print_welcome(model_name: str, tools: set[str] = None, cwd: str = None):
    """启动 banner — 在 patch_stdout 之前调用，直接用 Rich。"""
    from rich.console import Console as RichConsole
    _rc = RichConsole()
    tools_str = ", ".join(sorted(tools)) if tools else "none"
    cwd_display = cwd or os.getcwd()
    if len(cwd_display) > 60:
        cwd_display = "..." + cwd_display[-57:]

    info_lines = [
        f"[bold #FFD700]⚕ MiniHermes[/bold #FFD700]  [#B8860B]model: {model_name}[/#B8860B]",
        f"[dim #B8860B]cwd: {cwd_display}[/dim #B8860B]",
        f"[dim #B8860B]tools: {tools_str}[/dim #B8860B]",
        "[dim #B8860B]Ctrl+C to interrupt · Ctrl+D to exit · /help for commands[/dim #B8860B]",
    ]
    _rc.print(Panel("\n".join(info_lines), border_style="#CD7F32", padding=(0, 1)))


def print_error(message: str):
    _cprint(f"{_BOLD_RED}Error:{_RST} {message}")


def print_budget_warning(used: int, max_iter: int):
    _cprint(f"\n{_GOLD}⚠ Iteration budget exhausted ({used}/{max_iter}). Task may be incomplete.{_RST}")


def _format_tool_calls(msg: dict) -> str:
    """从 assistant 消息中提取 tool_calls 摘要。"""
    tool_calls = msg.get("tool_calls")
    if not tool_calls:
        return ""
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except (json.JSONDecodeError, TypeError):
            return ""
    names = [tc.get("function", {}).get("name", "?") for tc in tool_calls
             if isinstance(tc, dict)]
    if not names:
        return ""
    return f" [dim]→ {', '.join(names[:6])}{' …' if len(names) > 6 else ''}[/dim]"


def _format_tool_result(msg: dict) -> str:
    """格式化 tool 消息为一行摘要。"""
    tool_name = msg.get("tool_name", "tool") or "tool"
    content = msg.get("content", "") or ""
    # 截断: 保留第一行或前 120 字符
    first_line = content.split("\n", 1)[0].strip()
    if len(first_line) > 120:
        first_line = first_line[:120] + "..."
    label = {"read_file": "📖", "write_file": "✍️", "bash": "💻",
             "web_search": "🔍", "web_extract": "📄", "execute_code": "⚡"}.get(tool_name, "🔧")
    return f"[dim]{label} {tool_name}: {_escape(first_line)}[/dim]"


def print_resumed_history(messages: list[dict]):
    """展示恢复会话的历史消息摘要（含工具调用）— 通过 _RichAdapter 路由。"""
    if not messages:
        return

    # 展示 user / assistant / tool，保留工具调用上下文
    display_roles = {"user", "assistant", "tool"}

    max_items = 20
    visible_all = [m for m in messages if m.get("role") in display_roles]
    skipped = max(0, len(visible_all) - max_items)
    visible = visible_all[-max_items:] if skipped > 0 else visible_all

    lines = []
    if skipped > 0:
        lines.append(f"[dim]... {skipped} earlier messages ...[/dim]")

    for i, msg in enumerate(visible):
        role = msg["role"]
        content = msg.get("content", "") or ""
        is_last_assistant = (role == "assistant" and i == len(visible) - 1)

        if role == "user":
            truncated = content[:300] + "..." if len(content) > 300 else content
            lines.append(f"[bold #FFD700]● You:[/bold #FFD700] [dim]{_escape(truncated)}[/dim]")
        elif role == "assistant":
            tools_hint = _format_tool_calls(msg)
            if is_last_assistant:
                lines.append(f"[bold #FFBF00]◆ Hermes:[/bold #FFBF00] {_escape(content)}{tools_hint}")
            else:
                truncated = content[:200] + "..." if len(content) > 200 else content
                lines.append(f"[bold #FFBF00]◆ Hermes:[/bold #FFBF00] [dim]{_escape(truncated)}{tools_hint}[/dim]")
        elif role == "tool":
            lines.append(f"  {_format_tool_result(msg)}")

    if not lines:
        return

    panel_content = "\n".join(lines)
    console.print(
        Panel(
            panel_content,
            title="[#B8860B]Previous Conversation[/#B8860B]",
            border_style="#CD7F32 dim",
            padding=(0, 1),
        )
    )


def _render_todo(result: str):
    """将 todo JSON 渲染为带图标的面板 — 通过 _RichAdapter 路由。"""
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        _cprint(f"{_AMBER}✓ todo:{_RST} {_DIM}{result[:120]}{_RST}")
        return

    todos = data.get("todos", [])
    summary = data.get("summary", {})

    if not todos:
        _cprint(f"{_AMBER}✓ todo:{_RST} {_DIM}(empty list){_RST}")
        return

    _STATUS_STYLE = {
        "completed":   ("[green]✔[/green]", "strike dim"),
        "in_progress": ("[#FFD700]▶[/#FFD700]", "bold"),
        "pending":     ("[dim]○[/dim]", ""),
        "cancelled":   ("[red]✘[/red]", "strike dim"),
    }

    lines = []
    for item in todos:
        status = item.get("status", "pending")
        icon, style = _STATUS_STYLE.get(status, ("[dim]?[/dim]", ""))
        content = _escape(item.get("content", ""))
        if style:
            lines.append(f"  {icon} [{style}]{content}[/{style}]")
        else:
            lines.append(f"  {icon} {content}")

    total = summary.get("total", len(todos))
    done = summary.get("completed", 0)
    progress = f"{done}/{total}"

    panel_content = "\n".join(lines)
    console.print(Panel(
        panel_content,
        title=f"[#FFD700]📋 Tasks[/#FFD700] [dim]{progress} done[/dim]",
        border_style="#CD7F32 dim",
        padding=(0, 1),
    ))


def _escape(text: str) -> str:
    """转义 Rich markup 特殊字符。"""
    return text.replace("[", "\\[").replace("]", "\\]")

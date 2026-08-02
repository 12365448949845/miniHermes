"""Approval 交互状态机与 UI 片段（参考 hermes 内嵌 widget 方案）。"""

import shutil
from queue import Empty, Queue

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.filters import Condition

from cli.state import AppState


_APPROVAL_CHOICES = ["Allow once", "Allow for session", "Deny"]
_APPROVAL_KEYS = ["once", "session", "deny"]


def get_approval_fragments(state: AppState):
    """构建 approval 面板的 prompt_toolkit 片段（hermes 风格 box-drawing）。"""
    if not state.approval_state:
        return []

    description = state.approval_state.get("description", "")
    detail = state.approval_state.get("detail", "")
    selected = state.approval_state.get("selected", 0)

    title = "⚠  Dangerous Command"
    cmd_display = detail[:70] + "..." if len(detail) > 70 else detail
    hint_text = "↑↓ move · Enter confirm · Ctrl-C deny"

    choice_lines = [f"❯ {i + 1}. {c}" for i, c in enumerate(_APPROVAL_CHOICES)]
    content_lines = [title, description, cmd_display, hint_text] + choice_lines
    longest = max(len(line) for line in content_lines if line)
    term_cols = shutil.get_terminal_size((80, 24)).columns
    inner_width = min(max(longest + 2, 44), 72, max(30, term_cols - 6))
    box_width = inner_width + 2

    def _pad(text: str) -> str:
        return text.ljust(inner_width)

    lines = []
    lines.append(("class:approval-border", "╭" + "─" * box_width + "╮\n"))

    lines.append(("class:approval-border", "│ "))
    lines.append(("class:approval-title", _pad(title)))
    lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "│" + " " * box_width + "│\n"))

    if description:
        desc_display = description if len(description) <= inner_width else description[:inner_width - 3] + "..."
        lines.append(("class:approval-border", "│ "))
        lines.append(("class:approval-desc", _pad(desc_display)))
        lines.append(("class:approval-border", " │\n"))

    if cmd_display:
        if len(cmd_display) > inner_width:
            cmd_display = cmd_display[:inner_width - 3] + "..."
        lines.append(("class:approval-border", "│ "))
        lines.append(("class:approval-cmd", _pad(cmd_display)))
        lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "│" + " " * box_width + "│\n"))

    for i, choice in enumerate(_APPROVAL_CHOICES):
        prefix = "❯" if i == selected else " "
        label = f"{prefix} {i + 1}. {choice}"
        style = "class:approval-selected" if i == selected else "class:approval-choice"
        lines.append(("class:approval-border", "│ "))
        lines.append((style, _pad(label)))
        lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "│ "))
    lines.append(("class:approval-hint", _pad(hint_text)))
    lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "╰" + "─" * box_width + "╯\n"))
    return lines


def build_approval_widget(state: AppState):
    """构建 approval ConditionalContainer widget。"""
    return ConditionalContainer(
        Window(
            content=FormattedTextControl(lambda: get_approval_fragments(state)),
            wrap_lines=True,
        ),
        filter=Condition(lambda: state.approval_state is not None),
    )


def make_approval_callback(state: AppState):
    """工厂函数：生成传给 Agent 的 approval 回调（替代 tools/approval.py 的 radiolist）。"""

    def _approval_callback(tool_name: str, args: dict, description: str) -> str:
        response_queue: Queue = Queue()

        if tool_name == "bash":
            detail = args.get("command", "")
        else:
            detail = args.get("path", "")

        state.approval_state = {
            "description": description,
            "detail": detail,
            "selected": 0,
            "response_queue": response_queue,
        }
        state.invalidate()

        while not state.should_exit:
            try:
                response = response_queue.get(timeout=1)
                state.clear_approval()
                state.invalidate()
                return response
            except Empty:
                state.invalidate()

        state.clear_approval()
        state.invalidate()
        return "deny"

    return _approval_callback

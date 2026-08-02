"""Application 布局组装。"""

from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, Window, FormattedTextControl, Dimension, Float, FloatContainer
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import TextArea
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import merge_completers
from prompt_toolkit.filters import Condition

from cli.state import AppState
from cli.styles import STYLE
from cli.completers import SlashCommandCompleter, FileRefCompleter
from cli.clarify import build_clarify_widget
from cli.approval import build_approval_widget
from cli.plan import build_plan_approval_widget
from cli.keybindings import build_keybindings


def _build_input_area(state: AppState) -> TextArea:
    """构建底部输入框。"""

    def get_prompt():
        if state.clarify_state:
            return [("class:prompt", "? ")]
        if state.command_running:
            return [("class:prompt", "⚕ ")]
        return [("class:prompt", "❯ ")]

    input_area = TextArea(
        height=Dimension(min=1, max=8, preferred=1),
        prompt=get_prompt,
        style="class:input-area",
        multiline=True,
        wrap_lines=True,
        history=FileHistory(str(Path.home() / ".minihermes_history")),
        completer=merge_completers([SlashCommandCompleter(), FileRefCompleter()]),
        complete_while_typing=True,
        read_only=Condition(lambda: False),
    )

    def _input_height():
        try:
            from prompt_toolkit.application import get_app
            from prompt_toolkit.utils import get_cwidth
            doc = input_area.buffer.document
            prompt_width = 3
            try:
                available_width = get_app().output.get_size().columns - prompt_width
            except Exception:
                available_width = 77
            if available_width < 10:
                available_width = 40
            visual_lines = 0
            for line in doc.lines:
                line_width = get_cwidth(line)
                if line_width <= 0:
                    visual_lines += 1
                else:
                    visual_lines += max(1, -(-line_width // available_width))
            return min(max(visual_lines, 1), 8)
        except Exception:
            return 1

    input_area.window.height = _input_height
    return input_area


def _build_status_bar(state: AppState):
    """构建状态栏。"""
    def _get_status_fragments():
        return [("class:status-bar", state.status_text)]

    return Window(
        content=FormattedTextControl(_get_status_fragments),
        height=1,
    )


def build_app(state: AppState) -> Application:
    """构建完整的 prompt_toolkit Application。"""
    input_area = _build_input_area(state)
    clarify_widget = build_clarify_widget(state)
    approval_widget = build_approval_widget(state)
    plan_approval_widget = build_plan_approval_widget(state)
    status_bar = _build_status_bar(state)

    input_rule_top = Window(char='─', height=1, style='class:input-rule')
    input_rule_bot = Window(char='─', height=1, style='class:input-rule')
    completions_menu = CompletionsMenu(max_height=12, scroll_offset=1)

    layout = Layout(
        FloatContainer(
            content=HSplit([
                Window(height=0),
                plan_approval_widget,
                approval_widget,
                clarify_widget,
                status_bar,
                input_rule_top,
                input_area,
                input_rule_bot,
            ]),
            floats=[Float(xcursor=True, ycursor=True, content=completions_menu)],
        )
    )

    kb = build_keybindings(state, input_area)

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=STYLE,
        full_screen=False,
        mouse_support=False,
    )

    state.app = app
    return app

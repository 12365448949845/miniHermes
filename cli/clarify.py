"""Clarify 交互状态机与 UI 片段。"""

import time
from queue import Empty, Queue

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.filters import Condition

from cli.state import AppState
from renderer.renderer import _cprint, _DIM, _RST


def get_clarify_fragments(state: AppState):
    """构建 clarify 面板的 prompt_toolkit 片段。"""
    if not state.clarify_state:
        return []

    question = state.clarify_state.get("question", "")
    choices = state.clarify_state.get("choices") or []
    selected = state.clarify_state.get("selected", 0)
    remaining = max(0, int(state.clarify_deadline - time.monotonic()))
    fragments = [
        ("class:clarify-border", "╭─ "),
        ("class:clarify-title", "MiniHermes needs your input"),
        ("class:clarify-border", " ─╮\n"),
        ("class:clarify-question", f"  {question}\n"),
    ]

    if choices:
        for i, choice in enumerate(choices):
            prefix = "❯" if i == selected and not state.clarify_freetext else " "
            style = "class:clarify-selected" if i == selected and not state.clarify_freetext else "class:clarify-choice"
            fragments.append((style, f"  {prefix} {i + 1}. {choice}\n"))

        other_idx = len(choices)
        other_prefix = "❯" if other_idx == selected or state.clarify_freetext else " "
        other_style = (
            "class:clarify-selected"
            if other_idx == selected or state.clarify_freetext
            else "class:clarify-choice"
        )
        custom_text = state.clarify_state.get("custom_text", "")
        if state.clarify_other_selected():
            other_label = f"Other: {custom_text}▏" if custom_text else "Other: ▏"
        else:
            other_label = "Other (type your answer)"
        fragments.append((other_style, f"  {other_prefix} {other_idx + 1}. {other_label}\n"))
    else:
        fragments.append(("class:clarify-choice", "  Type your answer below, then press Enter.\n"))

    hint = "↑↓ move · Enter confirm"
    if state.clarify_other_selected():
        hint = "type inline · Backspace edit · Enter submit"
    elif state.clarify_freetext:
        hint = "Enter submit · Ctrl+C cancel"
    fragments.extend([
        ("class:clarify-hint", f"  {hint} · {remaining}s left\n"),
        ("class:clarify-border", "╰──────────────────────────────╯"),
    ])
    return fragments


def get_clarify_height(state: AppState):
    """计算 clarify 面板高度。"""
    if not state.clarify_state:
        return 0
    choices = state.clarify_state.get("choices") or []
    return 6 + (len(choices) + 1 if choices else 1)


def build_clarify_widget(state: AppState):
    """构建 clarify ConditionalContainer widget。"""
    return ConditionalContainer(
        Window(
            content=FormattedTextControl(lambda: get_clarify_fragments(state)),
            height=lambda: get_clarify_height(state),
            wrap_lines=True,
        ),
        filter=Condition(lambda: state.clarify_state is not None),
    )


def make_clarify_callback(state: AppState):
    """工厂函数：生成传给 Agent 的 clarify 回调。"""

    def _clarify_callback(question, choices):
        timeout = 120
        response_queue: Queue = Queue()
        normalized_choices = list(choices or [])

        state.clarify_state = {
            "question": question,
            "choices": normalized_choices,
            "selected": 0,
            "custom_text": "",
            "response_queue": response_queue,
        }
        state.clarify_freetext = not bool(normalized_choices)
        state.clarify_deadline = time.monotonic() + timeout
        state.invalidate()

        while not state.should_exit:
            try:
                response = response_queue.get(timeout=1)
                state.clear_clarify()
                state.invalidate()
                return response
            except Empty:
                if time.monotonic() >= state.clarify_deadline:
                    break
                state.invalidate()

        state.clear_clarify()
        state.invalidate()
        _cprint(f"\n{_DIM}(clarify timed out after {timeout}s — agent will decide){_RST}")
        return (
            "The user did not provide a response within the time limit. "
            "Use your best judgement to make the choice and proceed."
        )

    return _clarify_callback

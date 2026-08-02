"""键绑定注册。"""

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.filters import Condition

from cli.state import AppState


def build_keybindings(state: AppState, input_area) -> KeyBindings:
    """注册所有键绑定并返回 KeyBindings 实例。"""
    kb = KeyBindings()

    def _submit_clarify_text(event):
        """提交 clarify 自由文本答案。"""
        if not state.clarify_state:
            return
        text = event.app.current_buffer.text.strip() or "(empty)"
        state.clarify_state["response_queue"].put(text)
        state.clear_clarify()
        event.app.current_buffer.reset()
        event.app.invalidate()

    def _submit_clarify_selection(event):
        """提交当前 clarify 选项或 Other 文本。"""
        if not state.clarify_state:
            return
        choices = state.clarify_state.get("choices") or []
        selected = state.clarify_state.get("selected", 0)
        if selected < len(choices):
            state.clarify_state["response_queue"].put(choices[selected])
            state.clear_clarify()
            event.app.invalidate()
            return
        text = state.clarify_state.get("custom_text", "").strip() or "(empty)"
        state.clarify_state["response_queue"].put(text)
        state.clear_clarify()
        event.app.invalidate()

    def _cancel_clarify(event):
        """取消 clarify 并释放 agent 线程。"""
        if not state.clarify_state:
            return
        state.clarify_state["response_queue"].put(
            "The user cancelled the clarification prompt. "
            "Use your best judgement and proceed."
        )
        state.clear_clarify()
        event.app.current_buffer.reset()
        event.app.invalidate()

    def _submit_approval_selection(event):
        """提交当前 approval 选项。"""
        if not state.approval_state:
            return
        from cli.approval import _APPROVAL_KEYS
        selected = state.approval_state.get("selected", 0)
        state.approval_state["response_queue"].put(_APPROVAL_KEYS[selected])
        state.clear_approval()
        event.app.invalidate()

    def _submit_plan_approval_selection(event):
        """提交当前 plan 审批选项。"""
        if not state.plan_approval_state:
            return
        from cli.plan import _PLAN_KEYS
        selected = state.plan_approval_state.get("selected", 0)
        state.plan_approval_state["response_queue"].put(_PLAN_KEYS[selected])
        state.clear_plan_approval()
        event.app.invalidate()

    @kb.add('enter')
    def handle_enter(event):
        if state.plan_approval_state:
            _submit_plan_approval_selection(event)
            return
        if state.approval_state:
            _submit_approval_selection(event)
            return
        if state.clarify_state:
            if state.clarify_freetext:
                _submit_clarify_text(event)
            else:
                _submit_clarify_selection(event)
            return
        if state.command_running:
            return
        text = input_area.text.strip()
        if text:
            state.input_queue.put(text)
            input_area.text = ""

    @kb.add('c-j')
    def handle_ctrl_j(event):
        event.current_buffer.insert_text('\n')

    @kb.add('<any>', filter=Condition(state.clarify_other_selected))
    def handle_clarify_other_text(event):
        if not state.clarify_state or not state.clarify_other_selected():
            return
        data = event.data
        if data and data.isprintable():
            state.clarify_state["custom_text"] = state.clarify_state.get("custom_text", "") + data
            event.app.invalidate()

    @kb.add('backspace', filter=Condition(state.clarify_other_selected))
    def handle_clarify_other_backspace(event):
        if not state.clarify_state or not state.clarify_other_selected():
            return
        state.clarify_state["custom_text"] = state.clarify_state.get("custom_text", "")[:-1]
        event.app.invalidate()

    @kb.add('c-h', filter=Condition(state.clarify_other_selected))
    def handle_clarify_other_ctrl_h(event):
        if not state.clarify_state or not state.clarify_other_selected():
            return
        state.clarify_state["custom_text"] = state.clarify_state.get("custom_text", "")[:-1]
        event.app.invalidate()

    @kb.add('up', filter=Condition(lambda: bool(state.plan_approval_state)))
    def handle_plan_approval_up(event):
        if state.plan_approval_state:
            state.plan_approval_state["selected"] = max(0, state.plan_approval_state.get("selected", 0) - 1)
            event.app.invalidate()

    @kb.add('down', filter=Condition(lambda: bool(state.plan_approval_state)))
    def handle_plan_approval_down(event):
        if state.plan_approval_state:
            from cli.plan import _PLAN_CHOICES
            max_idx = len(_PLAN_CHOICES) - 1
            state.plan_approval_state["selected"] = min(max_idx, state.plan_approval_state.get("selected", 0) + 1)
            event.app.invalidate()

    @kb.add('up', filter=Condition(lambda: bool(state.approval_state)))
    def handle_approval_up(event):
        if state.approval_state:
            state.approval_state["selected"] = max(0, state.approval_state.get("selected", 0) - 1)
            event.app.invalidate()

    @kb.add('down', filter=Condition(lambda: bool(state.approval_state)))
    def handle_approval_down(event):
        if state.approval_state:
            from cli.approval import _APPROVAL_CHOICES
            max_idx = len(_APPROVAL_CHOICES) - 1
            state.approval_state["selected"] = min(max_idx, state.approval_state.get("selected", 0) + 1)
            event.app.invalidate()

    @kb.add('up', filter=Condition(lambda: bool(state.clarify_state) and not state.clarify_freetext))
    def handle_clarify_up(event):
        if state.clarify_state:
            state.clarify_state["selected"] = max(0, state.clarify_state.get("selected", 0) - 1)
            event.app.invalidate()

    @kb.add('down', filter=Condition(lambda: bool(state.clarify_state) and not state.clarify_freetext))
    def handle_clarify_down(event):
        if state.clarify_state:
            choices = state.clarify_state.get("choices") or []
            max_idx = len(choices)
            state.clarify_state["selected"] = min(max_idx, state.clarify_state.get("selected", 0) + 1)
            event.app.invalidate()

    @kb.add('c-d')
    def handle_ctrl_d(event):
        if state.clarify_state:
            _cancel_clarify(event)
        state.input_queue.put(None)
        event.app.exit()

    @kb.add('c-c')
    def handle_ctrl_c(event):
        if state.plan_approval_state:
            state.plan_approval_state["response_queue"].put("cancel")
            state.clear_plan_approval()
            event.app.invalidate()
            return
        if state.approval_state:
            state.approval_state["response_queue"].put("deny")
            state.clear_approval()
            event.app.invalidate()
            return
        if state.clarify_state:
            _cancel_clarify(event)
            return
        if state.command_running:
            state.agent.interrupt()
        else:
            if input_area.text:
                input_area.text = ""
            else:
                state.input_queue.put(None)
                event.app.exit()

    return kb

"""共享可变状态容器，替代 main() 内部的 list-hack 闭包变量。"""

from dataclasses import dataclass, field
from queue import Queue
from typing import Optional


@dataclass
class AppState:
    """CLI 和对话线程共享的可变状态。"""

    # clarify 交互
    clarify_state: Optional[dict] = None
    clarify_freetext: bool = False
    clarify_deadline: float = 0.0

    # approval 交互
    approval_state: Optional[dict] = None

    # plan 审批
    plan_approval_state: Optional[dict] = None

    # 控制标志
    should_exit: bool = False
    command_running: bool = False

    # 显示信息
    status_text: str = ""
    model_name: str = ""
    context_window: int = 200000

    # 线程通信
    input_queue: Queue = field(default_factory=Queue)

    # 运行时引用（启动后注入）
    agent: object = None
    app: object = None
    session_id: str = ""
    conversation_history: list = field(default_factory=list)

    # 主事件循环引用（供后台线程通过 run_in_terminal 桥接交互式 I/O）
    _main_loop: object = None

    def invalidate(self):
        """安全地请求 UI 重绘。"""
        try:
            if self.app:
                self.app.invalidate()
        except Exception:
            pass

    def clear_clarify(self):
        """清空 clarify UI 状态。"""
        self.clarify_state = None
        self.clarify_freetext = False
        self.clarify_deadline = 0.0

    def clear_approval(self):
        """清空 approval UI 状态。"""
        self.approval_state = None

    def clear_plan_approval(self):
        """清空 plan 审批 UI 状态。"""
        self.plan_approval_state = None

    def clarify_other_selected(self) -> bool:
        """判断是否选中了多选 clarify 的 Other 行。"""
        if not self.clarify_state:
            return False
        choices = self.clarify_state.get("choices") or []
        return bool(choices) and self.clarify_state.get("selected", 0) == len(choices)

    def clarify_uses_input_area(self) -> bool:
        """判断当前 clarify 是否需要底部输入框（开放式问题）。"""
        if not self.clarify_state:
            return False
        choices = self.clarify_state.get("choices") or []
        return not bool(choices)

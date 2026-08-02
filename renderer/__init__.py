"""终端渲染层：Rich 输出适配。"""
from .renderer import (
    StreamRenderer, SubagentRenderer, print_welcome, print_error,
    print_budget_warning, print_resumed_history, render_diff,
    console, _cprint,
    _GOLD, _AMBER, _CREAM, _BRONZE, _DIM, _BOLD, _GREEN, _RED, _BOLD_RED, _RST,
)

__all__ = [
    "StreamRenderer", "SubagentRenderer", "print_welcome", "print_error",
    "print_budget_warning", "print_resumed_history", "render_diff",
    "console", "_cprint",
    "_GOLD", "_AMBER", "_CREAM", "_BRONZE", "_DIM", "_BOLD", "_GREEN", "_RED", "_BOLD_RED", "_RST",
]

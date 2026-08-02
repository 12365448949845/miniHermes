"""Plan mode: 只读规划 + 审批执行。"""

import re
import shutil
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from prompt_toolkit.layout import Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.filters import Condition

from cli.state import AppState


# ── Plan 模式允许的工具（只读）─────────────────────────────────────────────────

PLAN_ALLOWED_TOOLS = {
    "read_file", "list_dir", "web_search", "web_extract",
    "session_search", "process", "memory", "clarify",
    "todo", "skill_view",
}

# ── Plan 模式系统提示追加 ─────────────────────────────────────────────────────

PLAN_MODE_PROMPT = """

# Plan Mode Active

You are in PLAN MODE. Your job is to analyze the codebase and produce a detailed implementation plan.

RULES:
- You MUST NOT make any changes to files or run any commands that modify state.
- You can ONLY read files, search code, browse the web, and use other read-only tools.
- Analyze the codebase thoroughly before producing the plan.

Your final response MUST be a complete implementation plan in markdown format with:
1. **Goal** — What needs to be accomplished
2. **Analysis** — Key findings from reading the code
3. **Steps** — Ordered implementation steps with specific file paths and changes
4. **Risks** — Potential issues or considerations

Be specific: include file paths, function names, and describe exact changes needed.
"""

# ── Plan 文件路径生成 ──────────────────────────────────────────────────────────

def generate_plan_path(description: str) -> Path:
    """生成 plan 文件路径：.minihermes/plans/<timestamp>-<slug>.md"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    words = re.sub(r'[^a-zA-Z0-9一-鿿\s]', '', description).split()[:5]
    slug = "-".join(w.lower() for w in words) if words else "plan"
    slug = slug[:40]
    filename = f"{timestamp}-{slug}.md"
    plan_dir = Path.cwd() / ".minihermes" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir / filename


# ── Plan 审批 UI ──────────────────────────────────────────────────────────────

_PLAN_CHOICES = ["Execute this plan", "Cancel"]
_PLAN_KEYS = ["execute", "cancel"]


def get_plan_approval_fragments(state: AppState):
    """构建 plan 审批面板的 prompt_toolkit 片段。"""
    if not state.plan_approval_state:
        return []

    plan_path = state.plan_approval_state.get("plan_path", "")
    plan_text = state.plan_approval_state.get("plan_text", "")
    selected = state.plan_approval_state.get("selected", 0)

    title = "Plan Ready"
    path_display = str(plan_path)

    preview_lines = plan_text.strip().split("\n")[:3]
    preview = " | ".join(line.strip() for line in preview_lines if line.strip())
    if len(preview) > 70:
        preview = preview[:67] + "..."

    content_lines = [title, path_display, preview, "↑↓ move · Enter confirm · Ctrl-C cancel"]
    for c in _PLAN_CHOICES:
        content_lines.append(f"❯ {c}")
    longest = max(len(line) for line in content_lines if line)
    term_cols = shutil.get_terminal_size((80, 24)).columns
    inner_width = min(max(longest + 2, 44), 72, max(30, term_cols - 6))
    box_width = inner_width + 2

    def _pad(text: str) -> str:
        if len(text) > inner_width:
            text = text[:inner_width - 3] + "..."
        return text.ljust(inner_width)

    lines = []
    lines.append(("class:approval-border", "╭" + "─" * box_width + "╮\n"))

    lines.append(("class:approval-border", "│ "))
    lines.append(("class:approval-title", _pad(title)))
    lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "│" + " " * box_width + "│\n"))

    lines.append(("class:approval-border", "│ "))
    lines.append(("class:approval-cmd", _pad(path_display)))
    lines.append(("class:approval-border", " │\n"))

    if preview:
        lines.append(("class:approval-border", "│ "))
        lines.append(("class:approval-hint", _pad(preview)))
        lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "│" + " " * box_width + "│\n"))

    for i, choice in enumerate(_PLAN_CHOICES):
        prefix = "❯" if i == selected else " "
        label = f"{prefix} {i + 1}. {choice}"
        style = "class:approval-selected" if i == selected else "class:approval-choice"
        lines.append(("class:approval-border", "│ "))
        lines.append((style, _pad(label)))
        lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "│ "))
    lines.append(("class:approval-hint", _pad("↑↓ move · Enter confirm · Ctrl-C cancel")))
    lines.append(("class:approval-border", " │\n"))

    lines.append(("class:approval-border", "╰" + "─" * box_width + "╯\n"))
    return lines


def build_plan_approval_widget(state: AppState):
    """构建 plan 审批 ConditionalContainer widget。"""
    return ConditionalContainer(
        Window(
            content=FormattedTextControl(lambda: get_plan_approval_fragments(state)),
            wrap_lines=True,
        ),
        filter=Condition(lambda: state.plan_approval_state is not None),
    )


def make_plan_approval_callback(state: AppState):
    """工厂函数：生成 plan 审批回调，阻塞等待用户选择。"""

    def _plan_approval_callback(plan_text: str, plan_path: str) -> str:
        response_queue: Queue = Queue()

        state.plan_approval_state = {
            "plan_text": plan_text,
            "plan_path": plan_path,
            "selected": 0,
            "response_queue": response_queue,
        }
        state.invalidate()

        while not state.should_exit:
            try:
                response = response_queue.get(timeout=1)
                state.clear_plan_approval()
                state.invalidate()
                return response
            except Empty:
                state.invalidate()

        state.clear_plan_approval()
        state.invalidate()
        return "cancel"

    return _plan_approval_callback

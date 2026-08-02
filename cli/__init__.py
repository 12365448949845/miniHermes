"""
CLI 模块：prompt_toolkit Application 构建与交互。

职责划分：
  state.py       — AppState 共享状态容器
  styles.py      — PTStyle 样式定义
  completers.py  — 输入补全器
  commands.py    — 斜杠命令定义与处理
  clarify.py     — clarify 交互状态机与 UI
  keybindings.py — 键绑定注册
  layout.py      — Application 布局组装
  conversation.py— 后台对话循环线程
"""

from cli.layout import build_app  # noqa: F401

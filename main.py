"""
MiniHermes CLI 入口。

职责：初始化 Provider/Agent/Session，构建 UI，启动对话线程。
具体的 UI 构建、命令处理、对话循环等逻辑拆分到 cli/ 包中。
"""

import asyncio
import os
import sys
import json
import threading

from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.keys import Keys
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES

from agent.agent import Agent
from provider import Provider
from renderer import StreamRenderer, print_welcome, print_error
from session import SessionDB
from cli.state import AppState
from cli.commands import generate_session_id, register_skill_commands
from skills import sync_builtin_skills
from cli.clarify import make_clarify_callback
from cli.approval import make_approval_callback
from cli.conversation import conversation_loop
from cli import build_app
import config as cfg


# ── 换行键序列注册 ───────────────────────────────────────────────────────────

_NEWLINE_KEY_SEQUENCES = (
    "\x1b[13;2u",
    "\x1b[27;2;13~",
    "\x1b[13;2~",
    "\x1b[13;9u",
    "\x1b[13;9~",
)


def _install_newline_key_sequences() -> None:
    """把 Shift+Enter / Command+Enter 终端序列映射为换行按键。"""
    for sequence in _NEWLINE_KEY_SEQUENCES:
        ANSI_SEQUENCES[sequence] = Keys.ControlJ


_install_newline_key_sequences()


# ── 主函数 ────────────────────────────────────────────────────────────────────

def main():
    """启动 MiniHermes 交互式 CLI。"""
    from provider.provider import MODEL_NAME
    from context.compressor import CONTEXT_WINDOW
    model_name = cfg.get_model_config().get("name") or MODEL_NAME
    context_window = CONTEXT_WINDOW

    db = SessionDB()

    try:
        provider = Provider()
    except Exception as e:
        print_error(f"Failed to initialize: {e}")
        sys.exit(1)

    renderer = StreamRenderer()

    # 构建共享状态
    state = AppState(
        model_name=model_name,
        context_window=context_window,
        status_text=f" ⚕ {model_name[:26]}",
    )

    # 初始化 Agent（需要 clarify/approval callback）
    clarify_callback = make_clarify_callback(state)
    approval_callback = make_approval_callback(state)
    try:
        agent = Agent(provider, db=db, clarify_callback=clarify_callback,
                      approval_callback=approval_callback)
    except Exception as e:
        print_error(f"Failed to initialize: {e}")
        sys.exit(1)

    state.agent = agent

    # 创建 Session
    session_id = generate_session_id()
    state.session_id = session_id
    db.create_session(
        session_id, model_name,
        model_config=json.dumps(cfg.get_model_config(), ensure_ascii=False),
        system_prompt=agent.system_prompt,
    )

    # 打印欢迎信息
    import tools as tool_registry
    print_welcome(model_name, tools=tool_registry.get_tool_manager().get_names(), cwd=os.getcwd())

    # 同步内置 skills 并注册 skill 斜杠命令
    sync_builtin_skills()
    register_skill_commands()

    # 构建 Application
    app = build_app(state)

    # 显式创建事件循环，存储到 state 供后台线程通过 run_in_terminal 使用
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    state._main_loop = loop

    # 启动对话线程
    worker = threading.Thread(
        target=conversation_loop,
        args=(state, db, renderer),
        daemon=True,
    )
    worker.start()

    # 运行 UI（使用显式事件循环）
    try:
        with patch_stdout(raw=True):
            loop.run_until_complete(app.run_async())
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        state.should_exit = True
        state.input_queue.put(None)
        db.end_session(session_id, end_reason="user_exit")
        print("\nBye!")


if __name__ == "__main__":
    main()

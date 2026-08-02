"""后台对话循环线程。"""

from pathlib import Path

from agent.agent import Agent
from cli.state import AppState
from cli.commands import handle_slash_command
from cli.plan import (
    PLAN_ALLOWED_TOOLS, PLAN_MODE_PROMPT,
    generate_plan_path, make_plan_approval_callback,
)
from cli.context_ref import preprocess as preprocess_refs
from renderer.renderer import _cprint, _AMBER, _BOLD, _DIM, _GOLD, _RST
from renderer import print_error, StreamRenderer
from session import SessionDB
import config as cfg


EVO_MEMORY_NUDGE_INTERVAL = 10
EVO_SKILL_NUDGE_INTERVAL = 10
EVO_NUDGE_MIN_TURNS = 5


def _try_nudge(agent, state: "AppState", user_turns: int):
    """检查进化系统 nudge 触发条件，满足则后台运行 nudge agent。"""
    evo_cfg = cfg.get_evolution_config()
    if not evo_cfg.get("enabled", False):
        return

    if user_turns < EVO_NUDGE_MIN_TURNS:
        return

    nudge_type = None

    # Memory nudge：按用户对话轮数
    agent.turns_since_memory += 1
    if EVO_MEMORY_NUDGE_INTERVAL > 0 and agent.turns_since_memory >= EVO_MEMORY_NUDGE_INTERVAL:
        nudge_type = "memory"
        agent.turns_since_memory = 0

    # Skill nudge：按工具迭代次数（计数器在 agent 主循环中递增）
    if EVO_SKILL_NUDGE_INTERVAL > 0 and agent.iters_since_skill >= EVO_SKILL_NUDGE_INTERVAL:
        nudge_type = "both" if nudge_type == "memory" else "skill"
        agent.iters_since_skill = 0

    if nudge_type and state.conversation_history:
        from evolution.nudge import spawn_nudge
        spawn_nudge(agent.provider, state.conversation_history, nudge_type)


def _handle_slash_commands(user_input: str, agent, state, db, model_name: str):
    """处理斜杠命令。返回 (is_init_run, handled, should_skip)。

    should_skip 为 True 时调用方应跳过本轮对话。
    """
    is_init_run = user_input.strip().lower().startswith("/init")

    # /setup: 需要 run_in_terminal 桥接，单独处理
    if user_input.strip().lower().startswith("/setup"):
        state.status_text = f" ⚕ {model_name[:26]} │ configuring..."
        state.invalidate()
        from config.setup_wizard import run_setup_cli
        try:
            run_setup_cli(app_loop=state._main_loop)
        except Exception as e:
            print(f"[Setup error: {e}]")
        agent.reset_token_tracking()
        state.status_text = f" ⚕ {model_name[:26]}"
        state.invalidate()
        return is_init_run, True

    handled, history, sid, override_msg = handle_slash_command(
        user_input, state.conversation_history, db, state.session_id,
        agent=agent,
    )
    if handled:
        state.conversation_history = history
        if sid:
            state.session_id = sid
        if user_input.strip().lower().startswith("/compress"):
            agent.request_compress()
        agent.reset_token_tracking()
        state.status_text = f" ⚕ {model_name[:26]}"
        state.invalidate()
        return is_init_run, True

    return is_init_run, False


def _execute_plan_mode(agent, state, db, renderer, model_name: str,
                        user_input: str, plan_description: str):
    """执行 Plan Mode（只读规划 + 审批执行）。返回 (user_input, should_skip)。"""
    state.command_running = True
    state.status_text = f" ⚕ {model_name[:26]} │ [PLAN] analyzing..."
    state.invalidate()

    plan_agent = Agent(
        provider=agent.provider,
        db=db,
        clarify_callback=agent.clarify_callback,
        auto_approve=True,
        tool_filter={"include": PLAN_ALLOWED_TOOLS},
        system_prompt_override=agent.system_prompt + PLAN_MODE_PROMPT,
        max_iterations_override=50,
    )

    try:
        plan_result = plan_agent.run_conversation(
            user_message=user_input, history=[],
            renderer=renderer, session_id=state.session_id,
        )
    except KeyboardInterrupt:
        _cprint(f"\n{_GOLD}⚡ Plan interrupted{_RST}")
        state.command_running = False
        state.status_text = f" ⚕ {model_name[:26]}"
        state.invalidate()
        return user_input, True
    except Exception as e:
        print()
        print_error(f"Plan agent error: {e}")
        state.command_running = False
        state.status_text = f" ⚕ {model_name[:26]}"
        state.invalidate()
        return user_input, True

    # 保存方案到文件
    plan_text = plan_result.final_response
    plan_path = generate_plan_path(plan_description)
    plan_path.write_text(plan_text, encoding="utf-8")
    _cprint(f"\n{_DIM}Plan saved: {plan_path}{_RST}")

    # Phase 2: 审批
    state.status_text = f" ⚕ {model_name[:26]} │ [PLAN] awaiting approval..."
    state.invalidate()

    plan_approval_cb = make_plan_approval_callback(state)
    choice = plan_approval_cb(plan_text, str(plan_path))

    if choice == "execute":
        _cprint(f"\n{_GOLD}▶ Executing plan...{_RST}\n")
        return (
            f"Execute the following approved implementation plan. "
            f"Implement each step in order using the appropriate tools.\n\n"
            f"---\n{plan_text}\n---"
        ), False
    else:
        _cprint(f"\n{_DIM}Plan cancelled.{_RST}")
        state.command_running = False
        state.status_text = f" ⚕ {model_name[:26]}"
        state.invalidate()
        return user_input, True


def _post_process(agent, state, db, result, is_init_run: bool,
                   model_name: str, context_window: int, user_input: str):
    """对话完成后的后处理：init 刷新、状态栏、session 跟踪、nudge。"""
    # /init 完成后：若 minihermes.md 已生成，刷新 system prompt
    if is_init_run:
        target = Path.cwd() / "minihermes.md"
        if target.is_file():
            agent.reload_system_prompt()
            agent.reset_token_tracking()
            _cprint(f"\n{_DIM}[minihermes.md created and loaded into system prompt]{_RST}")

    # 更新状态栏
    if agent.last_prompt_tokens > 0:
        percent = min(100, int(agent.last_prompt_tokens / context_window * 100))
        filled = round((percent / 100) * 8)
        bar = f"[{'█' * filled}{'░' * (8 - filled)}]"
        state.status_text = f" ⚕ {model_name[:26]} │ {bar} {percent}%"
    else:
        state.status_text = f" ⚕ {model_name[:26]}"
    state.invalidate()

    state.conversation_history = result.messages

    # 压缩后 session 可能改变
    if result.session_id and result.session_id != state.session_id:
        state.session_id = result.session_id

    # 第一轮自动设置 title
    user_turns = sum(1 for m in state.conversation_history if m["role"] == "user")
    if user_turns == 1:
        db.set_title(state.session_id, user_input[:10].rstrip())

    # 进化系统：Nudge 触发
    _try_nudge(agent, state, user_turns)

    # 每 20 轮提示
    if user_turns > 0 and user_turns % 20 == 0:
        _cprint(
            f"\n{_DIM}[tip: {user_turns} turns. "
            f"Use /clear to start fresh if responses slow down.]{_RST}"
        )


def conversation_loop(
    state: AppState,
    db: SessionDB,
    renderer: StreamRenderer,
):
    """后台消费用户输入并驱动 agent 对话。在 daemon 线程中运行。"""
    agent = state.agent
    model_name = state.model_name
    context_window = state.context_window

    while not state.should_exit:
        user_input = state.input_queue.get()
        if user_input is None:
            state.should_exit = True
            break

        # ── 斜杠命令 ──────────────────────────────────────────────
        is_init_run = False
        if user_input.startswith("/"):
            is_init_run, handled = _handle_slash_commands(
                user_input, agent, state, db, model_name)
            if handled:
                continue

        # ── Plan mode 检测与消息转换 ───────────────────────────────
        is_plan_mode = user_input.startswith("__PLAN_MODE__:")
        plan_description = ""
        if is_plan_mode:
            plan_description = user_input[len("__PLAN_MODE__:"):]
            user_input = (
                f"Create a detailed implementation plan for:\n\n"
                f"{plan_description}\n\n"
                f"Analyze the codebase thoroughly using read-only tools before producing the plan."
            ) if plan_description else (
                "The user wants you to create an implementation plan. "
                "Ask them what they'd like to plan using the clarify tool, "
                "then analyze the codebase and produce a detailed plan."
            )

        # ── @file 引用预处理 ──────────────────────────────────────
        if "@file:" in user_input:
            ref_result = preprocess_refs(user_input, cwd=Path.cwd())
            for w in ref_result.warnings:
                _cprint(f"{_AMBER}⚠ {w}{_RST}")
            user_input = ref_result.message

        # 用户消息回显
        if is_plan_mode:
            _cprint(f"\n{_AMBER}{'─' * 40}{_RST}")
            _cprint(f"{_BOLD}● [PLAN] {plan_description or '(interactive)'}{_RST}\n")
        else:
            display_text = user_input[:200] + "..." if len(user_input) > 200 else user_input
            _cprint(f"\n{_AMBER}{'─' * 40}{_RST}")
            _cprint(f"{_BOLD}● {display_text}{_RST}\n")

        # ── Plan Mode ─────────────────────────────────────────────
        if is_plan_mode:
            user_input, skip_turn = _execute_plan_mode(
                agent, state, db, renderer, model_name,
                user_input, plan_description,
            )
            if skip_turn:
                continue

        # ── 执行 Agent ────────────────────────────────────────────
        state.command_running = True
        state.status_text = f" ⚕ {model_name[:26]} │ working..."
        state.invalidate()

        try:
            result = agent.run_conversation(
                user_message=user_input,
                history=state.conversation_history,
                renderer=renderer,
                session_id=state.session_id,
            )
        except KeyboardInterrupt:
            _cprint(f"\n{_GOLD}⚡ Interrupted{_RST}")
            state.command_running = False
            state.status_text = f" ⚕ {model_name[:26]}"
            state.invalidate()
            continue
        except Exception as e:
            print()
            print_error(f"Agent error: {e}")
            state.command_running = False
            state.status_text = f" ⚕ {model_name[:26]}"
            state.invalidate()
            continue

        state.command_running = False

        # ── 后处理 ────────────────────────────────────────────────
        _post_process(agent, state, db, result, is_init_run,
                       model_name, context_window, user_input)

    # 退出前尝试运行 curator
    try:
        from evolution.curator import maybe_run_curator
        maybe_run_curator(provider=agent.provider)
    except Exception:
        pass

    db.end_session(state.session_id, end_reason="user_exit")

"""斜杠命令定义与处理。"""

import json
import sys
import time
from datetime import datetime
from typing import Optional
import uuid

from renderer.renderer import _cprint, _DIM, _RST
from renderer import print_resumed_history
from session import SessionDB
from skills import discover_skills, load_skill, load_skill_structured
import config as cfg


SLASH_COMMANDS: dict[str, str] = {
    "/clear":     "Clear conversation history",
    "/compress":  "Manually trigger context compression",
    "/plan":      "Enter plan mode (read-only analysis, then execute)",
    "/init":      "Scan project and generate minihermes.md",
    "/history":   "Show current conversation length",
    "/sessions":  "List recent sessions",
    "/resume":    "Resume a previous session",
    "/title":     "Set title for current session",
    "/sysprompt": "Print current system prompt (debug)",
    "/help":      "Show available commands",
    "/setup":     "Interactive configuration setup",
    "/exit":      "Exit MiniHermes",
    "/quit":      "Exit MiniHermes",
}


_INIT_INSTRUCTION = """[/init] Please analyze this codebase and create a minihermes.md file at the current working directory's root.

What to include:
1. Build/run/test commands used in this project
2. High-level architecture (the "big picture" that requires reading multiple files to understand)
3. Key conventions or patterns that aren't obvious from a single file

Guidelines:
- Read the README.md if present, plus key config files (package.json, pyproject.toml, Cargo.toml, go.mod, etc.)
- Use list_dir and read_file to explore structure; sample a few core source files
- Keep it concise — focus on what a future agent needs to be productive quickly
- Do NOT include obvious instructions ("write tests", "handle errors", "follow security best practices")
- Do NOT enumerate every file or directory — list only what matters
- If a CLAUDE.md / AGENTS.md / .hermes.md / README.md already exists, read it first and adapt key insights

Begin the file with this exact header:

# minihermes.md

This file provides project context to miniHermes when working in this repository.

When done, write the file using the write_file tool with path="minihermes.md", then briefly tell me what you captured.
"""


def generate_session_id() -> str:
    """生成唯一 session id。"""
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{timestamp_str}_{short_uuid}"


def register_skill_commands():
    """启动时扫描 skills 并追加到 SLASH_COMMANDS。"""
    for skill in discover_skills():
        cmd = f"/{skill['name']}"
        if cmd not in SLASH_COMMANDS:
            SLASH_COMMANDS[cmd] = f"[skill] {skill['description']}"


def handle_slash_command(
    cmd: str, history: list, db: SessionDB, session_id: str,
    agent=None,
) -> tuple[bool, list, str, Optional[str]]:
    """
    处理斜杠命令。

    Returns:
        (handled, history, session_id, override_message)
        handled=True 表示命令已处理完毕（主循环应 continue）
        override_message 非 None 时替换原始输入后继续交给 agent
    """
    parts = cmd.strip().split(None, 1)
    command = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if command in ("/exit", "/quit", "/q"):
        db.end_session(session_id, end_reason="user_exit")
        print("Bye!")
        sys.exit(0)

    if command == "/compress":
        print("[manual compression triggered — will execute on next LLM call]")
        return True, history, session_id, None

    if command == "/clear":
        print("\033[2J\033[H", end="")
        print("[history cleared — starting new session]")
        db.end_session(session_id, end_reason="clear")
        new_id = generate_session_id()
        from provider.provider import MODEL_NAME
        model_name = cfg.get_model_config().get("name") or MODEL_NAME
        db.create_session(new_id, model_name,
                          model_config=json.dumps(cfg.get_model_config(), ensure_ascii=False))
        return True, [], new_id, None

    if command == "/history":
        user_turns = sum(1 for m in history if m["role"] == "user")
        print(f"[session: {session_id} | {user_turns} user turns, {len(history)} total messages]")
        return True, history, session_id, None

    if command == "/sessions":
        sessions = db.list_sessions(limit=15)
        if not sessions:
            print("[no sessions found]")
        else:
            print(f"{'ID':<14} {'Messages':>4}  {'Time':<20} Title")
            print("─" * 60)
            for s in sessions:
                t = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
                title = s["title"] or ""
                active = " ◀" if s["id"] == session_id else ""
                print(f"{s['id']:<14} {s['message_count']:>4}  {t:<20} {title}{active}")
        return True, history, session_id, None

    if command == "/resume":
        target_id = arg.strip()
        if not target_id:
            sessions = db.list_sessions(limit=5)
            candidates = [s for s in sessions if s["id"] != session_id and s["message_count"] > 0]
            if not candidates:
                print("[no previous session to resume]")
                return True, history, session_id, None
            target_id = candidates[0]["id"]

        # 解析压缩链路：走到最新的有消息的 session
        resolved_id = db.resolve_resume_session_id(target_id)

        all_msgs = db.get_messages(resolved_id)
        if not all_msgs:
            print(f"[session {resolved_id} has no messages]")
            return True, history, session_id, None

        db.end_session(session_id, end_reason="resumed")
        if resolved_id != target_id:
            print(f"[session {target_id} was compressed → following to {resolved_id}]")
        print(f"[resumed session {resolved_id} with {len(all_msgs)} messages]")
        print_resumed_history(all_msgs)
        llm_msgs = db.get_messages_for_llm(resolved_id)
        return True, llm_msgs, resolved_id, None

    if command == "/title":
        if not arg:
            print("[usage: /title <name>]")
        else:
            db.set_title(session_id, arg)
            print(f"[session titled: {arg.strip()[:100]}]")
        return True, history, session_id, None

    if command == "/sysprompt":
        if agent is None or not getattr(agent, "system_prompt", None):
            print("[no system prompt available]")
            return True, history, session_id, None
        sp = agent.system_prompt
        char_count = len(sp)
        token_estimate = char_count // 4
        print(f"{_DIM}─── system prompt ({char_count} chars, ~{token_estimate} tokens) ───{_RST}")
        print(sp)
        print(f"{_DIM}─── end of system prompt ───{_RST}")
        return True, history, session_id, None

    if command == "/help":
        print(
            "/clear       — clear history & start new session\n"
            "/compress    — manually trigger context compression\n"
            "/history     — show current session info\n"
            "/sessions    — list recent sessions\n"
            "/resume [id] — resume a previous session\n"
            "/title <name>— name the current session\n"
            "/sysprompt   — print current system prompt (debug)\n"
            "/setup       — interactive configuration setup\n"
            "/init        — scan project and generate minihermes.md\n"
            "/exit        — exit MiniHermes\n"
            "Ctrl+C       — interrupt current response\n"
            "Ctrl+D       — exit\n"
            "Shift+Enter / Cmd+Enter — new line (multiline input)"
        )
        return True, history, session_id, None

    if command == "/plan":
        override_msg = f"__PLAN_MODE__:{arg}"
        return False, history, session_id, override_msg

    if command == "/init":
        from pathlib import Path
        target = Path.cwd() / "minihermes.md"
        if target.exists():
            print(f"[minihermes.md already exists at {target}. Delete it first to regenerate.]")
            return True, history, session_id, None
        return False, history, session_id, _INIT_INSTRUCTION

    if command == "/setup":
        # /setup 的实际处理在 conversation.py 中通过 run_in_terminal 完成
        # 如果走到这里说明不在正常对话循环上下文中
        from config.setup_wizard import run_setup_cli
        try:
            run_setup_cli()  # 无 event loop，直接运行（首次向导等场景）
        except Exception as e:
            print(f"[Setup error: {e}]")
        return True, history, session_id, None

    # 尝试匹配 skill（优先用结构化数据，降级到旧版 load_skill）
    skill_name = command.lstrip("/")
    skill_info = load_skill_structured(skill_name)
    if skill_info:
        # Build rich activation message
        lines = [
            f"[IMPORTANT: The user has invoked the '{skill_name}' skill. "
            f"Follow the instructions below unless the user asks otherwise.]",
            "",
        ]
        # Category hint
        if skill_info.get("category"):
            lines.insert(0, f"[Skill category: {skill_info['category']}]")

        # Supporting files hint
        linked = skill_info.get("linked_files", {})
        has_linked = any(v for v in linked.values())
        if has_linked:
            lines.append(f"[This skill has supporting files at {skill_info['skill_dir']}:]")
            for subdir, files in linked.items():
                if files:
                    file_list = ", ".join(files[:5])
                    if len(files) > 5:
                        file_list += f" (+{len(files) - 5} more)"
                    lines.append(f"  {subdir}/: {file_list}")
            lines.append(f"[Use skill_view('{skill_name}', file_path='...') to load a specific file.]")
            lines.append("")

        # Platform warning
        if not skill_info.get("platform_compatible", True):
            lines.append("[WARNING: This skill may not be fully compatible with your current platform.]")
            lines.append("")

        # Setup warning
        if skill_info.get("setup_needed"):
            lines.append(f"[SETUP NEEDED: {skill_info.get('setup_note', 'Some environment variables are missing.')}]")
            lines.append("")

        # Main content
        lines.append(skill_info["content"])

        # User instruction
        if arg:
            lines.append(f"\n\n[User request: {arg}]")

        override_msg = "\n".join(lines)
        return False, history, session_id, override_msg

    # Fallback: try old-school load_skill for backward compat
    skill_content = load_skill(skill_name)
    if skill_content:
        msg = f"[Skill '{skill_name}' loaded. Follow these instructions:]\n\n{skill_content}"
        if arg:
            msg += f"\n\n[User request: {arg}]"
        return False, history, session_id, msg

    print(f"[unknown command: {command}. Type /help for available commands]")
    return True, history, session_id, None

"""
工具调用审批系统：拦截高危 bash 命令和 write_file 敏感操作，要求用户确认。

两层防线：
  1. 硬拦截 (HARDLINE) — 绝对不执行，无法绕过
  2. 软拦截 (DANGEROUS) — 弹出审批 UI，用户确认后可执行

审批 UI 复用 clarify.py 的 tty 箭头选择模式。
"""

import re
from typing import Optional

from renderer import console

# ── 硬拦截模式（绝对不执行）────────────────────────────────────────────────────

HARDLINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\brm\s+(-[^\s]*\s+)*(/|/\*)\s*$', re.IGNORECASE),
     "delete root filesystem"),
    (re.compile(r'\bmkfs\b', re.IGNORECASE),
     "format filesystem"),
    (re.compile(r'\bdd\b.*\bof=/dev/', re.IGNORECASE),
     "overwrite block device"),
    (re.compile(r':\(\)\{\s*:\|:&\s*\};:', re.IGNORECASE),
     "fork bomb"),
    (re.compile(r'\b(shutdown|reboot|halt|poweroff)\b', re.IGNORECASE),
     "system shutdown/reboot"),
    (re.compile(r'\bkill\s+(-9\s+)?1\b', re.IGNORECASE),
     "kill init process"),
    (re.compile(r'\bkill\s+-1\b', re.IGNORECASE),
     "kill all processes"),
]

# ── 软拦截模式（需确认）──────────────────────────────────────────────────────

DANGEROUS_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (compiled regex, pattern_key, description)

    # ── 删除/移除文件 ──
    (re.compile(r'(^|[;&|`]\s*|xargs\s+)\brm\b', re.IGNORECASE | re.MULTILINE),
     "delete_file", "delete file/directory"),
    (re.compile(r'(^|[;&|`]\s*)\brmdir\b', re.IGNORECASE | re.MULTILINE),
     "delete_file", "remove directory"),
    (re.compile(r'(^|[;&|`]\s*)\bunlink\b', re.IGNORECASE | re.MULTILINE),
     "delete_file", "unlink file"),
    (re.compile(r'\bfind\b.*(-delete|--delete|-exec\s+rm)', re.IGNORECASE),
     "delete_file", "find and delete files"),

    # ── 进程终止 ──
    (re.compile(r'\b(kill|pkill|killall)\b', re.IGNORECASE),
     "kill_process", "terminate process"),

    # ── 权限修改 ──
    (re.compile(r'\bchmod\s+(-R\s+)?(777|666)', re.IGNORECASE),
     "chmod_dangerous", "dangerous permission change"),
    (re.compile(r'\bchown\s+-R\b', re.IGNORECASE),
     "chown_recursive", "recursive ownership change"),

    # ── Git 破坏性操作 ──
    (re.compile(r'\bgit\s+reset\s+--hard\b', re.IGNORECASE),
     "git_destructive", "git reset --hard (destroys uncommitted changes)"),
    (re.compile(r'\bgit\s+push\s+.*--force\b', re.IGNORECASE),
     "git_destructive", "git force push (rewrites remote history)"),
    (re.compile(r'\bgit\s+clean\s+-[^\s]*f', re.IGNORECASE),
     "git_destructive", "git clean (remove untracked files)"),
    (re.compile(r'\bgit\s+checkout\s+--\s+\.', re.IGNORECASE),
     "git_destructive", "git checkout -- . (discard all changes)"),
    (re.compile(r'\bgit\s+stash\s+drop', re.IGNORECASE),
     "git_destructive", "git stash drop (discard stashed changes)"),

    # ── 远程代码执行 ──
    (re.compile(r'\b(curl|wget)\b.*\|\s*(ba)?sh', re.IGNORECASE),
     "pipe_to_shell", "pipe remote content to shell"),

    # ── 提权 ──
    (re.compile(r'\bsudo\b', re.IGNORECASE),
     "sudo", "elevated privileges (sudo)"),

    # ── 数据库破坏 ──
    (re.compile(r'\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)', re.IGNORECASE),
     "db_destructive", "destructive database operation"),
    (re.compile(r'\bDELETE\s+FROM\b(?!.*WHERE)', re.IGNORECASE),
     "db_delete_no_where", "DELETE without WHERE clause"),

    # ── 敏感路径写入（bash 重定向/cp/mv）──
    (re.compile(r'(>|cp\s|mv\s).*(/etc/|/etc\b)', re.IGNORECASE),
     "modify_etc", "write/copy/move to /etc/"),
    (re.compile(r'(>|cp\s|mv\s).*~?/\.ssh/', re.IGNORECASE),
     "modify_ssh", "write/copy/move to ~/.ssh/"),
    (re.compile(r'(>|cp\s|mv\s).*\.env\b', re.IGNORECASE),
     "modify_env", "write/copy/move to .env file"),

    # ── 包管理卸载 ──
    (re.compile(r'\bpip\s+uninstall\b', re.IGNORECASE),
     "pip_uninstall", "uninstall Python package"),
    (re.compile(r'\bnpm\s+(uninstall|rm)\b', re.IGNORECASE),
     "npm_uninstall", "uninstall npm package"),
    (re.compile(r'\bbrew\s+(uninstall|remove)\b', re.IGNORECASE),
     "brew_uninstall", "uninstall Homebrew package"),
    (re.compile(r'\bdiskutil\s+erase', re.IGNORECASE),
     "diskutil_erase", "erase disk"),
]

# ── write_file 敏感路径模式 ──────────────────────────────────────────────────

SENSITIVE_PATH_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r'(^|/)\.env(\.\w+)?$'),
     "write_env_file", "write to .env file"),
    (re.compile(r'(^|/)\.ssh/'),
     "write_ssh", "write to .ssh/ directory"),
    (re.compile(r'^/etc/'),
     "write_etc", "write to /etc/"),
    (re.compile(r'(credentials|secrets|token)', re.IGNORECASE),
     "write_credentials", "write to credentials/secrets file"),
    (re.compile(r'(^|/)\.gitconfig$|\.git/config$'),
     "write_git_config", "write to git config"),
    (re.compile(r'(^|/)\.(bashrc|zshrc|profile|bash_profile)$'),
     "write_shell_config", "write to shell config"),
    (re.compile(r'(^|/)(package\.json|Pipfile|requirements\.txt|pyproject\.toml)$'),
     "write_pkg_config", "write to package manager config"),
]

# write_file 敏感内容模式
SENSITIVE_CONTENT_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r'(API_KEY|SECRET|PASSWORD|TOKEN)\s*[=:]\s*\S{6,}', re.IGNORECASE),
     "content_has_secret", "content contains embedded secrets"),
]

# ── Session 白名单 ───────────────────────────────────────────────────────────

_session_approved: set[str] = set()


def reset_session():
    """清空 session 白名单（session 切换时调用）。"""
    _session_approved.clear()


# ── 检查函数 ─────────────────────────────────────────────────────────────────

def _check_command(command: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    检查 bash 命令安全性。
    返回: (action, pattern_key, description)
      action: "allow" | "block" | "confirm"
    """
    for pattern, desc in HARDLINE_PATTERNS:
        if pattern.search(command):
            return ("block", None, desc)

    for pattern, key, desc in DANGEROUS_PATTERNS:
        if pattern.search(command):
            if key in _session_approved:
                return ("allow", key, None)
            return ("confirm", key, desc)

    return ("allow", None, None)


def _check_write_file(path: str, content: str) -> tuple[str, Optional[str], Optional[str]]:
    """
    检查 write_file 操作安全性。
    返回: (action, pattern_key, description)
      action: "allow" | "confirm"（write_file 无硬拦截）
    """
    for pattern, key, desc in SENSITIVE_PATH_PATTERNS:
        if pattern.search(path):
            if key in _session_approved:
                return ("allow", key, None)
            return ("confirm", key, desc)

    content_head = content[:2000]
    for pattern, key, desc in SENSITIVE_CONTENT_PATTERNS:
        if pattern.search(content_head):
            if key in _session_approved:
                return ("allow", key, None)
            return ("confirm", key, desc)

    return ("allow", None, None)


# ── 审批 UI（fallback，无 TUI 时使用）─────────────────────────────────────────

def _prompt_approval(tool_name: str, args: dict, description: str) -> str:
    """
    Fallback 审批 UI（radiolist_dialog）。
    当 Agent 没有注入 approval_callback 时使用（如非交互模式）。
    返回: "once" | "session" | "deny"
    """
    from prompt_toolkit.shortcuts import radiolist_dialog
    from prompt_toolkit.formatted_text import HTML

    if tool_name == "bash":
        detail = args.get("command", "")
    else:
        detail = args.get("path", "")

    console.print(f"\n[bold #FF6B35]⚠  Dangerous: {description}[/bold #FF6B35]")
    preview = detail[:120]
    if len(detail) > 120:
        preview += "..."
    console.print(f"   [dim]{preview}[/dim]\n")

    values = [
        ("once", "Allow once"),
        ("session", "Allow for session"),
        ("deny", "Deny"),
    ]

    try:
        result = radiolist_dialog(
            title=HTML("<ansiyellow>Permission Required</ansiyellow>"),
            text="Select an action:",
            values=values,
            default="once",
        ).run()
    except (EOFError, KeyboardInterrupt):
        result = "deny"

    if result is None:
        result = "deny"

    label_map = {"once": "Allow once", "session": "Allow for session", "deny": "Deny"}
    style = "#FF6B35" if result == "deny" else "#FFBF00"
    console.print(f"  [{style}]→ {label_map[result]}[/{style}]\n")
    return result

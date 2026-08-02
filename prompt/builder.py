"""
系统提示词构建模块。

层次设计（参考 hermes agent/prompt_builder.py 精简）：
  Layer 1:  身份层      — ~/.minihermes/SOUL.md，fallback 到硬编码常量
  Layer 7:  记忆层      — MemoryStore 冻结快照注入（session 内不变）
  Layer 9:  上下文文件  — .hermes.md / AGENTS.md / CLAUDE.md / .cursorrules
                          优先级递减，第一个匹配生效，含注入检测 + 截断
  Layer 10: 时间戳      — 对话开始时间 + 模型名
  Layer 11: 环境提示    — 动态检测运行环境（macOS / Linux / WSL / Windows）
  Layer 12: 平台提示    — CLI 格式化指引（固定）
"""

import json
import os
import platform
import re
import tempfile
import threading
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import MINIHERMES_HOME

# ── Layer 1: 默认身份（SOUL.md 不存在时的兜底）────────────────────────────────

DEFAULT_IDENTITY = """\
You are MiniHermes, a concise and capable AI assistant.
Use tools proactively when needed. Think step by step before acting.
Always prefer using tools over making assumptions about the environment.\
"""

# ── 工具行为引导（按需条件注入，工具存在才加）────────────────────────────────

MEMORY_GUIDANCE = (
    "You have persistent memory across sessions. "
    "Save durable facts using the memory tool: user preferences, environment details, "
    "tool quirks, and stable conventions. "
    "Write memories as declarative facts, not instructions to yourself. "
    "'User prefers concise responses' ✓  —  'Always respond concisely' ✗. "
    "Do NOT save task progress or temporary state to memory."
)

CLARIFY_GUIDANCE = (
    "# When to ask clarifying questions\n"
    "Use the clarify tool PROACTIVELY when:\n"
    "- The user's request is ambiguous and multiple valid interpretations exist\n"
    "- A decision has meaningful trade-offs the user should weigh in on\n"
    "- You need to choose between approaches that significantly differ in scope or outcome\n"
    "- Required context is missing and CANNOT be retrieved by other tools\n\n"
    "Do NOT use clarify for:\n"
    "- Simple yes/no confirmations you can infer from context\n"
    "- Information retrievable via search, file reading, or other tools\n"
    "- Trivial decisions that don't materially affect the outcome\n\n"
    "Prefer retrieving information with tools over asking the user. "
    "But when genuinely uncertain about intent, asking is ALWAYS better than guessing wrong."
)

TODO_GUIDANCE = (
    "# Task planning with todo\n"
    "Use the todo tool PROACTIVELY when:\n"
    "- The task requires 3 or more distinct steps\n"
    "- The user provides multiple tasks or a list of requirements\n"
    "- The work is complex enough that tracking progress prevents missed steps\n\n"
    "Create the todo list BEFORE starting work, not after. "
    "Keep exactly ONE item in_progress at a time. "
    "Mark items completed immediately when done — do not batch completions. "
    "If a task turns out to be unnecessary, remove it from the list entirely.\n\n"
    "Do NOT use todo for single-step tasks or trivial operations."
)

CODE_EXECUTION_GUIDANCE = (
    "# Cloud code execution\n"
    "Use execute_code for small code snippets, data processing, algorithm checks, "
    "or generating intermediate results in a Qiniu E2B-compatible cloud sandbox. "
    "Do NOT use it for interactive scripts or tasks that require local project files "
    "unless you explicitly create/provide those files inside the sandbox. "
    "Do not print secrets or credentials from code. "
    "Use read_file, write_file, or bash for local filesystem work."
)

DELEGATE_GUIDANCE = (
    "# Task delegation\n"
    "Use delegate_task to offload self-contained subtasks to an independent subagent. "
    "Good candidates: research, file analysis, code generation, multi-step tool operations.\n"
    "The subagent has its own fresh context and cannot see your conversation history.\n\n"
    "Guidelines:\n"
    "- Provide clear, actionable task descriptions\n"
    "- Include ALL necessary context (the subagent starts from zero)\n"
    "- Use for tasks that don't require user judgment or approval\n"
    "- Prefer doing simple tasks directly over delegating them\n"
    "- Do NOT delegate trivial one-step operations"
)

SKILL_MANAGE_GUIDANCE = (
    "# Skill evolution\n"
    "Use skill_manage to create reusable skills when you discover a pattern worth preserving.\n\n"
    "When to create a skill:\n"
    "- A non-trivial technique, fix, or workflow emerged that would clearly recur\n"
    "- The user corrected your approach in a way that applies broadly\n"
    "- You solved a complex multi-step problem (5+ tool calls)\n\n"
    "Skill design principles:\n"
    "- CLASS-LEVEL umbrellas, not narrow one-offs: 'python-testing' ✓, 'fix-pytest-in-foo' ✗\n"
    "- Include: When to Use, Procedure (numbered steps), Pitfalls, Verification\n"
    "- Patch existing skills rather than creating overlapping new ones\n"
    "- Keep skills general enough to apply across projects\n\n"
    "Use 'patch' when a loaded skill turns out wrong or missing steps. "
    "Use 'archive' when a skill is clearly obsolete."
)

# 工具引导映射（模块级常量，避免每次 build_system_prompt 重建）
_TOOL_GUIDANCE: dict[str, str] = {}

# ── Layer 12: 平台提示（固定 CLI）────────────────────────────────────────────

CLI_PLATFORM_HINT = """\
You are a CLI AI Agent. Try not to use markdown but simple text renderable inside a terminal.
File delivery: there is no attachment channel — the user reads your response directly in their
terminal. When referring to a file you created or changed, just state its absolute path in plain
text; the user can open it from there.\
"""

# ── Layer 11: 环境提示常量 ───────────────────────────────────────────────────

_ENV_HINTS = {
    "wsl": (
        "You are running inside WSL (Windows Subsystem for Linux). "
        "The Windows host filesystem is mounted under /mnt/ — "
        "/mnt/c/ is the C: drive, /mnt/d/ is D:, etc. "
        "The user's Windows files are typically at "
        "/mnt/c/Users/<username>/Desktop/, Documents/, Downloads/, etc. "
        "When the user references Windows paths or desktop files, translate "
        "to the /mnt/c/ equivalent. You can list /mnt/c/Users/ to discover "
        "the Windows username if needed."
    ),
    "macos": (
        "You are running on macOS. "
        "Package management is typically via Homebrew (/opt/homebrew on Apple Silicon, "
        "/usr/local on Intel). Use 'open <path>' to open files in the default app. "
        "The user's home directory is /Users/<username>."
    ),
    "linux": (
        "You are running on Linux."
    ),
    "windows": (
        "You are running on Windows. "
        "The default shell is PowerShell or cmd. "
        "Use backslashes for paths (e.g. C:\\Users\\<username>\\Desktop) "
        "or forward slashes where supported. "
        "Package management is typically via winget, Chocolatey, or Scoop."
    ),
}

# ── Layer 9: 注入检测模式 ────────────────────────────────────────────────────

_THREAT_PATTERNS = [
    (r'ignore\s+(previous|all|above|prior)\s+instructions',   "prompt_injection"),
    (r'do\s+not\s+tell\s+the\s+user',                         "deception_hide"),
    (r'system\s+prompt\s+override',                           "sys_prompt_override"),
    (r'disregard\s+(your|all|any)\s+(instructions|rules)',    "disregard_rules"),
    (r'act\s+as\s+(if|though)\s+you\s+(have\s+no|don\'t\s+have)\s+(restrictions|limits)', "bypass_restrictions"),
    (r'<!--[^>]*(?:ignore|override|system|secret|hidden)[^>]*-->', "html_comment_injection"),
]

_INVISIBLE_CHARS = {'\u200b', '\u200c', '\u200d', '\u2060', '\ufeff'}

CONTEXT_MAX_CHARS = 20_000


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _scan_for_injection(content: str, filename: str) -> str:
    """扫描内容是否含 prompt injection，发现则返回 BLOCKED 占位。"""
    findings = []
    for char in _INVISIBLE_CHARS:
        if char in content:
            findings.append(f"invisible unicode U+{ord(char):04X}")
    for pattern, pid in _THREAT_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            findings.append(pid)
    if findings:
        return (
            f"[BLOCKED: {filename} contained potential prompt injection "
            f"({', '.join(findings)}). Content not loaded.]"
        )
    return content


def _truncate_content(content: str, filename: str, max_chars: int = CONTEXT_MAX_CHARS) -> str:
    """超出上限时保留头部 70% + 尾部 20%，中间插入截断标记。"""
    if len(content) <= max_chars:
        return content
    head = content[:int(max_chars * 0.7)]
    tail = content[-int(max_chars * 0.2):]
    marker = (
        f"\n\n[...truncated {filename}: kept first 70% + last 20% "
        f"of {len(content)} chars. Use file tools to read the full file.]\n\n"
    )
    return head + marker + tail


def _load_file(path: Path, name: str) -> str:
    """读取单个上下文文件，注入检测 + 截断，返回带标题的块。"""
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return ""
        content = _scan_for_injection(content, name)
        result = f"## {name}\n\n{content}"
        return _truncate_content(result, name)
    except OSError:
        return ""


def _find_git_root(start: Path) -> Optional[Path]:
    """向上搜索 .git 目录，返回包含它的目录；找不到返回 None。"""
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    return None


# ── Layer 1 ───────────────────────────────────────────────────────────────────

def load_soul_md() -> Optional[str]:
    """加载 ~/.minihermes/SOUL.md 作为身份覆盖，含注入检测。文件不存在时自动创建并写入默认内容。"""
    soul_path = MINIHERMES_HOME / "SOUL.md"
    if not soul_path.exists():
        MINIHERMES_HOME.mkdir(parents=True, exist_ok=True)
        soul_path.write_text(DEFAULT_IDENTITY, encoding="utf-8")
        return DEFAULT_IDENTITY
    try:
        content = soul_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        return _scan_for_injection(content, "SOUL.md")
    except OSError:
        return None


# ── Layer 9 ───────────────────────────────────────────────────────────────────

def build_context_files_prompt(cwd: Optional[str] = None) -> str:
    """
    按优先级发现并加载上下文文件，第一个匹配的生效（互斥）。

    优先级：
      0. minihermes.md          — 从 cwd 向上搜索到 git root（/init 生成）
      1. .hermes.md / HERMES.md  — 从 cwd 向上搜索到 git root
      2. AGENTS.md / agents.md   — 仅 cwd
      3. CLAUDE.md / claude.md   — 仅 cwd
      4. .cursorrules             — 仅 cwd
    """
    cwd_path = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    git_root = _find_git_root(cwd_path)

    # minihermes.md — 向上搜索（/init 命令生成的项目自描述文件）
    minihermes_md = ""
    search = cwd_path
    while True:
        candidate = search / "minihermes.md"
        if candidate.is_file():
            minihermes_md = _load_file(candidate, "minihermes.md")
            break
        if git_root and search == git_root:
            break
        parent = search.parent
        if parent == search:
            break
        search = parent

    project_ctx = minihermes_md

    # .hermes.md / HERMES.md — 向上搜索
    if not project_ctx:
        hermes_md = ""
        search = cwd_path
        while True:
            for name in (".hermes.md", "HERMES.md"):
                candidate = search / name
                if candidate.is_file():
                    hermes_md = _load_file(candidate, name)
                    break
            if hermes_md:
                break
            if git_root and search == git_root:
                break
            parent = search.parent
            if parent == search:
                break
            search = parent
        project_ctx = hermes_md

    # AGENTS.md — cwd 仅
    if not project_ctx:
        for name in ("AGENTS.md", "agents.md"):
            p = cwd_path / name
            if p.exists():
                project_ctx = _load_file(p, name)
                break

    # CLAUDE.md — cwd 仅
    if not project_ctx:
        for name in ("CLAUDE.md", "claude.md"):
            p = cwd_path / name
            if p.exists():
                project_ctx = _load_file(p, name)
                break

    # .cursorrules + .cursor/rules/*.mdc — cwd 仅
    if not project_ctx:
        cursor_content = ""
        p = cwd_path / ".cursorrules"
        if p.exists():
            cursor_content += _load_file(p, ".cursorrules")
        cursor_dir = cwd_path / ".cursor" / "rules"
        if cursor_dir.is_dir():
            for mdc in sorted(cursor_dir.glob("*.mdc")):
                cursor_content += _load_file(mdc, f".cursor/rules/{mdc.name}")
        if cursor_content:
            project_ctx = cursor_content

    if not project_ctx:
        return ""

    return (
        "# Project Context\n\n"
        "The following project context files have been loaded and should be followed:\n\n"
        + project_ctx
    )


# ── Layer 11 ──────────────────────────────────────────────────────────────────

def _detect_env() -> str:
    """
    动态检测当前运行环境，返回环境标识符。

    检测顺序：
      1. WSL   — platform.system() == "Linux" 且 /proc/version 含 "microsoft"
      2. macOS — platform.system() == "Darwin"
      3. Windows — platform.system() == "Windows"
      4. Linux — 其余 Linux
    """
    system = platform.system()
    if system == "Linux":
        try:
            proc_version = Path("/proc/version").read_text(encoding="utf-8").lower()
            if "microsoft" in proc_version:
                return "wsl"
        except OSError:
            pass
        return "linux"
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    return ""


def build_env_hint() -> str:
    """返回当前环境对应的提示字符串；未知环境返回空字符串。"""
    env = _detect_env()
    return _ENV_HINTS.get(env, "")


def build_env_block(cwd: Optional[str] = None) -> str:
    """构建 <env> 事实块：工作目录、平台、OS 版本、日期。

    与 build_env_hint()（行为引导）互补：env block 是事实信息，hint 是建议。
    """
    cwd_path = str(Path(cwd).resolve()) if cwd else str(Path.cwd().resolve())
    env_id = _detect_env() or "unknown"
    os_version = platform.platform()
    today = datetime.now().strftime("%Y/%m/%d")

    return (
        "<env>\n"
        f"Working directory: {cwd_path}\n"
        f"Platform: {env_id}\n"
        f"OS Version: {os_version}\n"
        f"Today's date: {today}\n"
        "</env>"
    )


# ── Skills cache (two-layer: in-process LRU + disk snapshot) ────────────────────

_SKILLS_PROMPT_CACHE: OrderedDict = OrderedDict()
_SKILLS_PROMPT_CACHE_MAX = 8
_SKILLS_PROMPT_CACHE_LOCK = threading.Lock()


def _skills_prompt_snapshot_path() -> Path:
    return MINIHERMES_HOME / ".skills_prompt_snapshot.json"


def _build_skills_manifest() -> dict:
    """Build a manifest of all SKILL.md files keyed by relative path → [mtime_ns, size]."""
    from skills.manager import _get_skills_dirs, iter_skill_index_files

    manifest = {}
    for skills_dir in _get_skills_dirs():
        if not skills_dir.is_dir():
            continue
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            try:
                st = skill_file.stat()
                rel = str(skill_file)
                manifest[rel] = [st.st_mtime_ns, st.st_size]
            except OSError:
                continue
    return manifest


def _load_skills_snapshot() -> Optional[str]:
    """Try to load cached skills prompt from disk snapshot, validating against manifest."""
    snap_path = _skills_prompt_snapshot_path()
    if not snap_path.is_file():
        return None

    try:
        data = json.loads(snap_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    cached_manifest = data.get("manifest", {})
    prompt_text = data.get("prompt", "")

    # Validate manifest matches current files
    current_manifest = _build_skills_manifest()
    if cached_manifest == current_manifest:
        return prompt_text

    return None


def _write_skills_snapshot(prompt_text: str):
    """Atomically write the skills prompt + manifest to disk snapshot."""
    manifest = _build_skills_manifest()
    data = {"manifest": manifest, "prompt": prompt_text}

    snap_path = _skills_prompt_snapshot_path()
    snap_path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write: tempfile + os.replace
    fd, tmp = tempfile.mkstemp(dir=str(snap_path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data, ensure_ascii=False).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, str(snap_path))


def _skill_should_show(
    conditions: dict,
    available_tools: Optional[set] = None,
    available_toolsets: Optional[set] = None,
) -> bool:
    """Determine if a skill should appear in the index based on conditional activation rules.

    Rules (from frontmatter ``metadata.hermes``):
      - ``fallback_for_tools``: hide when any listed tool IS available (skill is a fallback)
      - ``requires_tools``: hide when any listed tool is NOT available
      - ``fallback_for_toolsets``: hide when any listed toolset IS available
      - ``requires_toolsets``: hide when any listed toolset is NOT available
      - No conditions declared → always show (backward-compatible default)
    """
    tools = available_tools or set()
    toolsets = available_toolsets or set()

    # Hide if skill is a fallback for an available tool
    for t in conditions.get("fallback_for_tools", []):
        if t in tools:
            return False

    # Hide if skill requires a tool that's not available
    for t in conditions.get("requires_tools", []):
        if t not in tools:
            return False

    # Hide if skill is a fallback for an available toolset
    for ts in conditions.get("fallback_for_toolsets", []):
        if ts in toolsets:
            return False

    # Hide if skill requires a toolset that's not available
    for ts in conditions.get("requires_toolsets", []):
        if ts not in toolsets:
            return False

    return True


def _get_skills_prompt_cached(
    available_tools: Optional[set] = None,
    available_toolsets: Optional[set] = None,
) -> str:
    """Get the skills index prompt text, using two-layer cache when possible.

    Strategy: in-process LRU → disk snapshot (mtime validated) → filesystem scan.
    Filters skills based on conditional activation rules.
    """
    from skills.manager import _get_skills_dirs

    skills_dirs = tuple(str(d) for d in _get_skills_dirs())
    tools_key = tuple(sorted(available_tools)) if available_tools else ()
    toolsets_key = tuple(sorted(available_toolsets)) if available_toolsets else ()
    cache_key = (skills_dirs, tools_key, toolsets_key)

    # Layer 1: in-process LRU cache
    with _SKILLS_PROMPT_CACHE_LOCK:
        if cache_key in _SKILLS_PROMPT_CACHE:
            _SKILLS_PROMPT_CACHE.move_to_end(cache_key)
            return _SKILLS_PROMPT_CACHE[cache_key]

    # Layer 2: disk snapshot (validated by mtime/size manifest — tools-agnostic base)
    # We still use the snapshot but then apply conditional filtering in-memory
    base_cached = _load_skills_snapshot()
    if base_cached is not None:
        # Apply conditional filtering if tools are specified
        if available_tools or available_toolsets:
            filtered = _build_filtered_skills_index(available_tools, available_toolsets)
        else:
            filtered = base_cached
        with _SKILLS_PROMPT_CACHE_LOCK:
            if cache_key not in _SKILLS_PROMPT_CACHE:
                _SKILLS_PROMPT_CACHE[cache_key] = filtered
                if len(_SKILLS_PROMPT_CACHE) > _SKILLS_PROMPT_CACHE_MAX:
                    _SKILLS_PROMPT_CACHE.popitem(last=False)
        return filtered

    # Layer 3: filesystem scan with filtering
    prompt_text = _build_filtered_skills_index(available_tools, available_toolsets)

    # Populate disk snapshot with base (unfiltered) version for reuse
    from skills import build_skills_index
    _write_skills_snapshot(build_skills_index())

    with _SKILLS_PROMPT_CACHE_LOCK:
        if cache_key not in _SKILLS_PROMPT_CACHE:
            _SKILLS_PROMPT_CACHE[cache_key] = prompt_text
            if len(_SKILLS_PROMPT_CACHE) > _SKILLS_PROMPT_CACHE_MAX:
                _SKILLS_PROMPT_CACHE.popitem(last=False)

    return prompt_text


def _build_filtered_skills_index(
    available_tools: Optional[set] = None,
    available_toolsets: Optional[set] = None,
) -> str:
    """Build skills index, filtering out skills that don't match conditional activation rules."""
    from skills.manager import discover_skills, parse_frontmatter, extract_skill_conditions, _get_skills_dirs

    if not available_tools and not available_toolsets:
        from skills import build_skills_index
        return build_skills_index()

    # We need to discover with full frontmatter to check conditions
    raw_skills = discover_skills()
    filtered = []
    for s in raw_skills:
        # Read frontmatter to get conditions
        try:
            content = s["path"].read_text(encoding="utf-8")
            fm, _ = parse_frontmatter(content)
            conditions = extract_skill_conditions(fm)
            if _skill_should_show(conditions, available_tools, available_toolsets):
                filtered.append(s)
        except OSError:
            continue

    if not filtered:
        return ""

    lines = [
        "## Available Skills",
        "If a task matches a skill below, load it with the skill_view tool before proceeding.",
        "",
    ]

    # Group by category
    categorized: dict = {}
    uncategorized = []
    for s in filtered:
        cat = s.get("category")
        if cat:
            categorized.setdefault(cat, []).append(s)
        else:
            uncategorized.append(s)

    for cat_name in sorted(categorized.keys()):
        lines.append(f"  {cat_name}:")
        for s in categorized[cat_name]:
            lines.append(f"    - {s['name']}: {s['description']}")

    if uncategorized:
        if categorized:
            lines.append("  uncategorized:")
        for s in uncategorized:
            if categorized:
                lines.append(f"    - {s['name']}: {s['description']}")
            else:
                lines.append(f"- {s['name']}: {s['description']}")

    return "\n".join(lines)


def clear_skills_system_prompt_cache():
    """Clear both in-process and disk caches for skills prompt.

    Call this after skill mutations (create/edit/patch/archive/restore/sync).
    """
    with _SKILLS_PROMPT_CACHE_LOCK:
        _SKILLS_PROMPT_CACHE.clear()

    snap_path = _skills_prompt_snapshot_path()
    try:
        snap_path.unlink(missing_ok=True)
    except OSError:
        pass


# ── 主入口 ────────────────────────────────────────────────────────────────────

def build_system_prompt(
    model_name: str = "",
    memory_store=None,
    cwd: Optional[str] = None,
    tool_names: Optional[set] = None,
) -> str:
    """
    组装完整系统提示词（每 session 调用一次）。

    Args:
        model_name:   模型名，注入时间戳行
        memory_store: MemoryStore 实例，提供冻结快照；为 None 时跳过记忆层
        cwd:          工作目录，用于上下文文件发现；为 None 时用 os.getcwd()
        tool_names:   已注册工具名集合，用于条件注入工具行为引导
    """
    _tools = tool_names or set()
    parts = []

    # Layer 1: 身份
    soul = load_soul_md()
    parts.append(soul if soul else DEFAULT_IDENTITY)

    # 工具行为引导：按注册工具条件注入，数据驱动
    # 首次调用时初始化模块级映射，后续调用复用
    if not _TOOL_GUIDANCE:
        _TOOL_GUIDANCE.update({
            "memory": MEMORY_GUIDANCE,
            "clarify": CLARIFY_GUIDANCE,
            "todo": TODO_GUIDANCE,
            "execute_code": CODE_EXECUTION_GUIDANCE,
            "delegate_task": DELEGATE_GUIDANCE,
            "skill_manage": SKILL_MANAGE_GUIDANCE,
        })
    for tool_name in _tools:
        guidance = _TOOL_GUIDANCE.get(tool_name)
        if guidance:
            parts.append(guidance)

    # Layer 7: 记忆（冻结快照，session 内不变）
    if memory_store:
        mem_block = memory_store.format_for_system_prompt("memory")
        if mem_block:
            parts.append(mem_block)
        user_block = memory_store.format_for_system_prompt("user")
        if user_block:
            parts.append(user_block)

    # Layer 9: 上下文文件
    ctx = build_context_files_prompt(cwd)
    if ctx:
        parts.append(ctx)

    # Skills 索引（走两层缓存，含条件激活过滤）
    skills_idx = _get_skills_prompt_cached(available_tools=_tools)
    if skills_idx:
        parts.append(skills_idx)

    # Layer 10: 时间戳
    now = datetime.now()
    ts = f"Conversation started: {now.strftime('%A, %B %d, %Y %I:%M %p')}"
    if model_name:
        ts += f"\nModel: {model_name}"
    parts.append(ts)

    # Layer 10.5: 环境事实块（cwd / platform / OS / date）
    parts.append(build_env_block(cwd))

    # Layer 11: 环境提示（动态检测）
    env = build_env_hint()
    if env:
        parts.append(env)

    # Layer 12: 平台提示（CLI 固定）
    parts.append(CLI_PLATFORM_HINT)

    return "\n\n".join(p.strip() for p in parts if p.strip())

"""
文件操作工具：read_file / write_file / list_dir。
"""

from pathlib import Path
from tools import register


# ── read_file ────────────────────────────────────────────────────────────────

_MAX_LINES = 2000
_MAX_LINE_LENGTH = 2000
_MAX_FILE_SIZE = 1 * 1024 * 1024  # 1MB — 超过直接拒绝读取
_MAX_OUTPUT_CHARS = 80_000


def _add_line_numbers(lines: list[str], start: int) -> str:
    """给行列表加行号，格式：6位右对齐 + 竖线 + 内容。超长行截断。"""
    numbered = []
    for i, line in enumerate(lines, start=start):
        if len(line) > _MAX_LINE_LENGTH:
            line = line[:_MAX_LINE_LENGTH] + "... [truncated]"
        numbered.append(f"{i:6d}|{line}")
    return "\n".join(numbered)


@register({
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file with line-based pagination. Returns numbered lines. Use offset and limit to page through large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Starting line number (1-based, default: 1).",
                    "default": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of lines to read (default: 500, max: 2000).",
                    "default": 500,
                },
            },
            "required": ["path"],
        },
    },
})
def read_file(path: str, offset: int = 1, limit: int = 500) -> str:
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return f"Error: file not found: {path}"
        if not p.is_file():
            return f"Error: not a file: {path}"

        file_size = p.stat().st_size
        if file_size > _MAX_FILE_SIZE:
            return (
                f"Error: file too large ({_format_size(file_size)}, limit {_format_size(_MAX_FILE_SIZE)}). "
                f"Use bash tool with 'head -n 100 {path}' or 'sed -n \"1,50p\" {path}' to read partial content."
            )

        offset = max(1, offset)
        limit = max(1, min(limit, _MAX_LINES))

        selected = []
        total = 0
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                total = i
                if i < offset:
                    continue
                if len(selected) < limit:
                    selected.append(line.rstrip("\n\r"))

        if not selected:
            return f"Error: offset {offset} is beyond end of file ({total} lines)."

        result = _add_line_numbers(selected, start=offset)

        if len(result) > _MAX_OUTPUT_CHARS:
            result = result[:_MAX_OUTPUT_CHARS]
            result += f"\n\n[OUTPUT TRUNCATED at {_MAX_OUTPUT_CHARS} chars. Use smaller limit or specific offset to read less.]"
            return result

        shown_end = offset + len(selected) - 1
        if shown_end < total:
            result += f"\n\n[Showing lines {offset}-{shown_end} of {total}. Use offset={shown_end + 1} to see more.]"
        else:
            result += f"\n\n[End of file. Total: {total} lines.]"

        return result
    except Exception as e:
        return f"Error reading file: {e}"


# ── write_file ───────────────────────────────────────────────────────────────

# 单次 write_file 调用允许的最大 content 字符数。
# 由来：上游 LLM 网关对单次流式响应的字节量有限制（实测 ~130KB 字节、~48K 字符即被
# 强制断流），超过会导致工具参数 JSON 截断、整轮调用失败。设为 8000 字符是对中文
# UTF-8（每字 3 字节）和英文都安全的阈值，对应 SSE 字节量约 24-32KB。
MAX_WRITE_CHARS = 8000


@register({
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            f"Write text content to a file. Creates the file and parent directories if they don't exist.\n\n"
            f"⚠ IMPORTANT — content length limit: each call accepts at most {MAX_WRITE_CHARS} characters in `content`. "
            f"For longer files (essays, code modules, reports >{MAX_WRITE_CHARS} chars):\n"
            f"  1. First call: write the first chunk with `append=false` (creates/overwrites the file).\n"
            f"  2. Subsequent calls: write each next chunk with `append=true` to extend the same file.\n"
            f"Split on natural boundaries (paragraph / function / section breaks). Do NOT attempt to send "
            f"the entire content in one call — the streaming response will be truncated by the upstream "
            f"gateway and the call will fail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to write to.",
                },
                "content": {
                    "type": "string",
                    "description": (
                        f"The text content to write. Maximum {MAX_WRITE_CHARS} characters per call — "
                        f"chunk longer content across multiple calls using append=true."
                    ),
                },
                "append": {
                    "type": "boolean",
                    "description": (
                        "If true, append to existing file instead of overwriting (default: false). "
                        "Use append=true for chunks 2+ when writing a long file in multiple calls."
                    ),
                    "default": False,
                },
            },
            "required": ["path", "content"],
        },
    },
})
def write_file(path: str, content: str, append: bool = False) -> str:
    if len(content) > MAX_WRITE_CHARS:
        return (
            f"ERROR: content too large ({len(content)} chars > {MAX_WRITE_CHARS} char limit). "
            f"The upstream LLM gateway truncates streaming responses above this size, which would "
            f"break the next tool call. Split the content into chunks of <= {MAX_WRITE_CHARS} chars "
            f"and call write_file multiple times: first chunk with append=false, subsequent chunks "
            f"with append=true. Split on natural boundaries (paragraph / function / section breaks)."
        )
    try:
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
        action = "appended to" if append else "written to"
        return f"Successfully {action} {path} ({len(content)} chars)"
    except Exception as e:
        return f"Error writing file: {e}"


# ── list_dir ─────────────────────────────────────────────────────────────────

@register({
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "List files and directories in a given path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (default: current directory).",
                    "default": ".",
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files (starting with .) (default: false).",
                    "default": False,
                },
            },
            "required": [],
        },
    },
})
def list_dir(path: str = ".", show_hidden: bool = False) -> str:
    try:
        p = Path(path).expanduser()
        if not p.exists():
            return f"Error: path not found: {path}"
        if not p.is_dir():
            return f"Error: not a directory: {path}"

        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        lines = []
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            if entry.is_dir():
                lines.append(f"[dir]  {entry.name}/")
            else:
                size = entry.stat().st_size
                size_str = _format_size(size)
                lines.append(f"[file] {entry.name}  ({size_str})")

        if not lines:
            return f"(empty directory: {path})"
        return f"{path}:\n" + "\n".join(lines)
    except Exception as e:
        return f"Error listing directory: {e}"


def _format_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"

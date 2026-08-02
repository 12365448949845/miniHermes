"""
@file 上下文引用：解析用户输入中的 @file:path 并注入文件内容。

语法：
  @file:path.py           — 注入全文
  @file:path.py:10        — 注入第 10 行
  @file:path.py:10-25     — 注入第 10-25 行
  @file:"path spaces.py"  — 引号包裹含空格路径
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

_QUOTED = r'(?:`[^`\n]+`|"[^"\n]+"|\'[^\'\n]+\')'
PATTERN = re.compile(
    rf'(?<![\w/])@file:(?P<value>{_QUOTED}(?::\d+(?:-\d+)?)?|\S+)'
)

_MAX_FILE_SIZE = 100 * 1024  # 100KB

_EXT_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "jsx", ".tsx": "tsx", ".java": "java", ".go": "go",
    ".rs": "rust", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".sh": "bash", ".zsh": "bash", ".bash": "bash",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".toml": "toml",
    ".xml": "xml", ".html": "html", ".css": "css", ".scss": "scss",
    ".sql": "sql", ".md": "markdown", ".txt": "", ".log": "",
}


@dataclass
class FileReference:
    raw: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    start: int = 0
    end: int = 0


@dataclass
class RefResult:
    message: str
    references: list[FileReference] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_value(value: str) -> tuple[str, int | None, int | None]:
    """解析引用值，返回 (path, line_start, line_end)。"""
    # 剥离外层引号
    if value and value[0] in ('`', '"', "'"):
        quote = value[0]
        end_quote = value.find(quote, 1)
        if end_quote > 0:
            path_part = value[1:end_quote]
            remainder = value[end_quote + 1:]
        else:
            path_part = value[1:]
            remainder = ""
    else:
        # 无引号：找最后的 :数字 部分
        parts = value.rsplit(":", 1)
        if len(parts) == 2 and re.match(r'^\d+(-\d+)?$', parts[1]):
            path_part = parts[0]
            remainder = ":" + parts[1]
        else:
            return value, None, None

    # 解析行范围
    line_start, line_end = None, None
    line_match = re.match(r':(\d+)(?:-(\d+))?', remainder)
    if line_match:
        line_start = int(line_match.group(1))
        line_end = int(line_match.group(2)) if line_match.group(2) else line_start

    return path_part, line_start, line_end


def _parse_references(text: str) -> list[FileReference]:
    """从文本中解析所有 @file: 引用。"""
    refs = []
    for m in PATTERN.finditer(text):
        value = m.group("value")
        path, line_start, line_end = _parse_value(value)
        refs.append(FileReference(
            raw=m.group(0),
            path=path,
            line_start=line_start,
            line_end=line_end,
            start=m.start(),
            end=m.end(),
        ))
    return refs


def _is_binary(path: Path) -> bool:
    """前 4KB 含 null byte 视为二进制。"""
    try:
        chunk = path.read_bytes()[:4096]
        return b"\x00" in chunk
    except OSError:
        return True


def _expand_file(ref: FileReference, cwd: Path) -> tuple[str | None, str | None]:
    """展开单个文件引用，返回 (block, warning)。"""
    resolved = (cwd / ref.path).resolve()

    if not resolved.exists():
        return None, f"@file:{ref.path}: file not found"
    if not resolved.is_file():
        return None, f"@file:{ref.path}: not a file"
    if _is_binary(resolved):
        return None, f"@file:{ref.path}: binary file, skipped"

    try:
        size = resolved.stat().st_size
        truncated = size > _MAX_FILE_SIZE
        text = resolved.read_text(encoding="utf-8", errors="replace")
        if truncated:
            text = text[:_MAX_FILE_SIZE]
    except OSError as e:
        return None, f"@file:{ref.path}: {e}"

    # 行范围截取
    if ref.line_start is not None:
        lines = text.splitlines()
        start_idx = max(ref.line_start - 1, 0)
        end_idx = min(ref.line_end or ref.line_start, len(lines))
        text = "\n".join(lines[start_idx:end_idx])

    # 语言推断
    lang = _EXT_LANG.get(resolved.suffix.lower(), "")

    # 格式化标题
    tokens = len(text) // 4
    title = f"@file:{ref.path}"
    if ref.line_start is not None:
        if ref.line_end and ref.line_end != ref.line_start:
            title += f":{ref.line_start}-{ref.line_end}"
        else:
            title += f":{ref.line_start}"

    block = f"📄 {title} ({tokens} tokens)\n```{lang}\n{text}\n```"
    if truncated:
        block += "\n[truncated: file exceeds 100KB]"

    warning = f"@file:{ref.path}: truncated (>{_MAX_FILE_SIZE // 1024}KB)" if truncated else None
    return block, warning


def _remove_tokens(message: str, refs: list[FileReference]) -> str:
    """从消息中移除 @file: token，规范化空白。"""
    result = message
    for ref in sorted(refs, key=lambda r: r.start, reverse=True):
        result = result[:ref.start] + result[ref.end:]
    result = re.sub(r" {2,}", " ", result)
    result = re.sub(r"\s+([,.;:!?])", r"\1", result)
    return result.strip()


def preprocess(message: str, cwd: Path = None) -> RefResult:
    """主入口：解析 @file 引用 → 展开 → 拼接到消息末尾。"""
    if cwd is None:
        cwd = Path.cwd()

    refs = _parse_references(message)
    if not refs:
        return RefResult(message=message)

    blocks = []
    warnings = []

    for ref in refs:
        block, warning = _expand_file(ref, cwd)
        if block:
            blocks.append(block)
        if warning:
            warnings.append(warning)

    stripped = _remove_tokens(message, refs)

    if blocks:
        final = f"{stripped}\n\n--- Attached Context ---\n\n" + "\n\n".join(blocks)
    else:
        final = stripped

    return RefResult(message=final, references=refs, warnings=warnings)

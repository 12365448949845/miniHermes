# read_file / write_file / list_dir — 文件操作三件套

代码: `tools/files.py` (232行)

---

## read_file

### Schema

```python
{
    "name": "read_file",
    "parameters": {
        "path": {"type": "string"},
        "offset": {"type": "integer", "default": 1},
        "limit": {"type": "integer", "default": 500}
    },
    "required": ["path"]
}
```

### 实现

```python
def read_file(path: str, offset: int = 1, limit: int = 500) -> str:
    p = Path(path).expanduser()
    total_lines = sum(1 for _ in open(p, 'rb'))  # 快速计数

    if offset > total_lines:
        return f"Error: offset {offset} is beyond end of file ({total_lines} lines)."

    lines = []
    with open(p, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            if i < offset: continue
            if len(lines) >= limit: break
            if len(line) > _MAX_LINE_LENGTH:
                line = line[:_MAX_LINE_LENGTH] + "... [truncated]"
            lines.append(f"{i:6d}| {line}")

    result = "".join(lines)
    if offset + len(lines) - 1 < total_lines:
        result += f"\n\n[Showing lines {offset}-{offset+len(lines)-1} of {total_lines}. Use offset=N to continue.]"

    return result[:_MAX_OUTPUT_CHARS]
```

### 三层保护

| 保护层 | 限制 | 原因 |
|--------|------|------|
| 文件大小 | 1 MB | 避免大文件撑爆内存 |
| 行数 | 2000 lines | 单次阅读足够多 |
| 字符数 | 80,000 chars | 上下文保护 |

### 行号格式

`{i:6d}| ` — 固定 6 位宽度，方便 LLM 引用 `file:line`。

### 尾部提示

`[Showing lines N-M of TOTAL. Use offset=N+1 to continue.]` — 引导 LLM 分段读取。

---

## write_file

### Schema

```python
{
    "name": "write_file",
    "parameters": {
        "path": {"type": "string"},
        "content": {"type": "string", "description": "Content to write (max 8000 chars)."},
        "append": {"type": "boolean", "default": False}
    },
    "required": ["path", "content"]
}
```

### 8000 字符硬限制

来自生产环境 LLM 网关对流式响应的截断限制。超过 8000 chars 必须分块写入：首次 `append=false`，后续 `append=true`。

### 自动创建父目录

```python
p.parent.mkdir(parents=True, exist_ok=True)
```

### 独立审批

write_file 有自己的敏感检查（`tools/approval.py`）：
- **敏感路径**: `.env`、`.ssh/`、`/etc/`、`credentials`、`secrets`、`.gitconfig`、shell 配置文件
- **敏感内容**: 嵌入 `API_KEY`/`SECRET`/`PASSWORD`/`TOKEN` 模式

---

## list_dir

### Schema

```python
{
    "name": "list_dir",
    "parameters": {
        "path": {"type": "string", "default": "."},
        "show_hidden": {"type": "boolean", "default": False}
    },
    "required": []
}
```

### 实现

```python
def list_dir(path: str = ".", show_hidden: bool = False) -> str:
    p = Path(path).expanduser()
    entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    # 目录优先 → 字母排序
    lines = []
    for entry in entries:
        if not show_hidden and entry.name.startswith('.'):
            continue
        type_char = "/" if entry.is_dir() else ""
        size_str = _format_size(entry.stat().st_size) if entry.is_file() else "-"
        lines.append(f"{type_char}{entry.name} ({size_str})")
    return "\n".join(lines)
```

- 隐藏文件默认不显示
- 目录优先排序
- 人类可读的文件大小（B/KB/MB/GB）

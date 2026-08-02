"""
记忆工具：跨 session 的持久化知识管理。

双轨道设计（参考 hermes tools/memory_tool.py）：
  - 冻结快照（_snapshot）：启动时读盘一次，注入系统提示词，session 内永不变化
    → 前缀缓存在整个 session 内保持稳定
  - Live 状态（memory_entries / user_entries）：工具调用可修改，每次写入后持久化到磁盘
    → 下次 session 启动时才会体现到系统提示词

文件结构：
  ~/.minihermes/memory/MEMORY.md  — agent 的个人笔记（环境事实、项目约定等）
  ~/.minihermes/memory/USER.md    — 用户画像（偏好、背景、姓名等）

条目格式：每个条目是一行或多行文本，条目之间用 ENTRY_DELIMITER 分隔。
"""

from pathlib import Path
from typing import Optional

from tools import register
from config import MINIHERMES_HOME

MEMORY_DIR = MINIHERMES_HOME / "memory"
ENTRY_DELIMITER = "\n---\n"


class MemoryStore:
    """
    持久化记忆存储，维护 memory 和 user 两条独立轨道。

    - load_from_disk(): 启动时调用，冻结快照
    - format_for_system_prompt(): 返回快照块，注入系统提示词
    - add / update / delete / view: 工具调用的 live 操作，每次自动写盘

    字符上限（字符数，不含分隔符）：
      memory: 2200 chars  /  user: 1375 chars
    超限时 add/update 报错并返回当前用量，要求先删除或替换旧条目。
    """

    MEMORY_CHAR_LIMIT = 2200
    USER_CHAR_LIMIT = 1375

    def __init__(self):
        self.memory_entries: list[str] = []
        self.user_entries: list[str] = []
        # 冻结快照：load_from_disk() 时捕获，此后不再修改
        self._snapshot: dict[str, str] = {"memory": "", "user": ""}

    def load_from_disk(self):
        """从磁盘加载，捕获冻结快照用于系统提示词注入。"""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self.memory_entries = self._read_file(MEMORY_DIR / "MEMORY.md")
        self.user_entries = self._read_file(MEMORY_DIR / "USER.md")
        self._snapshot = {
            "memory": self._render_block("memory", self.memory_entries),
            "user":   self._render_block("user",   self.user_entries),
        }

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """
        返回冻结快照块，供 build_system_prompt() 注入。
        返回 None 表示该 target 无内容（空文件 / 从未写入）。
        """
        block = self._snapshot.get(target, "")
        return block if block else None

    # ── Live 操作（工具调用路径）──────────────────────────────────────────────

    def add(self, target: str, entry: str) -> str:
        entries = self._entries_for(target)
        entry = entry.strip()
        if not entry:
            return "Error: entry cannot be empty."
        if entry in entries:
            return f"Entry already exists in {target} (no duplicate added)."
        new_entries = entries + [entry]
        new_total = len(ENTRY_DELIMITER.join(new_entries))
        limit = self._char_limit(target)
        if new_total > limit:
            current = self._char_count(target)
            return (
                f"Error: {target} at {current:,}/{limit:,} chars. "
                f"Adding this entry ({len(entry):,} chars) would exceed the limit. "
                f"Replace or remove existing entries first."
            )
        entries.append(entry)
        self._save(target)
        return f"Added to {target}. {self._usage_str(target)}"

    def update(self, target: str, index: int, entry: str) -> str:
        entries = self._entries_for(target)
        if not (0 <= index < len(entries)):
            return f"Error: index {index} out of range (0–{len(entries) - 1})."
        entry = entry.strip()
        if not entry:
            return "Error: entry cannot be empty. Use 'delete' to remove entries."
        new_entries = entries[:index] + [entry] + entries[index + 1:]
        new_total = len(ENTRY_DELIMITER.join(new_entries))
        limit = self._char_limit(target)
        if new_total > limit:
            current = self._char_count(target)
            return (
                f"Error: updating entry [{index}] would put {target} at "
                f"{new_total:,}/{limit:,} chars (currently {current:,}). "
                f"Shorten the new content or remove other entries first."
            )
        entries[index] = entry
        self._save(target)
        return f"Updated entry [{index}] in {target}. {self._usage_str(target)}"

    def delete(self, target: str, index: int) -> str:
        entries = self._entries_for(target)
        if not (0 <= index < len(entries)):
            return f"Error: index {index} out of range (0–{len(entries) - 1})."
        removed = entries.pop(index)
        self._save(target)
        preview = removed[:60] + ("..." if len(removed) > 60 else "")
        return f"Deleted entry [{index}] from {target}: \"{preview}\". {self._usage_str(target)}"

    def view(self, target: str) -> str:
        entries = self._entries_for(target)
        usage = self._usage_str(target)
        if not entries:
            return f"No entries in {target}. {usage}"
        lines = [f"[{i}] {e}" for i, e in enumerate(entries)]
        return f"{target.upper()} ({len(entries)} entries, {usage}):\n" + "\n".join(lines)

    # ── 内부工具 ──────────────────────────────────────────────────────────────

    def _entries_for(self, target: str) -> list[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _char_limit(self, target: str) -> int:
        return self.USER_CHAR_LIMIT if target == "user" else self.MEMORY_CHAR_LIMIT

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        return len(ENTRY_DELIMITER.join(entries)) if entries else 0

    def _usage_str(self, target: str) -> str:
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int(current / limit * 100)) if limit > 0 else 0
        return f"{pct}% — {current:,}/{limit:,} chars"

    def _save(self, target: str):
        """将 live 状态写入磁盘（不影响本 session 的冻结快照）。"""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        path = MEMORY_DIR / ("USER.md" if target == "user" else "MEMORY.md")
        entries = self._entries_for(target)
        path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")

    def _render_block(self, target: str, entries: list[str]) -> str:
        """渲染系统提示词注入块（包含标题、用量百分比和分隔线）。"""
        if not entries:
            return ""
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        limit = self._char_limit(target)
        pct = min(100, int(current / limit * 100)) if limit > 0 else 0
        header = (
            f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
            if target == "user"
            else f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
        )
        sep = "═" * 46
        return f"{sep}\n{header}\n{sep}\n{content}"

    @staticmethod
    def _read_file(path: Path) -> list[str]:
        if not path.exists():
            return []
        try:
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                return []
            return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        except OSError:
            return []


# ── 模块级单例（agent 和工具函数共享同一个 store 实例）───────────────────────

_store: Optional[MemoryStore] = None


def get_store() -> MemoryStore:
    """懒加载：第一次调用时从磁盘初始化，之后返回同一实例。"""
    global _store
    if _store is None:
        _store = MemoryStore()
        _store.load_from_disk()
    return _store


# ── 工具注册 ──────────────────────────────────────────────────────────────────

@register({
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Read and write persistent memory that survives across sessions. "
            "Use 'memory' target for your own notes (facts about the environment, "
            "project conventions, tool quirks). Use 'user' target for user profile "
            "(preferences, background, name). "
            "Char limits: memory=2200, user=1375. Add returns an error if the limit "
            "would be exceeded — replace or delete old entries first. "
            "Note: changes take effect in the NEXT session's system prompt. "
            "This session's context is fixed at startup."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "delete", "view"],
                    "description": "add: append new entry | update: replace entry by index | delete: remove entry by index | view: list all entries",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "memory: agent notes | user: user profile",
                },
                "entry": {
                    "type": "string",
                    "description": "Content for add/update actions.",
                },
                "index": {
                    "type": "integer",
                    "description": "0-based entry index for update/delete actions.",
                },
            },
            "required": ["action", "target"],
        },
    },
})
def memory_fn(
    action: str,
    target: str = "memory",
    entry: Optional[str] = None,
    index: Optional[int] = None,
) -> str:
    store = get_store()
    if action == "view":
        return store.view(target)
    if action == "add":
        if not entry:
            return "Error: 'entry' is required for action 'add'."
        return store.add(target, entry)
    if action == "update":
        if index is None:
            return "Error: 'index' is required for action 'update'."
        if not entry:
            return "Error: 'entry' is required for action 'update'."
        return store.update(target, index, entry)
    if action == "delete":
        if index is None:
            return "Error: 'index' is required for action 'delete'."
        return store.delete(target, index)
    return f"Error: unknown action '{action}'. Use: add | update | delete | view."

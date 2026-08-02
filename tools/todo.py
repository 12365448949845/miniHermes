"""
任务列表工具：agent 自我管理多步骤任务进度。
纯内存存储，session 结束即清空。
"""

import json
from tools import register

_items: list[dict] = []

_VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


@register({
    "type": "function",
    "function": {
        "name": "todo",
        "description": (
            "Manage a task list for tracking multi-step work. "
            "Use PROACTIVELY when the task requires 3+ steps or the user provides multiple requirements. "
            "Create the todo list BEFORE starting work, not after. "
            "Call with no parameters to read current list. "
            "Call with 'todos' to write/update tasks. "
            "Keep exactly ONE item in_progress at a time. "
            "Mark items completed immediately when done — do not batch completions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "Task items to write. Omit to read current list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Unique task identifier.",
                            },
                            "content": {
                                "type": "string",
                                "description": "Task description.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed", "cancelled"],
                                "description": "Current task status.",
                            },
                        },
                        "required": ["id", "content", "status"],
                    },
                },
                "merge": {
                    "type": "boolean",
                    "description": "true: update existing by id, add new. false (default): replace entire list.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
})
def todo(todos: list = None, merge: bool = False) -> str:
    global _items

    if todos is None:
        return _format_output()

    validated = _validate(todos)

    if merge:
        existing_ids = {item["id"]: i for i, item in enumerate(_items)}
        for item in validated:
            if item["id"] in existing_ids:
                _items[existing_ids[item["id"]]] = item
            else:
                _items.append(item)
    else:
        _items = validated

    return _format_output()


def _validate(todos: list) -> list[dict]:
    result = []
    seen_ids = set()
    for item in todos:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", ""))
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        status = item.get("status", "pending")
        if status not in _VALID_STATUSES:
            status = "pending"
        result.append({
            "id": item_id,
            "content": str(item.get("content", "")),
            "status": status,
        })
    return result


def _format_output() -> str:
    summary = {
        "total": len(_items),
        "pending": sum(1 for i in _items if i["status"] == "pending"),
        "in_progress": sum(1 for i in _items if i["status"] == "in_progress"),
        "completed": sum(1 for i in _items if i["status"] == "completed"),
        "cancelled": sum(1 for i in _items if i["status"] == "cancelled"),
    }
    return json.dumps({"todos": _items, "summary": summary}, ensure_ascii=False, indent=2)


def reset():
    """清空任务列表（session 切换时调用）。"""
    global _items
    _items = []

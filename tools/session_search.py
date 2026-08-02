"""
会话搜索工具：FTS5 全文检索历史会话。
无 query 时列出最近会话，有 query 时搜索消息内容。
"""

import json
import time
from tools import register
from session import SessionDB


@register({
    "type": "function",
    "function": {
        "name": "session_search",
        "description": (
            "Search past conversation sessions or browse recent history. "
            "Two modes: (1) No query → list recent sessions with titles and timestamps. "
            "(2) With query → FTS5 keyword search across all past messages. "
            "Query syntax: simple keywords, OR, NOT, prefix* (e.g. 'python OR javascript', 'deploy*')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search keywords (FTS5 syntax). Omit to list recent sessions.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max sessions to return (default: 5, max: 10).",
                    "default": 5,
                },
            },
            "required": [],
        },
    },
})
def session_search(query: str = None, limit: int = 5) -> str:
    limit = min(max(limit, 1), 10)
    db = SessionDB()

    try:
        if not query:
            return _list_recent(db, limit)
        return _search(db, query, limit)
    finally:
        db.close()


def _list_recent(db: SessionDB, limit: int) -> str:
    sessions = db.list_sessions(limit=limit)
    if not sessions:
        return "No sessions found."

    lines = [f"Recent sessions ({len(sessions)}):\n"]
    for s in sessions:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["started_at"]))
        title = s["title"] or "(untitled)"
        lines.append(f"  {s['id']}  {t}  msgs:{s['message_count']}  {title}")

    return "\n".join(lines)


def _search(db: SessionDB, query: str, limit: int) -> str:
    results = db.search_messages(query, limit=limit)
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for r in results:
        t = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["started_at"]))
        title = r["title"] or "(untitled)"
        lines.append(f"Session: {r['session_id']}  {t}  {title}")
        for m in r["matches"]:
            snippet = m["snippet"].replace("\n", " ")[:200]
            lines.append(f"  [{m['role']}] {snippet}")
        lines.append("")

    return "\n".join(lines).strip()

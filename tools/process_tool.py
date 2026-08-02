"""
进程查询工具（只读）。
列出系统进程信息，不支持 kill 等写操作。
"""

import time
from tools import register

try:
    import psutil
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


@register({
    "type": "function",
    "function": {
        "name": "process",
        "description": (
            "List running system processes (read-only). "
            "Returns PID, name, CPU%, memory usage, and status. "
            "Use name_filter to find specific processes. "
            "This tool only supports querying — no kill or signal operations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list"],
                    "description": "Action to perform. Currently only 'list' is supported.",
                },
                "name_filter": {
                    "type": "string",
                    "description": "Optional: filter processes by name (case-insensitive substring match).",
                },
            },
            "required": ["action"],
        },
    },
})
def process(action: str, name_filter: str = None) -> str:
    if not _HAS_PSUTIL:
        return "Error: psutil is not installed. Run: pip install psutil"

    if action != "list":
        return f"Error: action '{action}' is not supported. Only 'list' is available."

    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info', 'status', 'create_time']):
        try:
            info = p.info
            name = info.get('name') or ''
            if name_filter and name_filter.lower() not in name.lower():
                continue
            mem = info.get('memory_info')
            mem_mb = mem.rss / (1024 * 1024) if mem else 0
            create_time = info.get('create_time') or 0
            uptime = time.time() - create_time if create_time > 0 else 0
            procs.append({
                'pid': info.get('pid', 0),
                'name': name,
                'cpu': info.get('cpu_percent', 0) or 0,
                'mem_mb': mem_mb,
                'status': info.get('status', 'unknown'),
                'uptime': uptime,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    procs.sort(key=lambda x: x['cpu'], reverse=True)
    procs = procs[:50]

    if not procs:
        msg = "No processes found"
        if name_filter:
            msg += f" matching '{name_filter}'"
        return msg

    lines = [f"{'PID':>7}  {'Name':<25} {'CPU%':>5} {'MEM(MB)':>8} {'Status':<10} {'Uptime'}"]
    lines.append("-" * 75)

    for p in procs:
        uptime_str = _format_uptime(p['uptime'])
        lines.append(
            f"{p['pid']:>7}  {p['name'][:25]:<25} {p['cpu']:>5.1f} {p['mem_mb']:>8.1f} {p['status']:<10} {uptime_str}"
        )

    header = f"Processes: {len(procs)} shown"
    if name_filter:
        header += f" (filter: '{name_filter}')"
    return f"{header}\n\n" + "\n".join(lines)


def _format_uptime(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60)}m"
    return f"{int(seconds // 86400)}d{int((seconds % 86400) // 3600)}h"

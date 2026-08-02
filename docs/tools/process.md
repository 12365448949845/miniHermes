# process — 进程查询

代码: `tools/process_tool.py` (103行)

---

## Schema 设计

```python
{
    "name": "process",
    "parameters": {
        "action": {"enum": ["list"], "description": "Currently only 'list' is supported."},
        "name_filter": {"type": "string", "description": "Optional process name filter (case-insensitive)."}
    },
    "required": []
}
```

action 目前只有 `list`（enum 保留给未来扩展）。

## 实现

```python
def process(action: str = "list", name_filter: str = None) -> str:
    if not _HAS_PSUTIL:
        return "Error: psutil is not installed. Run: pip install psutil"

    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_info']):
        try:
            info = p.info
            if name_filter and name_filter.lower() not in info['name'].lower():
                continue
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue  # 静默跳过

    procs.sort(key=lambda x: x['cpu_percent'] or 0, reverse=True)
    procs = procs[:50]  # top 50 by CPU

    # 格式化为表格输出
    ...
```

## 关键设计点

### psutil 可选依赖

未安装时返回友好的安装提示。psutil 是纯 Python 包，`pip install psutil` 即可。

### 静默跳过异常

`NoSuchProcess`、`AccessDenied`、`ZombieProcess` 三种异常静默处理——进程在迭代期间可能已退出。

### Top 50 by CPU

按 CPU 降序排列后取前 50 条，防止在进程数多的系统上输出过长。

### 人类可读 uptime

将 uptime 秒数转换为 `Xd Xh Xm Xs` 格式。

### action enum 预留

当前仅 `list`，不提供 `kill` 等修改性操作。enum 参数设计为未来扩展保留空间。

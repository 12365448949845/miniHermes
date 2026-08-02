# bash — 本地 Shell 执行

代码: `tools/bash.py` (72行)

---

## Schema 设计

```python
{
    "name": "bash",
    "parameters": {
        "command": {"type": "string", "description": "The shell command to execute."},
        "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)."}
    },
    "required": ["command"]
}
```

两个参数：`command`（必填）和 `timeout`（可选，默认 30s）。

## 实现

```python
def bash(command: str, timeout: int = 30) -> str:
    result = subprocess.run(
        command,
        shell=True,         # ★ 支持 pipe/redirect
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        output += f"\n[exit code: {result.returncode}]"

    return truncate_output(output, _MAX_OUTPUT_CHARS)
```

## 关键设计点

### shell=True 的决策

原因：LLM 生成的命令经常包含 pipe (`|`)、redirect (`>`)、变量展开等 shell 特性。安全风险委托给审批层处理。

### capture_output 而非 streaming

`capture_output=True`，内存缓存全部输出。subprocess 的 streaming 会引入复杂的中断处理。当前方案简单可靠。

### 错误透传而非异常

非零 exit code 追加到输出末尾（`[exit code: N]`），不抛异常。让 LLM 看到错误信息并自行修正。

### 输出截断

50,000 chars 上限，head 40% + tail 60%。

### 重试机制

timeout 错误被 retry.py 识别，自动用 ×2 超时重试（max_retries=2）。

### 无 cwd / env 参数

保持工具最小化。cwd 已在系统提示词的环境块中声明，env 不暴露以保护敏感变量。

### 超时默认 30s

太短（如 `npm install`）会超时 → 自动重试用 ×2 超时。太长（如无限循环）浪费资源。30s 是经验值。

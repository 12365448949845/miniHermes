# execute_code — 云沙箱代码执行

代码: `tools/code_execution.py` (496行)

---

## 概述

支持 6 种语言的云沙箱执行：python, javascript, typescript, java, r, bash。每次工具调用创建全新沙箱实例，执行代码后立即销毁。这种"一次性容器"模式避免了状态残留和资源泄漏。

## Schema 设计

```python
{
    "name": "execute_code",
    "parameters": {
        "code": {"type": "string", "description": "Code to execute in the sandbox."},
        "language": {"type": "string", "description": "python/javascript/typescript/java/r/bash"},
        "timeout": {"type": "integer", "description": "Max execution seconds."}
    },
    "required": ["code"]
}
```

## 实现架构

```
execute_code(code, language, timeout)
  ├── _load_code_config()          # 读取配置
  ├── _missing_env(config)         # 检查必需的配置项
  ├── _create_sandbox(Sandbox, timeout)  # 创建沙箱（try/except 兼容多种 SDK 版本）
  ├── _run_code_in_sandbox(...)    # 执行代码 + 收集输出
  ├── _extract_response_text(...)  # 解析 SDK 返回值
  ├── _redact_sensitive_text(...)  # ★ 敏感信息脱敏
  └── finally: _kill_sandbox(...)  # ★ 确保沙箱销毁
```

## 关键设计点

### 一次性容器模式

```
创建沙箱 → 执行代码 → 收集输出 → 销毁沙箱
```

每次工具调用独立沙箱，不保留文件/环境/状态。即使代码抛异常，`finally` 块确保沙箱被销毁。

### 双重超时

- **code_timeout** (120s): 代码执行时间上限
- **sandbox_timeout** (300s): 沙箱生命周期上限

双层保护：代码超时是 SDK 层面的限制，沙箱超时是平台层面强制销毁。

### SDK 兼容性

```python
def _create_sandbox(Sandbox, timeout):
    try:
        return Sandbox.create(timeout=timeout)     # 新版本 SDK
    except AttributeError:
        return Sandbox(timeout=timeout)             # 旧版本 SDK
```

### 四路输出捕获

SDK 回调收集四种输出：stdout、stderr、result（Jupyter 风格表达式值）、error（运行时异常）。

### 敏感信息脱敏

```python
def _redact_sensitive_text(text):
    sensitive_values = [
        os.getenv("SANDBOX_API_KEY", ""),
        cfg.get_model_config().get("api_key", ""),
        cfg.get_search_config().get("api_key", ""),
    ]
    for value in sensitive_values:
        if value and len(value) >= 6:
            text = text.replace(value, "[REDACTED]")
    return text
```

防止代码执行结果意外包含 API key（如 `print(os.environ)`）。

### 环境变量隔离

```python
@contextmanager
def _temporary_env(key, value):
    old = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old is None:  os.environ.pop(key, None)
        else:            os.environ[key] = old
```

SDK 通过环境变量读取配置，但直接 set 会污染整个进程。`_temporary_env` 确保在工具调用结束后恢复原环境。

### JSON 返回格式

```json
{
    "stdout": "...",
    "stderr": "...",
    "result": "...",
    "error": null,
    "language": "python",
    "duration_seconds": 1.2
}
```

## 踩坑记录

- **沙箱不销毁导致配额耗尽**: 早期版本没有 `finally: sandbox.kill()`，异常时沙箱未被销毁，多次调用累积数十个僵尸沙箱。改为 try/finally 后解决。
- **SDK 版本不兼容**: 不同 SDK 版本创建沙箱的 API 不同（`Sandbox.create()` vs `Sandbox()`）。`_create_sandbox()` 的 try/except AttributeError 兼容了两者。
- **环境变量泄漏**: 早期直接 `os.environ["KEY"] = config["key"]` 永久修改，影响整个进程。改为 `_temporary_env` context manager 后解决。

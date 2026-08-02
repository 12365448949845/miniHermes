# Provider 层

> OpenAI 兼容 API 封装、流式调用、自动重试、推理模式 | `provider/provider.py`

---

## 1. Provider 初始化

```python
class Provider:
    def __init__(self, model_cfg: dict):
        self.client = OpenAI(
            base_url=model_cfg.get("base_url"),
            api_key=model_cfg.get("api_key"),
            max_retries=0,           # 显式关闭 SDK 重试
        )
        self.model = model_cfg.get("name")
        self.show_thinking = model_cfg.get("show_thinking", True)
        self.reasoning_mode = model_cfg.get("reasoning_mode")
```

**max_retries=0 的原因：** SDK 内置重试会与 stream() 的重试逻辑叠加，造成 N×M 请求倍增。

---

## 2. StreamResult 数据类

```python
@dataclass
class StreamResult:
    content: str = ""            # 累积的文本内容
    reasoning: str = ""          # 推理内容（独立轨道）
    tool_calls: list[dict] = []  # 按 index 累积的 tool calls
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    interrupted: bool = False

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0
```

---

## 3. stream() — 带重试的流式入口

```python
def stream(self, messages, tools, on_delta, on_thinking,
           on_tool_start, interrupt_check, renderer) -> StreamResult:

    for attempt in range(RETRY_API_MAX_RETRIES + 1):
        try:
            result, is_retryable = self._stream_once(...)
            if not is_retryable:
                return result

            # 重试前等待
            delay = jittered_backoff(attempt)
            if _interruptible_sleep(delay, interrupt_check):
                result.interrupted = True
                return result

        except Exception as exc:
            category = classify_api_error(exc)
            if category == "permanent":
                raise

            delay = jittered_backoff(attempt)
            if _interruptible_sleep(delay, interrupt_check):
                return StreamResult(interrupted=True)
```

## 4. _sanitize_messages() — 消息清洗

在发送前清洗消息列表：

```python
def _sanitize_messages(self, messages: list[dict]) -> list[dict]:
    cleaned = []
    for msg in messages:
        # 1. 剥离空 content 的 assistant 占位
        if msg["role"] == "assistant" and not msg.get("content"):
            continue
        # 2. 合并连续两个 user 消息
        if msg["role"] == "user" and cleaned and cleaned[-1]["role"] == "user":
            cleaned[-1]["content"] += "\n\n" + msg["content"]
            continue
        # 3. 只保留 API 认可的字段
        cleaned.append({k: v for k, v in msg.items()
                        if k in ("role", "content", "tool_calls", "tool_call_id")})
    return cleaned
```

## 5. _stream_once() — 单次流式调用

核心逻辑：

```python
def _stream_once(self, messages, tools, callbacks):
    # 推理模式处理
    extra_body = {}
    if self.reasoning_mode:
        extra_body["reasoning"] = {"mode": "enabled"}

    # SDK 流式调用
    response = self.client.chat.completions.create(
        model=self.model,
        messages=messages,
        tools=tools,
        stream=True,
        max_tokens=100000,
        extra_body=extra_body,
        stream_options={"include_usage": True},
    )

    # 逐 chunk 累积
    result = StreamResult()
    for chunk in response:
        if interrupt_check():
            result.interrupted = True
            response.close()
            break

        delta = chunk.choices[0].delta
        # content 累积 → on_delta 回调
        # reasoning_content 累积 → on_thinking 回调
        # tool_calls 按 index 累积 → on_tool_start 回调

        if chunk.usage:
            result.prompt_tokens = chunk.usage.prompt_tokens
            result.completion_tokens = chunk.usage.completion_tokens

    # Malformed tool_calls 诊断
    if result.has_tool_calls:
        for tc in result.tool_calls:
            if not tc.get("function", {}).get("arguments"):
                _dump_malformed_tool_call(...)  # 落盘诊断

    return result
```

## 6. build_assistant_message()

```python
def build_assistant_message(self, result: StreamResult) -> dict:
    msg = {"role": "assistant", "content": result.content}
    if result.tool_calls:
        msg["tool_calls"] = [{
            "id": tc["id"],
            "type": "function",
            "function": {
                "name": tc["function"]["name"],
                "arguments": tc["function"]["arguments"],
            }
        } for tc in result.tool_calls]
    return msg
```

## 7. 推理模式

通过 `extra_body` 传递 reasoning 参数：

```python
# 配置: reasoning_mode: "enabled"
extra_body = {"reasoning": {"mode": "enabled"}}
```

流式响应中的 `reasoning_content` 字段被独立轨道追踪，通过 `on_thinking` 回调展示。

## 8. 错误分类与重试

```python
def classify_api_error(exc) -> str:
    if isinstance(exc, RateLimitError):     return "retryable"
    if isinstance(exc, (APITimeoutError,
                        APIConnectionError)): return "retryable"
    if status_code >= 500:                  return "retryable"
    if status_code in (401, 403):           return "permanent"
    if isinstance(exc, BadRequestError):
        if "context_length" in msg:         return "context_overflow"
        return "permanent"
    # httpx network errors
    return "retryable"
```

**Jittered backoff：**
```python
def jittered_backoff(attempt, base=2.0, max_delay=60.0, jitter=0.5):
    delay = min(base * (2 ** attempt), max_delay)
    jitter_amount = delay * jitter * (2 * random.random() - 1)
    return delay + jitter_amount
```

优先使用 API 返回的 `Retry-After` header。

## 9. Malformed Tool Call 诊断

当 token 限制中断工具调用 JSON 时，落盘诊断文件到 `~/.minihermes/logs/`：

```python
def _dump_malformed_tool_call(payload):
    dump = {
        "timestamp": datetime.now().isoformat(),
        "tool_name": payload.get("function", {}).get("name"),
        "arguments_preview": payload.get("function", {}).get("arguments", "")[:500],
        "finish_reason": payload.get("finish_reason"),
        "byte_length": len(payload.get("function", {}).get("arguments", "")),
    }
    # 落盘到 ~/.minihermes/logs/malformed_tool_call_NNNN.json
```

Agent 层同时有 JSONDecodeError 容错，构造错误回填让 LLM 重发。

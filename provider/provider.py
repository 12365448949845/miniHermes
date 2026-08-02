"""
Provider 层：封装 OpenAI SDK，支持所有 OpenAI 兼容接口。
（DeepSeek / Qwen / GPT 系列 / 本地 Ollama 等只需修改 config.yaml）

流式处理设计（参考 hermes run_agent.py 的双轨道机制）：
  - 展示轨道：每个 delta 立即回调 on_delta / on_thinking（实时展示）
  - 存储轨道：SDK 内部或手动累积完整消息，流结束后一次性构建历史消息
"""

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import openai
from openai import OpenAI

import config as cfg
from renderer.renderer import _cprint, _DIM, _RST, _AMBER


MODEL_NAME = "deepseek-v4-pro"
RETRY_API_MAX_RETRIES = 2

# ── 调试日志目录 ─────────────────────────────────────────────────────────────
# malformed tool_calls 等罕见错误的现场会落盘到这里，方便事后分析
_DEBUG_DIR = Path.home() / ".minihermes" / "logs"


def _dump_malformed_tool_call(payload: dict) -> Optional[Path]:
    """把残缺的 tool call 现场落盘成 JSON，返回写入路径。失败时返回 None。"""
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        # 不用 Date.now / random，用计数器后缀避免覆盖
        stem = "malformed_tool_call"
        i = 0
        while True:
            target = _DEBUG_DIR / f"{stem}_{i:04d}.json"
            if not target.exists():
                break
            i += 1
            if i > 9999:
                break
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return target
    except OSError:
        return None


# ── API 错误分类与重试工具 ────────────────────────────────────────────────────

_CONTEXT_OVERFLOW_KEYWORDS = (
    "context_length_exceeded",
    "maximum context",
    "context window",
    "too many tokens",
    "context length",
    "prompt is too long",
)


def classify_api_error(exc: Exception) -> str:
    """将 API 调用异常分类为 retryable / permanent / context_overflow。"""
    if isinstance(exc, openai.RateLimitError):
        return "retryable"
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
        return "retryable"
    if isinstance(exc, openai.InternalServerError):
        return "retryable"
    if isinstance(exc, openai.AuthenticationError):
        return "permanent"
    if isinstance(exc, openai.PermissionDeniedError):
        return "permanent"
    if isinstance(exc, openai.NotFoundError):
        return "permanent"
    if isinstance(exc, openai.BadRequestError):
        msg = str(exc).lower()
        if any(kw in msg for kw in _CONTEXT_OVERFLOW_KEYWORDS):
            return "context_overflow"
        return "permanent"
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status in (429, 500, 502, 503, 504, 529):
            return "retryable"
        if 400 <= status < 500:
            return "permanent"
        return "retryable"

    # httpx / 网络层瞬态错误（流式中断常见）
    name = type(exc).__name__
    if name in ("RemoteProtocolError", "ReadTimeout", "ConnectTimeout",
                "ConnectError", "PoolTimeout", "ReadError", "WriteError"):
        return "retryable"

    # 未知异常：保守按 retryable，避免偶发故障直接失败
    return "retryable"


def jittered_backoff(
    attempt: int,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    jitter_ratio: float = 0.5,
) -> float:
    """计算 jittered exponential backoff 延迟。

    公式：min(base * 2^(attempt-1), max_delay) + uniform(0, jitter_ratio * delay)
    """
    exponent = max(0, attempt - 1)
    if exponent >= 30 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)
    jitter = random.uniform(0, jitter_ratio * delay)
    return delay + jitter


def _extract_retry_after(exc: Exception) -> Optional[float]:
    """从 RateLimitError 等异常中提取 Retry-After header（秒）。"""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _interruptible_sleep(seconds: float, interrupt_check: Optional[Callable[[], bool]]) -> bool:
    """以 0.2s 为粒度睡眠，期间检测中断；返回 True 表示被中断。"""
    end = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < end:
        if interrupt_check and interrupt_check():
            return True
        remaining = end - time.monotonic()
        time.sleep(min(0.2, remaining))
    return False


@dataclass
class StreamResult:
    """流式调用完成后的标准化结果。"""
    content: str = ""           # 文本回复
    reasoning: str = ""         # 思考过程（thinking / reasoning_content）
    tool_calls: list = field(default_factory=list)  # 工具调用列表
    finish_reason: str = ""     # "stop" | "tool_calls" | "length" 等
    prompt_tokens: int = 0      # 本轮发送的总 token 数（从 API usage 获取）
    completion_tokens: int = 0  # 本轮生成的 token 数
    interrupted: bool = False   # 是否被用户中断（Ctrl+C）

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class Provider:
    def __init__(self):
        model_cfg = cfg.get_model_config()
        # 显式关闭 SDK 内置重试（默认 2 次）；由 stream() 统一管理重试，
        # 避免 SDK 重试 × 我们重试 出现 N×M 的请求倍增
        self.client = OpenAI(
            base_url=model_cfg.get("base_url"),
            api_key=model_cfg.get("api_key"),
            max_retries=0,
        )
        self.model = model_cfg.get("name") or MODEL_NAME
        self.show_thinking = model_cfg.get("show_thinking", False)
        self.reason = model_cfg.get("reason", True)

    def _sanitize_messages(self, messages: list[dict]) -> list[dict]:
        """浅拷贝 messages，只保留 API 认可的字段。

        - 跳过空 assistant 消息（早期中断占位，无 content 且无 tool_calls）
        - 跳过连续重复的 user 消息（保留最后一条）
        """
        # 第一遍：剥离空 assistant 占位（中断写入的 finish_reason="interrupted_before_response"）
        filtered = []
        for msg in messages:
            if msg.get("role") == "assistant":
                if not msg.get("content") and not msg.get("tool_calls"):
                    continue
            filtered.append(msg)

        sanitized = []
        skip_count = 0

        for i, msg in enumerate(filtered):
            role = msg.get("role")

            # 检测连续 user 消息：只保留最后一条
            if role == "user":
                next_idx = i + 1
                if next_idx < len(filtered) and filtered[next_idx].get("role") == "user":
                    skip_count += 1
                    continue
                if skip_count > 0:
                    _cprint(f"{_DIM}⚠ skipped {skip_count} duplicate user message(s){_RST}")
                    skip_count = 0

            clean = {"role": role}
            if msg.get("content") is not None:
                clean["content"] = msg["content"]
            if role == "assistant" and msg.get("tool_calls"):
                clean["tool_calls"] = msg["tool_calls"]
            if role == "tool" and msg.get("tool_call_id"):
                clean["tool_call_id"] = msg["tool_call_id"]
            sanitized.append(clean)

        return sanitized

    def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str], None]] = None,
        interrupt_check: Optional[Callable[[], bool]] = None,
        renderer=None,
    ) -> StreamResult:
        """发起一次流式 API 调用，对瞬态错误（429/5xx/网络中断）自动重试。

        重试策略：jittered exponential backoff，配置见 retry.api 段。
        永久错误（4xx auth / model not found 等）不重试，直接抛。
        """
        # API 层重试始终启用
        max_attempts = RETRY_API_MAX_RETRIES + 1

        for attempt in range(1, max_attempts + 1):
            try:
                return self._stream_once(
                    messages, tools,
                    on_delta=on_delta,
                    on_thinking=on_thinking,
                    on_tool_start=on_tool_start,
                    interrupt_check=interrupt_check,
                )
            except Exception as exc:
                # 用户主动中断 → 立即抛，不重试
                if interrupt_check and interrupt_check():
                    raise

                err_class = classify_api_error(exc)

                if err_class in ("permanent", "context_overflow"):
                    raise
                if attempt >= max_attempts:
                    raise

                # 计算延迟：优先使用 Retry-After header
                retry_after = _extract_retry_after(exc)
                if retry_after is not None:
                    delay = min(retry_after, 60.0)  # 60.0 = 标准退避延迟上限
                else:
                    delay = jittered_backoff(attempt)

                _cprint(
                    f"\n{_AMBER}⟳ API error ({type(exc).__name__}), "
                    f"retrying in {delay:.1f}s (attempt {attempt + 1}/{max_attempts})...{_RST}"
                )

                # 重置 renderer 以便重试输出干净
                if renderer is not None:
                    try:
                        renderer.reset()
                    except Exception:
                        pass

                if _interruptible_sleep(delay, interrupt_check):
                    raise

        # unreachable
        raise RuntimeError("stream retry loop exited unexpectedly")

    def _stream_once(
        self,
        messages: list[dict],
        tools: list[dict],
        on_delta: Optional[Callable[[str], None]] = None,
        on_thinking: Optional[Callable[[str], None]] = None,
        on_tool_start: Optional[Callable[[str], None]] = None,
        interrupt_check: Optional[Callable[[], bool]] = None,
    ) -> StreamResult:
        """单次流式 API 调用（不含重试逻辑）。"""
        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.3,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.reason:
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled"},
                "reasoning_mode": "enabled",
                "reasoning_effort": "max",
            }
        else:
            kwargs["extra_body"] = {
                "thinking": {"type": "disabled"},
                "reasoning_mode": "disabled",
            }

        kwargs["messages"] = self._sanitize_messages(kwargs["messages"])
        response = self.client.chat.completions.create(**kwargs)

        # 累积变量
        full_content = ""
        full_reasoning = ""
        # tool_calls 按 index 累积，因为流式返回的是增量 JSON 片段
        # 格式：{index: {"id": ..., "name": ..., "arguments": ...}}
        raw_tool_calls: dict[int, dict] = {}
        # tool_calls 增量计数：每个 idx 收到了多少个 args chunk、累计追加了多少字符
        tool_chunk_stats: dict[int, dict] = {}
        finish_reason = ""
        announced_tools: set[int] = set()  # 已通知 on_tool_start 的 tool index
        prompt_tokens = 0
        completion_tokens = 0
        chunk_count = 0  # 收到的 SSE chunk 总数（用于诊断流是否被提前关闭）

        was_interrupted = False
        for chunk in response:
            chunk_count += 1
            if interrupt_check and interrupt_check():
                response.close()
                was_interrupted = True
                break

            # 最后一个 chunk 携带 usage（stream_options 开启时）
            if hasattr(chunk, "usage") and chunk.usage:
                prompt_tokens = chunk.usage.prompt_tokens or 0
                completion_tokens = chunk.usage.completion_tokens or 0

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            finish_reason = choice.finish_reason or finish_reason
            delta = choice.delta

            # ── 思考过程（DeepSeek-R1 / o系列）────────────────────────────
            # delta.reasoning_content 是 DeepSeek 扩展字段，标准 OpenAI 没有
            reasoning_chunk = getattr(delta, "reasoning_content", None)
            if reasoning_chunk:
                full_reasoning += reasoning_chunk
                if self.show_thinking and on_thinking:
                    on_thinking(reasoning_chunk)

            # ── 文本内容 ────────────────────────────────────────────────────
            # 有 tool_calls 时模型通常不输出 content，加判断防止空字符串噪声
            if delta.content:
                full_content += delta.content
                if on_delta:
                    on_delta(delta.content)

            # ── 工具调用（增量累积）─────────────────────────────────────────
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index

                    # 第一次见到这个 index：初始化条目
                    if idx not in raw_tool_calls:
                        raw_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}

                    if tc.id:
                        raw_tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            raw_tool_calls[idx]["name"] = tc.function.name
                            # 工具名首次出现时通知 UI
                            if idx not in announced_tools and on_tool_start:
                                on_tool_start(tc.function.name)
                                announced_tools.add(idx)
                        if tc.function.arguments:
                            raw_tool_calls[idx]["arguments"] += tc.function.arguments
                            stats = tool_chunk_stats.setdefault(idx, {"chunks": 0, "chars": 0, "last_chunk_chars": 0})
                            stats["chunks"] += 1
                            stats["chars"] += len(tc.function.arguments)
                            stats["last_chunk_chars"] = len(tc.function.arguments)

        # ── 诊断：检测残缺的 tool_calls ──────────────────────────────────────
        # 当 finish_reason="length"（撞 max_tokens 上限）、网关丢包、或上游断流时，
        # 最后一个工具的 arguments 经常是半个 JSON。这里打印精细诊断信息并落盘
        # 完整 raw，方便事后区分根因（max_tokens 截断 vs 网关 buffer 截断 vs
        # 模型自吐非法字符）。
        if raw_tool_calls and not was_interrupted:
            for idx in sorted(raw_tool_calls.keys()):
                raw_args = raw_tool_calls[idx].get("arguments", "")
                if not raw_args:
                    continue
                try:
                    json.loads(raw_args)
                except json.JSONDecodeError as e:
                    name = raw_tool_calls[idx].get("name", "?")

                    # 字节长度（UTF-8）：判断是否撞到网关 buffer 的整数倍（32K/64K/128K）
                    byte_len = len(raw_args.encode("utf-8", errors="replace"))

                    # 字面控制字符统计：JSON 里出现真 0x00-0x1F（除 \t\r\n）都是非法
                    # 模型如果直接吐了字面换行，会让 json 解析以为字符串没结束
                    bad_ctrl = sum(
                        1 for c in raw_args
                        if ord(c) < 0x20 and c not in ("\t", "\r", "\n")
                    )
                    literal_newlines = raw_args.count("\n")
                    escaped_newlines = raw_args.count("\\n")

                    # 头尾 repr：repr 能把字面换行显示成 \n，区分真假换行
                    head_repr = repr(raw_args[:120])
                    tail_repr = repr(raw_args[-200:])

                    # 累积统计：chunk 数 + 最后一个 chunk 的大小（看是否中途被切）
                    stats = tool_chunk_stats.get(idx, {})

                    preview = raw_args if len(raw_args) <= 300 else raw_args[:150] + " ... " + raw_args[-150:]

                    _cprint(
                        f"\n{_AMBER}⚠ malformed tool args"
                        f" (tool={name}, idx={idx}, finish_reason={finish_reason},"
                        f" chars={len(raw_args)}, bytes={byte_len},"
                        f" completion_tokens={completion_tokens},"
                        f" sse_chunks_total={chunk_count},"
                        f" arg_chunks={stats.get('chunks', 0)},"
                        f" last_arg_chunk_chars={stats.get('last_chunk_chars', 0)},"
                        f" literal_newlines={literal_newlines},"
                        f" escaped_newlines={escaped_newlines},"
                        f" bad_ctrl_chars={bad_ctrl}): {e}{_RST}\n"
                        f"{_DIM}raw: {preview}{_RST}\n"
                        f"{_DIM}tail_repr: {tail_repr}{_RST}"
                    )

                    # 完整 raw 落盘（CLI 截断了大部分内容，事后用编辑器看更清楚）
                    dump_path = _dump_malformed_tool_call({
                        "tool_name": name,
                        "tool_index": idx,
                        "finish_reason": finish_reason,
                        "model": self.model,
                        "max_tokens_sent": kwargs.get("max_tokens"),
                        "completion_tokens": completion_tokens,
                        "prompt_tokens": prompt_tokens,
                        "sse_chunks_total": chunk_count,
                        "arg_chunks": stats.get("chunks", 0),
                        "last_arg_chunk_chars": stats.get("last_chunk_chars", 0),
                        "char_len": len(raw_args),
                        "byte_len": byte_len,
                        "literal_newlines": literal_newlines,
                        "escaped_newlines": escaped_newlines,
                        "bad_ctrl_chars": bad_ctrl,
                        "json_error": str(e),
                        "head_repr": head_repr,
                        "tail_repr": tail_repr,
                        "raw_arguments": raw_args,
                    })
                    if dump_path is not None:
                        _cprint(f"{_DIM}full dump: {dump_path}{_RST}")

        if finish_reason == "length":
            _cprint(
                f"\n{_AMBER}⚠ response truncated by model output limit"
                f" (completion_tokens={completion_tokens}).{_RST}\n"
            )

        # 组装标准化工具调用列表
        tool_calls = [
            {
                "id": raw_tool_calls[i]["id"],
                "type": "function",
                "function": {
                    "name": raw_tool_calls[i]["name"],
                    "arguments": raw_tool_calls[i]["arguments"],
                },
            }
            for i in sorted(raw_tool_calls.keys())
        ]

        return StreamResult(
            content=full_content,
            reasoning=full_reasoning,
            tool_calls=tool_calls if not was_interrupted else [],
            finish_reason="interrupted" if was_interrupted else finish_reason,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            interrupted=was_interrupted,
        )

    def build_assistant_message(self, result: StreamResult) -> dict:
        """将 StreamResult 转换为可追加到 messages 的 assistant dict。"""
        msg: dict = {"role": "assistant", "content": result.content or ""}
        if result.tool_calls:
            msg["tool_calls"] = result.tool_calls
        if result.reasoning:
            # 存储思考过程，不回传给 API（避免污染上下文）
            msg["_reasoning"] = result.reasoning
        return msg

    def build_tool_result_message(self, tool_call_id: str, result: str) -> dict:
        """构建工具结果消息，回传给 LLM。"""
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": result,
        }

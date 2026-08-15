"""
Web 搜索工具，使用 Exa AI 搜索 API。
Exa 是专为 AI Agent 设计的搜索引擎，具有：
  - 多种搜索模式：auto / instant(~250ms) / fast / deep-lite / deep / deep-reasoning
  - token 高效的 highlights 提取（只返回 LLM 需要的片段）
  - 分类搜索：company / people / research paper / news / financial report
  - 结构化 JSON 输出（outputSchema）

API 文档：https://docs.exa.ai/reference/search
控制台获取 API Key：https://dashboard.exa.ai
"""

import json

from tools import register
from tools.retry import trusted_tool_failure
import config as cfg

_search_cfg = cfg.get_search_config()
_API_KEY = _search_cfg.get("api_key", "")
_DEFAULT_COUNT = _search_cfg.get("count", 5)

# 延迟初始化 Exa client，避免没装 SDK 时启动就报错
_exa_client = None


def _get_exa_client():
    """获取或创建 Exa client（单例）。"""
    global _exa_client
    if _exa_client is None:
        if not _API_KEY:
            raise ValueError(
                "EXA_API_KEY not configured. "
                "Set search.api_key in config.yaml, or EXA_API_KEY environment variable. "
                "Get your free API key at https://dashboard.exa.ai"
            )
        from exa_py import Exa

        _exa_client = Exa(api_key=_API_KEY)
    return _exa_client


@register({
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for up-to-date information using Exa. "
            "Use this when you need current events, documentation, "
            "or facts you are unsure about. "
            "Returns titles, URLs, and highlights (LLM-optimized key excerpts) "
            "for each result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query.",
                },
                "count": {
                    "type": "integer",
                    "description": (
                        f"Number of results to return "
                        f"(default: {_DEFAULT_COUNT}, max: 20)."
                    ),
                    "default": _DEFAULT_COUNT,
                },
            },
            "required": ["query"],
        },
    },
})
def web_search(query: str, count: int = _DEFAULT_COUNT) -> str:
    """使用 Exa 搜索网页，返回格式化的搜索结果。

    Args:
        query: 搜索关键词
        count: 返回结果数量（1-20）

    Returns:
        格式化的搜索结果字符串
    """
    count = max(1, min(count, 20))

    try:
        exa = _get_exa_client()
        response = exa.search(
            query,
            num_results=count,
            type="auto",
            contents={
                "highlights": True,
            },
        )
    except ValueError:
        # API Key 未配置
        return trusted_tool_failure(
            "Error: Exa API key not configured. "
            "Set search.api_key in config.yaml. "
            "Get your free API key (1,000 requests/month) at https://dashboard.exa.ai",
            "missing_configuration",
        )
    except Exception as e:
        error_msg = str(e)
        response = getattr(e, "response", None)
        status_code = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        lowered = error_msg.lower()
        # 识别常见错误
        if status_code == 401 or "401" in error_msg or "unauthorized" in lowered:
            return trusted_tool_failure(
                "Error: Exa API key is invalid. "
                "Check search.api_key in config.yaml. "
                "Get a valid key at https://dashboard.exa.ai",
                "authentication_failed",
            )
        if status_code == 403 or "403" in error_msg or "forbidden" in lowered:
            return trusted_tool_failure(
                "Error: Exa API permission denied. Check the API key permissions.",
                "permission_denied",
            )
        if status_code == 402 or "402" in error_msg or "quota" in lowered:
            return trusted_tool_failure(
                "Error: Exa API quota exceeded. "
                "Upgrade your plan at https://exa.ai/pricing "
                "or wait for your monthly quota to reset.",
                "quota_exceeded",
            )
        if status_code == 429 or "429" in error_msg or "rate limit" in lowered:
            return trusted_tool_failure(
                "Error: Exa API rate limit hit. "
                "Please wait a moment and try again.",
                "rate_limited",
                retryable=True,
                retry_after=retry_after,
            )
        transient = (
            isinstance(status_code, int) and status_code >= 500
        ) or any(
            marker in lowered
            for marker in (
                "timeout", "timed out", "connection", "connection reset",
                "temporarily unavailable",
            )
        )
        return trusted_tool_failure(
            f"Error: Exa search failed: {error_msg}",
            "network_transient" if transient else "permanent_failure",
            retryable=transient,
            retry_after=retry_after if transient else None,
        )

    return _parse_results(response, query)


def _parse_results(response, query: str) -> str:
    """解析 Exa 搜索结果，格式化为纯文本。"""
    lines = [f"Search results for: {query}\n"]

    results = response.results or []
    if not results:
        return f"Search results for: {query}\n\nNo results found."

    for i, r in enumerate(results, 1):
        title = r.title or "(no title)"
        url = r.url or ""

        # highlights 是 Exa 的 LLM 提取的关键片段，最省 token
        highlights = r.highlights or []
        if highlights:
            snippet = " | ".join(highlights)
        else:
            # fallback：用 text 或 summary
            snippet = (
                getattr(r, "summary", "")
                or (getattr(r, "text", "") or "")[:500]
            )

        # 如果有发布日期，附加上
        published = getattr(r, "publishedDate", "")
        author = getattr(r, "author", "")

        lines.append(f"{i}. **{title}**")
        if url:
            lines.append(f"   URL: {url}")
        if published:
            lines.append(f"   Published: {published}")
        if author:
            lines.append(f"   Author: {author}")
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")

    result_text = "\n".join(lines).strip()

    # 返回前截断，防止超出上下文
    max_len = 8_000
    if len(result_text) > max_len:
        result_text = result_text[:max_len] + "\n\n[output truncated]"

    return result_text

# web_search — Web 搜索

代码: `tools/search.py` (172行)

---

## 概述

通过第三方 AI 搜索引擎实现。该引擎专为 AI Agent 设计，提供 LLM 优化的 highlights 提取和多种搜索模式。免费套餐 1000 次/月。

## Schema 设计

```python
{
    "name": "web_search",
    "parameters": {
        "query": {"type": "string", "description": "Search query."},
        "count": {"type": "integer", "default": 5, "description": "Number of results (1-20)."}
    },
    "required": ["query"]
}
```

## 实现

```python
_API_KEY = _search_cfg.get("api_key", "")
_DEFAULT_COUNT = _search_cfg.get("count", 5)
_exa_client = None  # 延迟初始化

def _get_exa_client():
    global _exa_client
    if _exa_client is None:
        if not _API_KEY:
            raise ValueError("API key not configured.")
        from exa_py import Exa
        _exa_client = Exa(api_key=_API_KEY)
    return _exa_client

def web_search(query: str, count: int = None) -> str:
    if count is None:
        count = _DEFAULT_COUNT
    count = min(max(count, 1), 20)

    try:
        client = _get_exa_client()
        results = client.search_and_contents(
            query,
            type="auto",
            num_results=count,
            text=True,           # 全文
            highlights=True,     # LLM 提取的摘要
        )
        return _parse_results(results, query)
    except Exception as e:
        return _classify_error(e)
```

## 关键设计点

### 延迟初始化

```python
# Exa client 在第一次调用时才创建
# from exa_py import Exa 在函数内部 import
```

原因：
- 避免启动时 import 失败导致整个模块不可用
- 免 API key 配置前不浪费网络连接

### Highlights 优先

该搜索引擎的 highlights 是 LLM 提取的关键片段，比全文本更省 token。解析结果时 highlights 在前，全文本在后。

### 错误分类

```python
def _classify_error(e):
    if "401" in str(e) or "unauthorized" in str(e).lower():
        return "Error: Invalid API key. Get a valid key at <dashboard URL>."
    if "402" in str(e) or "quota" in str(e).lower():
        return "Error: API quota exceeded. Upgrade your plan."
    if "429" in str(e):
        return "Error: Rate limited. Retry later."
    return f"Search error: {e}"
```

### 输出截断

8,000 chars 上限。搜索结果可能包含大量全文本，需要严格控制。

### 搜索模式

`type="auto"` — 搜索引擎自动选择最合适的搜索模式（keyword / neural / auto）。

### 设计决策：此搜索引擎而非传统搜索引擎

- **AI-native**: 结果包含 LLM 提取的 highlights，更省 token
- **结构化输出**: 直接返回 text + highlights，不需要二次抓取
- **免爬虫**: 不需要自己处理 HTML 解析
- **免费套餐**: 1000 次/月足够个人使用

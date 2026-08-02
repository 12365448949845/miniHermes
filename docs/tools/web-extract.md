# web_extract — 网页内容提取

代码: `tools/web_extract.py` (126行)

---

## Schema 设计

```python
{
    "name": "web_extract",
    "parameters": {
        "url": {"type": "string", "description": "The URL to fetch and extract content from."}
    },
    "required": ["url"]
}
```

## 实现

```python
_MAX_CONTENT = 50_000

def web_extract(url: str) -> str:
    # 协议自动补全
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # 请求
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; MiniHermes/1.0)"},
        timeout=15,
    )

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return f"Error: unsupported content type: {content_type}"

    html = resp.text

    # 提取标题
    title = _extract_title(html)

    # HTML → text（双路径）
    try:
        text = _bs4_extract(html)  # 优先: BeautifulSoup
    except Exception:
        text = _simple_extract(html)  # 回退: regex

    return f"# {title}\n\n{text[:_MAX_CONTENT]}"
```

## 关键设计点

### 双路径提取

- **优先路径 (BeautifulSoup)**: 移除 script/style/nav/header/footer/aside/noscript/iframe 等语义标签，提取 main/article/body 内容
- **回退路径 (regex)**: 零依赖，移除 HTML 标签和多余空白

### 延迟导入

```python
# BeautifulSoup 导入在函数内部，不在模块顶层
from bs4 import BeautifulSoup
```

原因：bs4 是可选依赖，`pip install beautifulsoup4` 才可用。放在函数内部保证导入失败不影响模块加载。

### Custom User-Agent

使用 `MiniHermes/1.0` 标识，礼貌且可识别。

### Content-Type 白名单

仅处理 `text/html` 和 `text/plain`，避免下载 PDF/图片/视频等二进制内容。

### 50K chars 截断

防止超大网页撑爆上下文。

### 15s 超时

网页提取可能因网络问题阻塞，需要合理超时。

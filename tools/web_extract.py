"""
网页内容抓取工具：获取 URL 页面内容并转为文本/markdown。
"""

import re
import requests
from tools import register

_MAX_CONTENT = 50000


@register({
    "type": "function",
    "function": {
        "name": "web_extract",
        "description": (
            "Extract text content from a web page URL. "
            "Returns the page title and main content in plain text. "
            "Useful for reading documentation, articles, or any web page. "
            "Content over 50000 chars is truncated."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to extract content from.",
                },
            },
            "required": ["url"],
        },
    },
})
def web_extract(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MiniHermes/1.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.Timeout:
        return f"Error: request timed out for {url}"
    except requests.HTTPError as e:
        return f"Error: HTTP {e.response.status_code} for {url}"
    except requests.RequestException as e:
        return f"Error: {e}"

    content_type = resp.headers.get("content-type", "")
    if "text/html" not in content_type and "text/plain" not in content_type:
        return f"Error: unsupported content-type '{content_type}' for {url}"

    html = resp.text

    title = _extract_title(html)
    text = _html_to_text(html)

    if not text.strip():
        return f"Error: no content extracted from {url}"

    truncated = len(text) > _MAX_CONTENT
    if truncated:
        text = text[:_MAX_CONTENT]

    result = f"Title: {title}\nURL: {url}\n\n{text}"
    if truncated:
        result += "\n\n[content truncated — exceeded 50000 chars]"

    return result


def _extract_title(html: str) -> str:
    match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
    if match:
        title = match.group(1).strip()
        title = re.sub(r'<[^>]+>', '', title)
        title = _decode_entities(title)
        return title[:200]
    return "(no title)"


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        return _bs4_extract(html)
    except ImportError:
        return _simple_extract(html)


def _bs4_extract(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
        tag.decompose()

    # 优先提取 main/article 区域
    main = soup.find("main") or soup.find("article") or soup.find("body")
    if main is None:
        main = soup

    text = main.get_text(separator="\n", strip=True)
    # 压缩连续空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


def _simple_extract(html: str) -> str:
    """无 bs4 时的纯正则回退方案。"""
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '\n', text)
    text = _decode_entities(text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)
    return text.strip()


def _decode_entities(text: str) -> str:
    import html
    return html.unescape(text)

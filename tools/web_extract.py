"""
网页内容抓取工具：获取 URL 页面内容并转为文本/markdown。
"""

import re
import requests
from tools import register
from tools.retry import trusted_tool_cancelled, trusted_tool_failure

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
def web_extract(url: str, _timeout: float | None = None,
                _cancel_check=None) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    if _cancel_check and _cancel_check():
        return trusted_tool_cancelled("Error: request cancelled before start")
    timeout = max(0.1, min(15.0, float(_timeout))) if _timeout else 15.0
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MiniHermes/1.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.Timeout:
        return trusted_tool_failure(
            f"Error: request timed out for {url}",
            "timeout",
            retryable=True,
        )
    except requests.HTTPError as e:
        status_code = e.response.status_code
        retry_after = e.response.headers.get("Retry-After")
        if status_code == 429:
            error_code = "rate_limited"
            retryable = True
        elif status_code >= 500:
            error_code = "network_transient"
            retryable = True
        elif status_code == 401:
            error_code = "authentication_failed"
            retryable = False
        elif status_code == 403:
            error_code = "permission_denied"
            retryable = False
        else:
            error_code = "permanent_failure"
            retryable = False
        return trusted_tool_failure(
            f"Error: HTTP {status_code} for {url}",
            error_code,
            retryable=retryable,
            retry_after=retry_after if retryable else None,
        )
    except requests.RequestException as e:
        retryable = isinstance(e, requests.ConnectionError)
        return trusted_tool_failure(
            f"Error: {e}",
            "network_transient" if retryable else "permanent_failure",
            retryable=retryable,
        )

    if _cancel_check and _cancel_check():
        return trusted_tool_cancelled("Error: request cancelled before completion")

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

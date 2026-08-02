"""
在系统默认浏览器中打开 URL。
"""

import webbrowser
from tools import register


@register({
    "type": "function",
    "function": {
        "name": "web_open",
        "description": (
            "Open a URL in the system's default web browser. "
            "Use this when the user wants to view a web page, "
            "preview a link, or open a web application. "
            "Returns success/failure status."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to open in the browser. Must start with http:// or https://.",
                },
            },
            "required": ["url"],
        },
    },
})
def web_open(url: str) -> str:
    """在系统默认浏览器中打开 URL。"""
    if not url.startswith(("http://", "https://")):
        return f"Error: URL must start with http:// or https://, got: {url[:80]}"

    try:
        success = webbrowser.open(url)
    except webbrowser.Error as e:
        return f"Error: failed to open browser: {e}"

    if success:
        return f"Opened {url} in default browser."
    else:
        return f"Error: could not open browser for {url} (no browser found)."

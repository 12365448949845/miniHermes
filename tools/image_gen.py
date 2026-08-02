"""
文生图工具：调用 Pollinations.ai 免费的 GET 端点。
- 无需 API key，URL 写死
- 服务端固定返回 JPEG，落地保存为 .jpg
- 不做客户端限流，被限流时把服务端错误透传给模型
"""

import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import requests

from tools import register


def _open_in_default_viewer(path: Path) -> str | None:
    """用系统默认看图器打开文件。失败返回错误描述，成功返回 None。"""
    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", str(path)])
        elif system == "Windows":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["xdg-open", str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except (OSError, FileNotFoundError) as e:
        return str(e)
    return None

_BASE_URL = "https://image.pollinations.ai/prompt/"
_OUTPUT_SUBDIR = "image_tmp"
_REQUEST_TIMEOUT = 120


@register({
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an image from a text prompt using Pollinations.ai (free, no key). "
            "Saves a JPEG to ./image_tmp/ under the current working directory, "
            "auto-opens it in the system default viewer, and returns its absolute path. "
            "If the server rate-limits or errors, the error is returned verbatim — "
            "the model should wait a few seconds and retry."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the image to generate.",
                },
                "width": {
                    "type": "integer",
                    "description": "Image width in pixels (default 1024).",
                    "default": 1024,
                },
                "height": {
                    "type": "integer",
                    "description": "Image height in pixels (default 1024).",
                    "default": 1024,
                },
            },
            "required": ["prompt"],
        },
    },
})
def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> str:
    if not prompt or not prompt.strip():
        return "Error: prompt is empty."

    width = max(64, min(int(width), 2048))
    height = max(64, min(int(height), 2048))

    url = (
        f"{_BASE_URL}{quote(prompt, safe='')}"
        f"?width={width}&height={height}"
    )

    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
    except requests.Timeout:
        return f"Error: image generation timed out after {_REQUEST_TIMEOUT}s."
    except requests.RequestException as e:
        return f"Error: image generation request failed: {e}"

    if resp.status_code != 200:
        return (
            f"Error: Pollinations returned HTTP {resp.status_code}. "
            f"body={resp.text[:500]}"
        )

    content_type = resp.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return (
            f"Error: unexpected content-type '{content_type}'. "
            f"body={resp.text[:500]}"
        )

    try:
        out_dir = Path.cwd() / _OUTPUT_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{int(time.time() * 1000)}.jpeg"
        out_path = out_dir / filename
        out_path.write_bytes(resp.content)
    except OSError as e:
        return f"Error: failed to save image: {e}"

    open_err = _open_in_default_viewer(out_path)
    opened_note = "Opened in default viewer." if open_err is None else f"Could not auto-open ({open_err})."

    return (
        f"Image generated successfully.\n"
        f"Path: {out_path}\n"
        f"Size: {width}x{height}, {len(resp.content)} bytes (JPEG)\n"
        f"{opened_note}"
    )

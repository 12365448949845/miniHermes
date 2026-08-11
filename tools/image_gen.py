"""Image generation through Pollinations.ai."""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import requests

import config as cfg
from tools import register


_POLLINATIONS_BASE_URL = "https://image.pollinations.ai/prompt/"
_OUTPUT_SUBDIR = "image_tmp"
_REQUEST_TIMEOUT = 120


def _open_in_default_viewer(path: Path) -> str | None:
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
    except (OSError, FileNotFoundError) as error:
        return str(error)
    return None


def _load_image_config() -> dict:
    raw = cfg.get_image_generation_config()
    try:
        timeout = float(raw.get("timeout_seconds", _REQUEST_TIMEOUT))
    except (TypeError, ValueError):
        timeout = _REQUEST_TIMEOUT
    return {
        "base_url": str(raw.get("base_url", "")).strip(),
        "timeout_seconds": max(1.0, min(timeout, 600.0)),
        "auto_open": bool(raw.get("auto_open", True)),
    }


def _network_error(error: requests.RequestException) -> str:
    if isinstance(error, requests.exceptions.SSLError):
        return (
            "Error: image generation TLS connection failed. The Pollinations service "
            "certificate could not be verified or the TLS handshake was interrupted. "
            "Check whether this service is reachable from the current network; do not "
            "disable TLS verification."
        )
    if isinstance(error, requests.exceptions.ConnectionError):
        return (
            "Error: image generation service is unreachable. Could not connect to the "
            f"Pollinations endpoint: {error}"
        )
    return f"Error: image generation request failed: {error}"


def _image_extension(content_type: str) -> str:
    normalized = content_type.lower().split(";", 1)[0].strip()
    return {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
    }.get(normalized, ".img")


@register({
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "Generate an image from a text prompt using Pollinations.ai. Save the image "
            "under ./image_tmp/ and return its absolute path. On an error, report the "
            "diagnostic instead of immediately repeating the same request."
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
def generate_image(
    prompt: str,
    width: int = 1024,
    height: int = 1024,
    _timeout: float | None = None,
    _cancel_check=None,
) -> str:
    if not prompt or not prompt.strip():
        return "Error: prompt is empty."

    width = max(64, min(int(width), 2048))
    height = max(64, min(int(height), 2048))
    if _cancel_check and _cancel_check():
        return "Error: image generation cancelled before start"

    image_config = _load_image_config()
    timeout = image_config["timeout_seconds"]
    if _timeout is not None:
        timeout = max(0.1, min(timeout, float(_timeout)))
    endpoint = image_config["base_url"] or _POLLINATIONS_BASE_URL
    url = (
        f"{endpoint.rstrip('/')}/{quote(prompt, safe='')}"
        f"?width={width}&height={height}"
    )

    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as error:
        return _network_error(error)

    if _cancel_check and _cancel_check():
        return "Error: image generation cancelled before saving"
    if response.status_code != 200:
        return (
            f"Error: Pollinations returned HTTP {response.status_code}. "
            f"body={response.text[:500]}"
        )

    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/"):
        return (
            f"Error: Pollinations returned unexpected content-type '{content_type}'. "
            f"body={response.text[:500]}"
        )

    try:
        out_dir = Path.cwd() / _OUTPUT_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{int(time.time() * 1000)}{_image_extension(content_type)}"
        out_path.write_bytes(response.content)
    except OSError as error:
        return f"Error: failed to save image: {error}"

    if image_config["auto_open"]:
        open_error = _open_in_default_viewer(out_path)
        opened_note = (
            "Opened in default viewer."
            if open_error is None
            else f"Could not auto-open ({open_error})."
        )
    else:
        opened_note = "Auto-open disabled by configuration."

    return (
        "Image generated successfully.\n"
        f"Path: {out_path}\n"
        f"Size: {width}x{height}, {len(response.content)} bytes ({content_type})\n"
        f"{opened_note}"
    )

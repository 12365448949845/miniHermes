# recognize_image — 多模态图片识别

代码: `tools/vision.py` (150行)

---

## 概述

让 Agent 能"看到"图片内容。通过独立的多模态 LLM（OpenAI 兼容接口）实现——与主对话使用的 LLM 可以不同。

## Schema 设计

```python
{
    "name": "recognize_image",
    "parameters": {
        "image": {"type": "string", "description": "URL or local file path to the image."},
        "prompt": {"type": "string", "description": "What to look for in the image (default: describe fully)."}
    },
    "required": ["image"]
}
```

## 实现

```python
def recognize_image(image: str, prompt: str = None) -> str:
    if not _API_KEY:
        return "Error: vision.api_key not configured"

    # 判断来源：URL vs 本地文件
    if image.startswith(("http://", "https://")):
        image_content = {"type": "image_url", "image_url": {"url": image}}
    else:
        # 本地文件 → base64
        filepath = Path(image).expanduser()
        if not filepath.exists():
            return f"Error: file not found: {image}"
        mime_type = _guess_mime(filepath)
        b64 = base64.b64encode(filepath.read_bytes()).decode()
        image_content = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{b64}"}
        }

    # 调用 vision API
    resp = requests.post(
        f"{_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {_API_KEY}"},
        json={
            "model": _VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": prompt}, image_content]
            }],
            "max_tokens": 4096,
        },
        timeout=_TIMEOUT,
    )

    content = resp.json()["choices"][0]["message"]["content"]
    return content[:_MAX_OUTPUT]
```

## 关键设计点

### 独立 API 配置

使用独立的 `vision` 配置段（`base_url` / `api_key` / `model`），与主 `model` 段分离。原因：
- 主模型可能不支持多模态（纯文本模型）
- 视觉模型可以更小/更便宜
- 独立计费和控制

### 两种输入源

- **URL**: 直接透传给 API（API 自己下载）
- **本地文件**: base64 编码 + MIME 推断

### MIME 类型推断

```python
_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"
}
```

### 60s 超时

图片识别可能较慢，独立于其他工具的 timeout。

### 输出截断

20K chars 上限，因为图片描述通常较长。

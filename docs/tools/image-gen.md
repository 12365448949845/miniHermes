# generate_image — AI 文生图

代码: `tools/image_gen.py` (126行)

---

## 概述

通过免费 AI 图像生成 API 将文本描述转换为图片。无需 API key。

## Schema 设计

```python
{
    "name": "generate_image",
    "parameters": {
        "prompt": {"type": "string", "description": "Image description."},
        "width": {"type": "integer", "default": 1024},
        "height": {"type": "integer", "default": 1024}
    },
    "required": ["prompt"]
}
```

## 实现

```python
_BASE_URL = "https://image.pollinations.ai/prompt/"
_REQUEST_TIMEOUT = 120

def generate_image(prompt: str, width: int = 1024, height: int = 1024) -> str:
    width = max(64, min(width, 2048))     # clamp 64-2048
    height = max(64, min(height, 2048))

    url = f"{_BASE_URL}{urllib.parse.quote(prompt)}?width={width}&height={height}"
    resp = requests.get(url, timeout=_REQUEST_TIMEOUT)

    if "image" not in resp.headers.get("content-type", ""):
        return f"Error: API returned non-image response."

    # 保存到 image_tmp/<timestamp_ms>.jpeg
    out_dir = Path(_OUTPUT_SUBDIR)
    out_dir.mkdir(exist_ok=True)
    filename = f"{int(time.time() * 1000)}.jpeg"
    filepath = out_dir / filename
    filepath.write_bytes(resp.content)

    # 自动打开
    viewer_msg = _open_in_default_viewer(filepath)

    return f"Image generated: {filepath}\n{viewer_msg}"
```

## 关键设计点

### 免费 API

使用公开免费 API，无认证开销。

### 尺寸限制

64-2048 像素，两端 clamp。

### 中文 prompt 支持

通过 `urllib.parse.quote` URL 编码，支持中文描述。

### 自动打开

```python
def _open_in_default_viewer(path):
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.run(["xdg-open", str(path)])
```

### 错误检测

通过 `content-type` 检查响应是否为图片，而非 JSON 状态码。

### 文件命名

毫秒时间戳 + `.jpeg`：`1700000000123.jpeg`。简单去重，无需随机数。

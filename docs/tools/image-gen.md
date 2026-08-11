# generate_image - AI 文生图

代码位置：`tools/image_gen.py`

`generate_image` 通过 Pollinations.ai 将文字提示词转为图片，将返回的图片保存到当前项目的 `image_tmp/` 目录，并按配置决定是否自动打开。

主 Agent 使用的是 DeepSeek 聊天模型；图片生成是一项独立的外部服务。此工具不会使用、不会读取、也不会修改 DeepSeek 的 `model` 配置。

## 配置

默认配置如下：

```yaml
image_generation:
  # 留空时使用 https://image.pollinations.ai/prompt/
  base_url: ""
  timeout_seconds: 120
  auto_open: true
```

可以在 `~/.minihermes/config.yaml` 中修改此配置。`base_url` 只用于替换 Pollinations 服务的访问地址，不包含 API Key、聊天模型或中转站配置。

## 常见错误

- `TLS connection failed`：网络在 TLS 握手阶段中断，或证书无法验证。检查该图片服务在当前网络下是否可达；不要通过关闭证书验证来绕过问题。
- `service is unreachable`：当前网络无法连接到 Pollinations 的域名或端口。
- `HTTP 429/5xx`：服务端限流或临时故障，稍后再由用户发起一次新请求。

这些错误会直接返回给 Agent，并要求它不要在同一轮对完全相同的请求盲目重试。

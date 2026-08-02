# MiniHermes

轻量级 AI 编程助手 CLI，仿照 [Claude Code](https://claude.ai/code) 和 [Hermes](https://github.com/NousResearch/hermes-agent) 设计。基于 Python 构建，支持任意 OpenAI 兼容 API（DeepSeek、Claude、GPT 等），提供交互式终端界面，具备工具调用、记忆持久化和可扩展技能系统。
目前针对deepseek-v4-pro做了测试，其他部分模型并未特殊兼容，可能有一些问题。

`每个模块的详细文档都放在了目录/docs下，可以查看`

## 快速开始

```bash
# 全局安装
pip install minihermes

# 或源码运行
git clone https://github.com/xxx/minihermes.git
cd minihermes
uv sync
python main.py
```

首次启动自动进入配置向导，设置 API Key、Base URL、模型名称。

```bash
minihermes   # 启动交互式对话
```

## 基本操作

| 操作 | 方式 |
| --- | --- |
| 发送消息 | `Enter` |
| 多行输入 | `Ctrl+J` |
| 中断回复 | `Ctrl+C` |
| 退出 | `/exit` 或 `Ctrl+D` |
| 查看帮助 | `/help` |
| 规划模式 | `/plan <描述>` |
| 加载技能 | `/<skill-name>`，如 `/code-review` |

## 打包

```bash
bash build_wheel.sh                    # 构建 wheel 包到 dist/
pip install dist/minihermes-*.whl      # 全局安装
```

构建脚本将源码模块复制到 `minihermes_cli/app/`，通过 hatchling 打包为标准 wheel，安装后 `minihermes` 命令全局可用。

## 主要特性

流式对话 · 15 个内置工具 · 技能系统（条件激活 + 安全扫描） · 跨会话记忆 · 安全审批（两层防线） · 上下文自动压缩 · SQLite 持久化 + FTS5 全文搜索 · 子 Agent 委派 · 规划模式（只读分析 → 审批 → 执行）

## 详细文档

完整架构设计和模块文档见 [`docs/`](docs/)：

| 文档 | 内容 |
| --- | --- |
| [整体架构](docs/整体架构.md) | 项目总览与架构设计 |
| [调用链路](docs/01-call-chain.md) | 消息处理主流程 |
| [系统提示词](docs/02-system-prompt.md) | 12 层系统提示词组装 |
| [上下文压缩](docs/03-context-compression.md) | 五阶段压缩策略 |
| [记忆系统](docs/04-memory.md) | 双轨道记忆与自动复盘 |
| [工具系统](docs/05-tools.md) | 工具注册、执行、审批 |
| [Provider](docs/06-provider.md) | LLM API 封装 |
| [Agent 引擎](docs/07-agent.md) | 对话循环与子 Agent |
| [CLI 界面](docs/08-cli.md) | prompt_toolkit 终端 UI |
| [会话持久化](docs/09-session.md) | SQLite WAL + FTS5 |
| [Evolution](docs/10-evolution.md) | Nudge 复盘 + Curator |

## 环境

Python ≥ 3.11 · macOS / Linux / Windows


## 演示

![demo-1](asset/1.png)

![demo-2](asset/2.png)

![demo-3](asset/3.png)

![demo-4](asset/4.png)

## License

MIT
"""首次启动引导 & 运行时 /setup 配置向导。"""

import getpass
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_console = Console()

# ── 路径常量 ──────────────────────────────────────────────────────────────────

MINIHERMES_HOME = Path.home() / ".minihermes"
_USER_CONFIG_PATH = MINIHERMES_HOME / "config.yaml"
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"


# ── 配置读写工具 ──────────────────────────────────────────────────────────────

def _ensure_config_dir():
    """确保 ~/.minihermes/ 目录存在。"""
    MINIHERMES_HOME.mkdir(parents=True, exist_ok=True)


def read_user_config() -> dict:
    """读取用户配置。不存在时从模板复制一份返回。"""
    if _USER_CONFIG_PATH.exists():
        with open(_USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    # 首次：读模板
    if _DEFAULT_CONFIG_PATH.exists():
        with open(_DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def write_user_config(cfg: dict) -> Path:
    """写入 ~/.minihermes/config.yaml，返回路径。"""
    _ensure_config_dir()
    with open(_USER_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return _USER_CONFIG_PATH


# ── 输入辅助 ──────────────────────────────────────────────────────────────────

def _prompt(label: str, *, default: str = "", secret: bool = False) -> str:
    """交互式输入。secret=True 时隐藏字符，default 非空时回车采用默认值。"""
    suffix = f" [{default}]" if default else ""
    hint = "（回车跳过）" if not default and not secret else ""
    prompt_text = f"  {label}{suffix}{hint}: "

    try:
        if secret:
            value = getpass.getpass(prompt_text)
        else:
            value = input(prompt_text)
    except (EOFError, KeyboardInterrupt):
        raise KeyboardInterrupt

    return value.strip() or default


def _mask_secret(value: str) -> str:
    """将 secret 值渲染为 **** 格式（保留后 4 位）。"""
    if not value:
        return "(not set)"
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def _normalize_bool(value: str) -> bool | None:
    """标准化布尔输入。无法识别时返回 None。"""
    v = value.strip().lower()
    if v in ("true", "yes", "y", "1"):
        return True
    if v in ("false", "no", "n", "0"):
        return False
    return None


def _normalize_int(value: str) -> int | None:
    """标准化整数输入。无法识别时返回 None。"""
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return None


# ── 首次运行向导（Rich 风格，保留原有行为） ──────────────────────────────────

def run_setup_wizard() -> bool:
    """执行首次配置引导。成功返回 True，用户中断返回 False。"""
    try:
        _console.print()
        _console.print(Panel(
            Text.from_markup(
                "[bold gold1]Welcome to MiniHermes![/]\n\n"
                "首次运行，需要配置模型连接信息。\n"
                "完成后配置将保存到 [dim]~/.minihermes/config.yaml[/]"
            ),
            border_style="gold1",
            padding=(1, 2),
        ))
        _console.print()

        # ── 1. 模型配置（必填） ──
        _console.print("  [bold]◆ 模型配置[/]（必填）\n")

        base_url = ""
        while not base_url:
            base_url = _prompt("API Base URL (OpenAI 兼容)")
            if not base_url:
                _console.print("    [red]Base URL 不能为空[/]")

        api_key = ""
        while not api_key:
            api_key = _prompt("API Key", secret=True)
            if not api_key:
                _console.print("    [red]API Key 不能为空[/]")

        _console.print()

        # ── 2. 搜索配置（可选） ──
        _console.print("  [bold]◆ 搜索配置[/]（可选，回车跳过）\n")
        _console.print("    Exa AI 搜索引擎 — 专为 AI Agent 设计")
        _console.print("    免费注册获取 API Key：https://dashboard.exa.ai")
        _console.print()
        search_api_key = _prompt("Exa API Key", secret=True)
        _console.print()

        # ── 3. 代码沙箱配置（可选） ──
        _console.print("  [bold]◆ 代码沙箱配置[/]（可选，回车跳过）\n")
        code_api_key = _prompt("七牛 E2B 沙箱 API Key", secret=True)
        _console.print()

        # ── 写入配置 ──
        cfg = read_user_config()

        cfg["model"]["base_url"] = base_url
        cfg["model"]["api_key"] = api_key

        if search_api_key:
            cfg["search"]["api_key"] = search_api_key

        if code_api_key:
            cfg["code_execution"]["api_key"] = code_api_key

        config_path = write_user_config(cfg)

        _console.print(Panel(
            Text.from_markup(
                "[bold green]✓ 配置完成！[/]\n\n"
                f"配置文件: [dim]{config_path}[/]\n"
                "随时可以手动编辑该文件修改配置。"
            ),
            border_style="green",
            padding=(1, 2),
        ))
        _console.print()
        return True

    except KeyboardInterrupt:
        _console.print("\n\n  [dim]已取消配置，退出。[/]\n")
        return False


# ── CLI /setup 向导（Rich 美化，覆盖全部字段） ─────────────────────────────────

def run_setup_cli(app_loop=None) -> bool:
    """运行时配置引导（/setup 命令调用）。

    在 prompt_toolkit 运行期间，通过 run_in_terminal 临时恢复终端 cooked 模式
    来安全地进行交互式输入。app_loop 是 prompt_toolkit 的主事件循环。
    如果 app_loop 为 None（首次运行向导场景），则直接运行。
    """
    if app_loop is None:
        # 非 prompt_toolkit 上下文（如首次运行向导），直接执行
        return _run_setup_cli_core()

    import asyncio
    import threading
    from prompt_toolkit.application.run_in_terminal import run_in_terminal

    result_holder: list[bool] = []
    done_event = threading.Event()

    def _run():
        """在 cooked 终端中运行的实际设置逻辑。"""
        try:
            result_holder.append(_run_setup_cli_core())
        except Exception:
            result_holder.append(False)
        finally:
            done_event.set()

    async def _bridge():
        await run_in_terminal(_run)

    # 在主事件循环中调度运行
    asyncio.run_coroutine_threadsafe(_bridge(), app_loop)
    # 阻塞直到设置完成
    done_event.wait()

    return result_holder[0] if result_holder else False


def _run_setup_cli_core() -> bool:
    """设置引导的核心逻辑（在 cooked 终端中运行）。

    所有输出直接写到 sys.__stdout__，绕过 prompt_toolkit 的 patch_stdout，
    确保在 run_in_terminal 期间渲染干净无冲突。
    使用 ANSI 转义码做颜色和样式，不依赖任何第三方库。
    """
    _out = sys.__stdout__

    # ── ANSI 颜色 / 样式 ──────────────────────────────────────────────────
    B = "\033[1m"       # bold
    D = "\033[2m"       # dim
    _R = "\033[0m"      # reset
    # 前景色
    C = "\033[36m"      # cyan
    G = "\033[32m"      # green
    Y = "\033[33m"      # yellow
    R = "\033[31m"      # red
    W = "\033[37m"      # white
    # 粗体 + 颜色
    BC = "\033[1;36m"   # bold cyan
    BG = "\033[1;32m"   # bold green
    BY = "\033[1;33m"   # bold yellow
    BR = "\033[1;31m"   # bold red
    BW = "\033[1;37m"   # bold white

    WIDTH = 58

    # ── 框线辅助（先拼纯文本 + len() 算宽度，最后 ANSI 上色，参考 Hermes 做法）──

    def _box_top(color: str, inner: int) -> None:
        print(f"  {color}╔{'═' * inner}╗{_R}", file=_out)

    def _box_mid(color: str, inner: int) -> None:
        print(f"  {color}╠{'═' * inner}╣{_R}", file=_out)

    def _box_bot(color: str, inner: int) -> None:
        print(f"  {color}╚{'═' * inner}╝{_R}", file=_out)

    def _box_line(color: str, inner: int, left: str, mid: str,
                  mid_color: str = "") -> None:
        """单行框：border(color) + 内容(可选mid_color) + 右border(color)。
        内容区宽度 = inner，由 left(纯文本) + mid(纯文本) + 空格填充 组成。"""
        plain = left + mid
        pad = inner - len(plain)
        if mid_color:
            print(f"  {color}║{_R}{left}{mid_color}{mid}{_R}{' ' * pad}{color}║{_R}", file=_out)
        else:
            print(f"  {color}║{_R}{left}{mid}{' ' * pad}{color}║{_R}", file=_out)

    def _header(title: str):
        """主标题框。"""
        inner = WIDTH - 2
        print(file=_out)
        _box_top(C, inner)
        # 居中标题
        pad_total = inner - len(title)
        pad_l = pad_total // 2
        _box_line(C, inner, ' ' * pad_l, title, mid_color=BW)
        _box_mid(C, inner)
        _box_line(C, inner, ' ', "Enter = keep current   Ctrl+C = cancel",
                  mid_color=D)
        _box_bot(C, inner)
        print(file=_out)

    def _section(num: int, title: str):
        """编号分节标题。"""
        print(f"  {BY}◆ {num}. {title}{_R}", file=_out)
        print(f"  {D}{'─' * WIDTH}{_R}", file=_out)

    def _field(label: str, default: str = "", required: bool = False):
        """格式化字段标签行，显示默认值。"""
        req = f" {BR}*{_R}" if required else ""
        def_str = f" {D}[{default}]{_R}" if default else ""
        print(f"    {B}{label}{_R}{req}{def_str}", file=_out)

    def _info(msg: str):
        """提示。"""
        print(f"    {D}{msg}{_R}", file=_out)

    def _hint(msg: str):
        """选项提示。"""
        print(f"    {D}└ {msg}{_R}", file=_out)

    def _error(msg: str):
        """错误。"""
        print(f"    {R}✗ {msg}{_R}", file=_out)

    def _done(msg: str):
        """成功标记。"""
        print(f"  {G}✓{_R} {msg}", file=_out)

    def _summary_header():
        """汇总表头。"""
        print(file=_out)
        print(f"  {BG}  Summary  {_R}", file=_out)
        print(f"  {D}{'─' * WIDTH}{_R}", file=_out)

    def _summary_row(section: str, field: str, value: str):
        """汇总行。"""
        print(f"  {C}{section:<14s}{_R} {B}{field:<22s}{_R} {D}{value}{_R}", file=_out)

    def _success_box():
        """完成面板。"""
        inner = WIDTH - 2
        print(file=_out)
        print(f"  {G}╭{'─' * inner}╮{_R}", file=_out)
        _box_line(G, inner, ' ', "✓  Config saved", mid_color=BG)
        _box_line(G, inner, ' ', "Restart MiniHermes for changes to take effect.",
                  mid_color=D)
        print(f"  {G}╰{'─' * inner}╯{_R}", file=_out)
        print(file=_out)

    def _cancel_box():
        """取消面板。"""
        inner = WIDTH - 2
        print(file=_out)
        print(f"  {D}╭{'─' * inner}╮{_R}", file=_out)
        _box_line(D, inner, ' ', "Setup cancelled. No changes saved.",
                  mid_color=D)
        print(f"  {D}╰{'─' * inner}╯{_R}", file=_out)
        print(file=_out)

    try:
        cfg = read_user_config()
        model_cfg: dict[str, Any] = cfg.get("model", {})
        search_cfg: dict[str, Any] = cfg.get("search", {})
        code_cfg: dict[str, Any] = cfg.get("code_execution", {})
        evo_cfg: dict[str, Any] = cfg.get("evolution", {})


        # ── 标题 ──────────────────────────────────────────────────────────
        _header("MiniHermes Setup")

        # ═══════════════════ 1. Model Connection ═══════════════════════════
        _section(1, "Model Connection")

        _field("Model Name", model_cfg.get("name", "deepseek-v4-pro"))
        model_name = _prompt("  ▸ Model Name",
                             default=model_cfg.get("name", "deepseek-v4-pro"))
        model_cfg["name"] = model_name

        # Base URL
        current_base = model_cfg.get("base_url", "")
        _field("API Base URL", current_base, required=not bool(current_base))
        while True:
            base_url = _prompt("  ▸ API Base URL", default=current_base)
            if base_url.strip():
                model_cfg["base_url"] = base_url.strip()
                break
            if current_base:
                break
            _error("Base URL cannot be empty")

        # API Key
        current_key = model_cfg.get("api_key", "")
        _field("API Key", _mask_secret(current_key), required=not bool(current_key))
        while True:
            api_key = _prompt("  ▸ API Key",
                              default=_mask_secret(current_key) if current_key else "",
                              secret=True)
            if api_key and api_key != _mask_secret(current_key):
                model_cfg["api_key"] = api_key
                break
            if current_key:
                break
            _error("API Key cannot be empty")

        # ═══════════════════ 2. Model Parameters ═══════════════════════════
        print(file=_out)
        _section(2, "Model Parameters")

        _field("Max Iterations", str(model_cfg.get("max_iterations", 100)))
        max_iter = _prompt("  ▸ Max Iterations",
                           default=str(model_cfg.get("max_iterations", 100)))
        if (n := _normalize_int(max_iter)) is not None and n > 0:
            model_cfg["max_iterations"] = n
        elif n is not None:
            _error("Must be a positive integer, keeping current value")

        current_thinking = model_cfg.get("show_thinking", True)
        _field("Show Thinking", "yes" if current_thinking else "no")
        thinking = _prompt("  ▸ Show Thinking",
                           default="yes" if current_thinking else "no")
        if (b := _normalize_bool(thinking)) is not None:
            model_cfg["show_thinking"] = b

        current_reason = model_cfg.get("reason", True)
        _field("Reason", "yes" if current_reason else "no")
        _hint("Enable thinking/reasoning mode")
        reason = _prompt("  ▸ Reason",
                         default="yes" if current_reason else "no")
        if (b := _normalize_bool(reason)) is not None:
            model_cfg["reason"] = b

        # ═══════════════════ 3. Search ═════════════════════════════════════
        print(file=_out)
        _section(3, "Search — Exa AI")

        _info("AI-native search engine for agents")
        _info("Free API key → https://dashboard.exa.ai")
        print(file=_out)

        current_search_key = search_cfg.get("api_key", "")
        _field("Exa API Key", _mask_secret(current_search_key))
        search_key = _prompt("  ▸ Exa API Key",
                             default=_mask_secret(current_search_key) if current_search_key else "",
                             secret=True)
        if search_key and search_key != _mask_secret(current_search_key):
            search_cfg["api_key"] = search_key

        _field("Result Count", str(search_cfg.get("count", 5)))
        search_count = _prompt("  ▸ Result Count",
                               default=str(search_cfg.get("count", 5)))
        if (n := _normalize_int(search_count)) is not None and n > 0:
            search_cfg["count"] = n

        # ═══════════════════ 4. Code Execution ═════════════════════════════
        print(file=_out)
        _section(4, "Code Execution — E2B Sandbox")

        current_code_key = code_cfg.get("api_key", "")
        _field("E2B Sandbox API Key", _mask_secret(current_code_key))
        code_key = _prompt("  ▸ E2B Sandbox API Key",
                           default=_mask_secret(current_code_key) if current_code_key else "",
                           secret=True)
        if code_key and code_key != _mask_secret(current_code_key):
            code_cfg["api_key"] = code_key

        # ═══════════════════ 5. System ═════════════════════════════════════
        print(file=_out)
        _section(5, "System")

        evo_enabled = evo_cfg.get("enabled", True)
        _field("Evolution System", "yes" if evo_enabled else "no")
        evo = _prompt("  ▸ Evolution System",
                      default="yes" if evo_enabled else "no")
        if (b := _normalize_bool(evo)) is not None:
            evo_cfg["enabled"] = b
        cfg["evolution"] = evo_cfg

        # ── 写回 ──────────────────────────────────────────────────────────
        cfg["model"] = model_cfg
        cfg["search"] = search_cfg
        cfg["code_execution"] = code_cfg
        cfg["evolution"] = evo_cfg

        config_path = write_user_config(cfg)

        # ── 汇总 ───────────────────────────────────────────────────────────
        _summary_header()
        _summary_row("Model", "Name", model_cfg.get("name", ""))
        _summary_row("Model", "Base URL", model_cfg.get("base_url", ""))
        _summary_row("Model", "API Key", _mask_secret(model_cfg.get("api_key", "")))
        _summary_row("Model", "Max Iterations", str(model_cfg.get("max_iterations", "")))
        _summary_row("Model", "Show Thinking", "yes" if model_cfg.get("show_thinking") else "no")
        _summary_row("Model", "Reason", "yes" if model_cfg.get("reason", True) else "no")
        _summary_row("Search", "API Key", _mask_secret(search_cfg.get("api_key", "")))
        _summary_row("Search", "Count", str(search_cfg.get("count", "")))
        _summary_row("Code Exec", "API Key", _mask_secret(code_cfg.get("api_key", "")))
        _summary_row("Evolution", "Enabled", "yes" if evo_cfg.get("enabled") else "no")
        print(f"  {D}{'─' * WIDTH}{_R}", file=_out)
        _done(f"Saved to {config_path}")
        _success_box()
        return True

    except KeyboardInterrupt:
        _cancel_box()
        return False

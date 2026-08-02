"""
配置加载层。

职责单一：读取 ~/.minihermes/config.yaml（首次启动则触发 setup wizard），
并把项目模板里新增的顶层 key 补齐到用户配置中。

模块/算法级别的常量（如 MODEL_NAME、CONTEXT_WINDOW、RETRY 阈值等）已下沉到
各自使用方所在的模块，不再集中放在 config 包内。
"""

import sys
import yaml
from pathlib import Path

MINIHERMES_HOME = Path.home() / ".minihermes"

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_CONFIG_PATH = MINIHERMES_HOME / "config.yaml"


def _ensure_config():
    """确保 ~/.minihermes/config.yaml 存在，不存在则启动引导。"""
    if _CONFIG_PATH.exists():
        return
    from config.setup_wizard import run_setup_wizard
    if not run_setup_wizard():
        sys.exit(0)


def load() -> dict:
    _ensure_config()
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}

    mutated = False
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            default_cfg = yaml.safe_load(f) or {}
        for key, value in default_cfg.items():
            if key not in user_cfg:
                user_cfg[key] = value
                mutated = True

    if mutated:
        try:
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.safe_dump(user_cfg, f, allow_unicode=True, sort_keys=False)
        except OSError:
            pass

    return user_cfg


_cfg = load()


class Config:
    """可注入的配置容器。

    延迟加载 ~/.minihermes/config.yaml，
    首次访问 property 时读取并合并默认值。
    支持通过 config_path 指定自定义路径（测试用）。
    """

    def __init__(self, config_path: Path | None = None):
        """可指定自定义配置路径，默认 ~/.minihermes/config.yaml。"""
        self._config_path = config_path or _CONFIG_PATH
        self._data: dict | None = None

    def _ensure_loaded(self):
        """延迟加载：首次访问时从磁盘读取并合并默认值。"""
        if self._data is not None:
            return
        self._data = load()

    @property
    def model(self) -> dict:
        """模型相关配置（name, base_url, api_key, max_iterations 等）。"""
        self._ensure_loaded()
        return self._data.get("model", {})

    @property
    def search(self) -> dict:
        """搜索相关配置。"""
        self._ensure_loaded()
        return self._data.get("search", {})

    @property
    def code_execution(self) -> dict:
        """代码执行沙箱配置。"""
        self._ensure_loaded()
        return self._data.get("code_execution", {})

    @property
    def evolution(self) -> dict:
        """进化系统配置。"""
        self._ensure_loaded()
        return self._data.get("evolution", {})

    def reload(self):
        """强制从磁盘重新加载（运行时配置变更后调用）。"""
        self._data = None


# 向后兼容：模块级默认实例和访问器函数
_default_config = Config()


def get_model_config() -> dict:
    return _default_config.model


def get_search_config() -> dict:
    return _default_config.search


def get_code_execution_config() -> dict:
    return _default_config.code_execution


def get_evolution_config() -> dict:
    return _default_config.evolution



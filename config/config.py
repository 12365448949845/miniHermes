"""
配置加载层。

职责单一：读取 ~/.minihermes/config.yaml（首次启动则触发 setup wizard），
并把项目模板里新增的顶层 key 补齐到用户配置中。

模块/算法级别的常量（如 MODEL_NAME、CONTEXT_WINDOW、RETRY 阈值等）已下沉到
各自使用方所在的模块，不再集中放在 config 包内。
"""

import sys
import copy
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


def _fill_missing(default, user):
    """递归补齐缺失项；用户标量、列表和已有字典值优先。"""
    if not isinstance(default, dict) or not isinstance(user, dict):
        return copy.deepcopy(user)
    merged = copy.deepcopy(user)
    for key, value in default.items():
        if key not in merged:
            merged[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            merged[key] = _fill_missing(value, merged[key])
    return merged


def _load_from_path(path: Path, *, persist_missing: bool = False) -> dict:
    if path == _CONFIG_PATH:
        _ensure_config()
    with open(path, "r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    if not isinstance(user_cfg, dict):
        user_cfg = {}

    default_cfg = {}
    if DEFAULT_CONFIG_PATH.exists():
        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            default_cfg = yaml.safe_load(f) or {}
    merged = _fill_missing(default_cfg, user_cfg)
    if persist_missing and merged != user_cfg:
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(merged, f, allow_unicode=True, sort_keys=False)
        except OSError:
            pass
    return merged


def load() -> dict:
    return _load_from_path(_CONFIG_PATH, persist_missing=True)


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
        self._data = _load_from_path(
            self._config_path,
            persist_missing=self._config_path == _CONFIG_PATH,
        )

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
        """Code execution sandbox settings."""
        self._ensure_loaded()
        return self._data.get("code_execution", {})

    @property
    def image_generation(self) -> dict:
        """Pollinations image-generation settings."""
        self._ensure_loaded()
        return self._data.get("image_generation", {})

    @property
    def evolution(self) -> dict:
        """进化系统配置。"""
        self._ensure_loaded()
        return self._data.get("evolution", {})

    @property
    def agent_runtime(self) -> dict:
        """Agent Runtime 的取消宽限期和各类 Run deadline。"""
        self._ensure_loaded()
        return _normalize_agent_runtime(self._data.get("agent_runtime", {}))

    @property
    def reproducibility(self) -> dict:
        """本地执行证据和制品保留配置。"""
        self._ensure_loaded()
        return _normalize_reproducibility(self._data.get("reproducibility", {}))

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


def get_image_generation_config() -> dict:
    return _default_config.image_generation


def get_evolution_config() -> dict:
    return _default_config.evolution


def _normalize_agent_runtime(raw: dict | None) -> dict:
    defaults = {
        "cancel_grace_seconds": 3.0,
        # Delegate 并发必须显式开启；1 保持现有的严格串行行为。
        "max_concurrency": 1,
        "delegate_batch_timeout_seconds": 300.0,
        "worktree": {
            "enabled": False,
            # W3 只允许显式从默认 1 放开到 2。
            "max_write_concurrency": 1,
            "runner": "docker",
            "docker_image": "",
            "docker_user": "65532:65532",
            "pids_limit": 256,
            "memory_limit": "1g",
            "integration_verification_command": "",
            "preserve_failed_days": 30,
        },
        "run_timeout_seconds": {
            "main_turn": None,
            "delegate": 300.0,
            "plan": 600.0,
            "replay": 300.0,
            "worktree_integration": 300.0,
            "memory_nudge": 120.0,
            "skill_nudge": 180.0,
            "curator": 300.0,
        },
    }
    value = _fill_missing(defaults, raw if isinstance(raw, dict) else {})
    try:
        grace = float(value.get("cancel_grace_seconds", 3.0))
        value["cancel_grace_seconds"] = min(max(grace, 0.1), 60.0)
    except (TypeError, ValueError):
        value["cancel_grace_seconds"] = 3.0
    try:
        value["max_concurrency"] = min(
            max(int(value.get("max_concurrency", 1)), 1), 16
        )
    except (TypeError, ValueError):
        value["max_concurrency"] = 1
    try:
        timeout = float(value.get("delegate_batch_timeout_seconds", 300.0))
        value["delegate_batch_timeout_seconds"] = min(max(timeout, 1.0), 3600.0)
    except (TypeError, ValueError):
        value["delegate_batch_timeout_seconds"] = 300.0
    worktree = value.get("worktree")
    if not isinstance(worktree, dict):
        worktree = copy.deepcopy(defaults["worktree"])
        value["worktree"] = worktree
    worktree["enabled"] = bool(worktree.get("enabled", False))
    try:
        worktree["max_write_concurrency"] = min(
            max(int(worktree.get("max_write_concurrency", 1)), 1), 2
        )
    except (TypeError, ValueError):
        worktree["max_write_concurrency"] = 1
    worktree["runner"] = (
        worktree.get("runner", "docker").strip().lower()
        if isinstance(worktree.get("runner", "docker"), str)
        else "docker"
    )
    for key in ("docker_image", "docker_user", "memory_limit", "integration_verification_command"):
        raw_value = worktree.get(key, defaults["worktree"][key])
        worktree[key] = raw_value.strip() if isinstance(raw_value, str) else defaults["worktree"][key]
    try:
        worktree["pids_limit"] = min(
            max(int(worktree.get("pids_limit", 256)), 16), 4096
        )
    except (TypeError, ValueError):
        worktree["pids_limit"] = 256
    try:
        worktree["preserve_failed_days"] = min(
            max(int(worktree.get("preserve_failed_days", 30)), 1), 3650
        )
    except (TypeError, ValueError):
        worktree["preserve_failed_days"] = 30
    for kind, timeout in value.get("run_timeout_seconds", {}).items():
        if timeout is None:
            continue
        try:
            timeout = float(timeout)
            value["run_timeout_seconds"][kind] = timeout if timeout > 0 else None
        except (TypeError, ValueError):
            value["run_timeout_seconds"][kind] = defaults["run_timeout_seconds"].get(kind)
    return value


def _normalize_reproducibility(raw: dict | None) -> dict:
    defaults = {
        "enabled": True,
        "artifact_root": "",
        "max_log_bytes_per_stream": 20 * 1024 * 1024,
        "max_snapshot_bytes": 200 * 1024 * 1024,
        "retention_days": 30,
        "max_total_artifact_bytes": 1024 * 1024 * 1024,
        "keep_failed_days": 30,
    }
    value = _fill_missing(defaults, raw if isinstance(raw, dict) else {})
    value["enabled"] = bool(value.get("enabled", True))
    artifact_root = value.get("artifact_root", "")
    value["artifact_root"] = artifact_root.strip() if isinstance(artifact_root, str) else ""
    for key, default, minimum, maximum in (
        ("max_log_bytes_per_stream", defaults["max_log_bytes_per_stream"], 1024, 1024 * 1024 * 1024),
        ("max_snapshot_bytes", defaults["max_snapshot_bytes"], 1024, 10 * 1024 * 1024 * 1024),
        ("max_total_artifact_bytes", defaults["max_total_artifact_bytes"], 1024, 100 * 1024 * 1024 * 1024),
    ):
        try:
            value[key] = min(max(int(value.get(key, default)), minimum), maximum)
        except (TypeError, ValueError):
            value[key] = default
    for key, default in (("retention_days", 30), ("keep_failed_days", 30)):
        try:
            value[key] = min(max(int(value.get(key, default)), 1), 3650)
        except (TypeError, ValueError):
            value[key] = default
    return value


def get_agent_runtime_config() -> dict:
    return _default_config.agent_runtime


def get_reproducibility_config() -> dict:
    return _default_config.reproducibility

"""Phase 4：配置递归合并与注入路径测试。"""

import yaml

from config.config import Config


def test_injected_config_path_recursively_fills_defaults_without_overwriting(tmp_path):
    path = tmp_path / "config.yaml"
    original = {
        "model": {
            "name": "custom-model",
            "api_key": "sk-user-secret-must-stay",
        },
        "search": {"count": 9},
        "agent_runtime": {
            "cancel_grace_seconds": 7,
            "run_timeout_seconds": {"delegate": 42},
        },
        "custom": {"items": ["keep", "this"]},
    }
    path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")

    config = Config(config_path=path)

    assert config.model["name"] == "custom-model"
    assert config.model["api_key"] == "sk-user-secret-must-stay"
    assert config.search["count"] == 9
    assert config.image_generation["timeout_seconds"] == 120
    assert config.image_generation["auto_open"] is True
    assert config.agent_runtime["cancel_grace_seconds"] == 7.0
    assert config.agent_runtime["run_timeout_seconds"]["delegate"] == 42.0
    assert config.agent_runtime["run_timeout_seconds"]["plan"] == 600.0
    assert config._data["custom"]["items"] == ["keep", "this"]

    # 指定路径用于测试/注入时只在内存中补默认值，不能擅自改写文件。
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == original

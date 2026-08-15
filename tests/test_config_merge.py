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
        "reproducibility": {
            "enabled": False,
            "artifact_root": "C:/user-artifacts",
            "retention_days": 14,
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
    assert config.agent_runtime["run_timeout_seconds"]["worktree_integration"] == 300.0
    assert config.agent_runtime["worktree"] == {
        "enabled": False,
        "max_write_concurrency": 1,
        "runner": "docker",
        "docker_image": "",
        "docker_user": "65532:65532",
        "pids_limit": 256,
        "memory_limit": "1g",
        "integration_verification_command": "",
        "preserve_failed_days": 30,
    }
    assert config.reproducibility["enabled"] is False
    assert config.reproducibility["artifact_root"] == "C:/user-artifacts"
    assert config.reproducibility["retention_days"] == 14
    assert config.reproducibility["max_log_bytes_per_stream"] == 20 * 1024 * 1024
    assert config._data["custom"]["items"] == ["keep", "this"]

    # 指定路径用于测试/注入时只在内存中补默认值，不能擅自改写文件。
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == original


def test_worktree_write_concurrency_is_clamped_to_w3_limit(tmp_path):
    path = tmp_path / "config.yaml"

    path.write_text(
        yaml.safe_dump({"agent_runtime": {"worktree": {"max_write_concurrency": 9}}}),
        encoding="utf-8",
    )
    assert Config(config_path=path).agent_runtime["worktree"]["max_write_concurrency"] == 2

    path.write_text(
        yaml.safe_dump({"agent_runtime": {"worktree": {"max_write_concurrency": 0}}}),
        encoding="utf-8",
    )
    assert Config(config_path=path).agent_runtime["worktree"]["max_write_concurrency"] == 1

    path.write_text(
        yaml.safe_dump({"agent_runtime": {"worktree": {"max_write_concurrency": "bad"}}}),
        encoding="utf-8",
    )
    assert Config(config_path=path).agent_runtime["worktree"]["max_write_concurrency"] == 1

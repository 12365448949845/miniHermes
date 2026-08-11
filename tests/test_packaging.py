"""打包和可编辑安装入口的回归测试。"""

import importlib
import tomllib
from pathlib import Path

import minihermes_cli


def test_wheel_configuration_does_not_require_generated_app_directory():
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]

    assert "agent" in wheel["packages"]
    assert "minihermes_cli/app" not in wheel.get("force-include", {})
    assert "main.py" in wheel["force-include"]


def test_cli_entrypoint_loads_the_top_level_main_module(monkeypatch):
    application = importlib.import_module("main")
    invoked = []
    monkeypatch.setattr(application, "main", lambda: invoked.append(True))

    minihermes_cli.main()

    assert invoked == [True]

"""minihermes CLI — 全局命令入口。"""

import sys
from pathlib import Path


def main():
    """pip install 后的全局命令入口。

    将打包的 app/ 目录加入 sys.path，然后路由子命令。
    """
    app_dir = str(Path(__file__).resolve().parent / "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    from main import main as _main
    _main()


if __name__ == "__main__":
    main()

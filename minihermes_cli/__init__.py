"""minihermes CLI 全局命令入口。"""


def main():
    """可编辑安装和 wheel 安装共用同一份顶层源码。"""
    from main import main as _main
    _main()


if __name__ == "__main__":
    main()

#!/bin/bash
# 构建 minihermes wheel 包
# 使用方法: cd miniHermes && bash build_wheel.sh
# 产物: dist/minihermes-*.whl

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== 清理旧构建 ==="
rm -rf build/ dist/ *.egg-info minihermes_cli/app/

echo "=== 复制源码到 minihermes_cli/app/ ==="
mkdir -p minihermes_cli/app

# 复制所有源码模块（仅运行时需要的包和文件）
for item in agent approval cli config context evolution main.py \
            provider prompt renderer session skills tools _builtin_skills; do
    cp -r "$item" minihermes_cli/app/
done

# 未包含 __init__.py 的子目录补上空文件（防止缺少包的导入错误）
find minihermes_cli/app -type d ! -exec test -e "{}/__init__.py" \; -exec touch "{}/__init__.py" \;

echo "=== 构建 wheel ==="
if command -v uv &>/dev/null; then
    uv build --wheel -o dist/ 2>&1 | tail -5
else
    pip wheel . --no-deps -w dist/ 2>&1 | tail -5
fi

echo ""
echo "=== 清理临时构建文件 ==="
rm -rf build/ *.egg-info minihermes_cli/app/

echo ""
echo "=== 构建完成 ==="
ls -lh dist/*.whl 2>/dev/null
echo ""
echo "分发方式："
echo "  将 dist/ 下的 .whl 文件发给他人"
echo ""
echo "安装方式："
echo "  pip install dist/minihermes-*.whl"
echo "  # 或"
echo "  uv tool install dist/minihermes-*.whl"
echo ""
echo "使用方式："
echo "  minihermes    # 在任意目录的终端中输入"

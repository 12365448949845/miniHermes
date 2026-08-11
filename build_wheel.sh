#!/bin/bash
# 构建 minihermes wheel 包
# 使用方法: cd miniHermes && bash build_wheel.sh
# 产物: dist/minihermes-*.whl

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 在受限环境和 CI 中避免依赖用户目录的 uv 缓存权限。
export UV_CACHE_DIR="${UV_CACHE_DIR:-$SCRIPT_DIR/.uv-cache}"

echo "=== 清理旧构建 ==="
rm -rf build/ dist/ *.egg-info

echo "=== 构建 wheel ==="
if command -v uv &>/dev/null; then
    uv build --wheel -o dist/ 2>&1 | tail -5
else
    pip wheel . --no-deps -w dist/ 2>&1 | tail -5
fi

echo ""
echo "=== 清理临时构建文件 ==="
rm -rf build/ *.egg-info

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

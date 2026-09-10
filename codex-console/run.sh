#!/usr/bin/env bash
# codex-console 启动脚本。只绑 127.0.0.1，免登录。
set -euo pipefail
cd "$(dirname "$0")"

PORT="${CODEX_CONSOLE_PORT:-8420}"
ROUTER="${CODEX_ROUTER_URL:-http://127.0.0.1:8317}"

# 依赖检查：只需 tomlkit
if ! python3 -c "import tomlkit" 2>/dev/null; then
  echo "缺少 tomlkit，正在安装…"
  (uv pip install --system tomlkit 2>/dev/null || pip3 install --user tomlkit)
fi

echo "codex-console → http://127.0.0.1:${PORT}"
echo "后端中转      → ${ROUTER}"
echo "（Ctrl+C 停止）"
CODEX_CONSOLE_PORT="${PORT}" CODEX_ROUTER_URL="${ROUTER}" exec python3 server.py

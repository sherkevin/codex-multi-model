#!/usr/bin/env bash
# 把 codex-console 装成 launchd 常驻服务（开机自起、崩溃自拉、带密钥环境）。
# 只创建/加载当前用户的 LaunchAgent，不改任何系统级配置。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.codex-console"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="${CODEX_CONSOLE_PORT:-8420}"
PYTHON="$(command -v python3)"
LOG="/tmp/codex-console.log"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-c</string>
    <string>source ~/.zshrc &gt;/dev/null 2&gt;&amp;1; cd "$PROJECT_DIR"; exec "$PYTHON" server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>CODEX_CONSOLE_PORT</key><string>$PORT</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "✓ 已安装并启动 → http://127.0.0.1:$PORT"
echo "  日志：$LOG"
echo "  卸载：launchctl bootout gui/$(id -u)/$LABEL && rm '$PLIST'"

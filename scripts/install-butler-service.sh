#!/usr/bin/env bash
# 把管家 Server 装成 systemd 用户服务并启动。可重复执行；服务从本仓库所在位置运行。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${HOME}/.config/systemd/user"

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "linger 没开：开机不会自启，退出登录后服务也会停。先执行一次：" >&2
  echo "  sudo loginctl enable-linger $USER" >&2
  exit 1
fi

mkdir -p "$UNIT_DIR" "${HOME}/.mihome"
sed "s|@ROOT@|$ROOT|g" "$ROOT/scripts/butler.service" > "$UNIT_DIR/butler.service"
systemctl --user daemon-reload
systemctl --user enable butler.service
systemctl --user restart butler.service
sleep 2
systemctl --user --no-pager status butler.service | head -5

#!/usr/bin/env bash
# NAS 上前台跑管家 Server：WS :4399 给音箱，配置页 :8787。
set -euo pipefail
NODE_BIN="$(ls -d "${HOME}"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -1 || true)"
export PATH="${HOME}/.local/bin:${NODE_BIN:+${NODE_BIN}:}${PATH:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/butler"
[ -d node_modules ] || npm ci --omit=dev
exec node --import tsx src/index.ts

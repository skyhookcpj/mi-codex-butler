#!/usr/bin/env bash
# 装扫地机 skill 的依赖：独立的 Python 环境 + Tasshack/dreame-vacuum 的协议包（固定版本）。可重复执行。
#
#   bash .codex/skills/vacuum/install.sh
#
# 国内访问 PyPI 慢时临时换源（只影响这一次安装）：
#   UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash .codex/skills/vacuum/install.sh
#
# 装到 ~/.mihome（或 $MIHOME_HOME）下：
#   dreame-venv/      只给扫地机用的 Python 环境，不碰 mihome 的环境
#   vendor/dreame/    协议包，来自 https://github.com/Tasshack/dreame-vacuum（MIT）
set -euo pipefail

HOME_DIR="${MIHOME_HOME:-$HOME/.mihome}"
VENV="$HOME_DIR/dreame-venv"
VENDOR="$HOME_DIR/vendor"
REF="9857362d37fa6a1788a0ce2eb6e1290b3ec7d6fb" # v2.0.1

command -v uv >/dev/null || { echo "需要 uv：https://docs.astral.sh/uv/" >&2; exit 1; }

echo "== Python 环境 $VENV"
[ -x "$VENV/bin/python" ] || uv venv -q --python python3 "$VENV"
VIRTUAL_ENV="$VENV" uv pip install -q \
  requests paho-mqtt pycryptodome cryptography numpy Pillow mini-racer
# python-miio 依赖的 netifaces 没有新版 Python 的预编译包，要 gcc 现场编译；换成接口相同、自带预编译包的 netifaces2
VIRTUAL_ENV="$VENV" uv pip install -q --no-deps "python-miio==0.5.12"
VIRTUAL_ENV="$VENV" uv pip install -q \
  "PyYAML>=5,<7" "android_backup<1" "appdirs>=1,<2" attrs "click>=8" "construct>=2.10.56,<3" \
  "croniter>=1" "defusedxml<1" micloud pytz "tqdm>=4,<5" "zeroconf<1" netifaces2

echo "== 协议包 Tasshack/dreame-vacuum@${REF:0:7}"
if [ "$(cat "$VENDOR/dreame/.ref" 2>/dev/null)" != "$REF" ]; then
  # 访问 GitHub 慢时，可以在别的机器上 git clone 好（检出 $REF），拷过来用 DREAME_SRC=<目录> 指定
  if [ -n "${DREAME_SRC:-}" ]; then
    src="$DREAME_SRC"
  else
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fsSL --max-time 300 "https://codeload.github.com/Tasshack/dreame-vacuum/tar.gz/$REF" | tar -xz -C "$tmp"
    src="$tmp/dreame-vacuum-$REF"
  fi
  mkdir -p "$VENDOR"
  rm -rf "$VENDOR/dreame"
  cp -r "$src/custom_components/dreame_vacuum/dreame" "$VENDOR/dreame"
  cp "$src/LICENSE" "$VENDOR/dreame/LICENSE"
  # 状态、故障码的中文名
  cp "$src/custom_components/dreame_vacuum/translations/zh-Hans.json" "$VENDOR/dreame/zh-Hans.json"
  find "$VENDOR/dreame" -name __pycache__ -prune -exec rm -rf {} +
  echo "$REF" > "$VENDOR/dreame/.ref"
fi

echo "== 自检"
cd /tmp # 协议包里有个 types.py，别让它在当前目录盖住标准库
PYTHONPATH="$VENDOR" "$VENV/bin/python" -c 'from dreame.protocol import DreameVacuumProtocol; import dreame; print("dreame", dreame.VERSION, "ok")'

#!/usr/bin/env bash
# 把 mihome 装成用户级 CLI：之后直接敲 mihome，不必 uv run。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not on PATH after install; add ${HOME}/.local/bin to PATH and retry" >&2
  exit 1
fi

uv tool install -e "${ROOT}" --force

BIN="${HOME}/.local/bin"
mkdir -p "${BIN}"

ensure_path_line() {
  local rc="$1"
  local line='export PATH="$HOME/.local/bin:$PATH"'
  touch "${rc}"
  if ! grep -qF '.local/bin' "${rc}"; then
    printf '\n# mihome / uv tools\n%s\n' "${line}" >> "${rc}"
  fi
}

ensure_path_line "${HOME}/.bashrc"
ensure_path_line "${HOME}/.zshrc"

echo
echo "installed: $(command -v mihome || echo "${BIN}/mihome")"
"${BIN}/mihome" --help | head -20
echo
echo "open a new shell, or: export PATH=\"\$HOME/.local/bin:\$PATH\""

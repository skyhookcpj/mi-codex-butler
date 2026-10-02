"""本地配置：`$MIHOME_HOME/config.json`，默认 `~/.mihome`。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from uuid import uuid4


def mihome_dir() -> Path:
    raw = os.environ.get("MIHOME_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".mihome"


def config_path() -> Path:
    return mihome_dir() / "config.json"


def cache_dir() -> Path:
    return mihome_dir() / "cache"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return _empty_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_config()
    if not isinstance(data, dict):
        return _empty_config()
    merged = _empty_config()
    merged.update(data)
    if not merged.get("uuid"):
        merged["uuid"] = uuid4().hex
    if not merged.get("cloud_server"):
        merged["cloud_server"] = "cn"
    return merged


def save_config(data: dict[str, Any]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cache_dir().mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure_uuid(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    data = cfg if cfg is not None else load_config()
    if not data.get("uuid"):
        data["uuid"] = uuid4().hex
        save_config(data)
    return data


def _empty_config() -> dict[str, Any]:
    return {
        "cloud_server": "cn",
        "uuid": uuid4().hex,
        "oauth": None,
        "user": None,
        "home_id": None,
    }

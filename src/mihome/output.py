"""输出：JSON 默认紧凑，--pretty 缩进。"""

from __future__ import annotations

import json
from typing import Any


def dump(data: Any, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)
    return json.dumps(data, ensure_ascii=False, default=str)


def print_result(data: Any, pretty: bool = False) -> None:
    print(dump(data, pretty))


def escape_field(value: object | None) -> str:
    if value is None:
        return ""
    return str(value).replace("|", r"\|")

"""CLI 错误与退出码：1 参数/校验，2 网络，3 云端业务。"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import sys
from collections.abc import Callable
from typing import Any, TypeVar

import aiohttp

from mihome._miot.const import MIHOME_HTTP_API_TIMEOUT
from mihome._miot.error import MIoTErrorCode, MIoTHttpError, MIoTOAuth2Error, MIoTSpecError

F = TypeVar("F", bound=Callable[..., Any])


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ValidationError(CliError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code=1)


# _miot 的 OAuth 异常会把请求体（refresh_token / 授权码）和响应原文拼进 message，输出前一律遮掉。
_TOKEN_KEYS = r"access_token|refresh_token|service_token|serviceToken|passToken|ssecurity|token|X-Client-Secret|client_secret|secret"
# 带引号的值：令牌类 + 授权码 code（数字型错误码没有引号，不会误伤）
_QUOTED_RE = re.compile(
    r"""(?P<key>["']?(?:""" + _TOKEN_KEYS + r"""|code)["']?\s*[:=]\s*["'])(?P<val>[^"'\\\s,}&]{8,})"""
)
# URL / 表单里的 key=value：只认令牌类
_PLAIN_RE = re.compile(r"(?P<key>\b(?:" + _TOKEN_KEYS + r")=)(?P<val>[^\s\"'&,}]{8,})")
_BEARER_RE = re.compile(r"(Bearer\s*)[A-Za-z0-9._~+/=-]{8,}")


def redact(message: str) -> str:
    """遮掉报错里的凭据，只留前 4 位方便对照。"""

    def mask(m: re.Match[str]) -> str:
        return f"{m.group('key')}{m.group('val')[:4]}…(已隐去)"

    message = _QUOTED_RE.sub(mask, message)
    message = _PLAIN_RE.sub(mask, message)
    return _BEARER_RE.sub(r"\1…(已隐去)", message)


def _print_error(message: str) -> None:
    print(json.dumps({"error": redact(message)}, ensure_ascii=False), file=sys.stderr)


def run_async(func: F) -> F:
    """把 async click 命令跑在 asyncio.run 里，并统一错误出口。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return asyncio.run(func(*args, **kwargs))
        except CliError as exc:
            _print_error(exc.message)
            sys.exit(exc.code)
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError) as exc:
            _print_error(format_network_error(exc))
            sys.exit(2)
        except (MIoTHttpError, MIoTOAuth2Error, MIoTSpecError) as exc:
            code = 2 if _is_auth_or_network(exc) else 3
            _print_error(str(exc) or exc.__class__.__name__)
            sys.exit(code)

    return wrapper  # type: ignore[return-value]


def format_network_error(exc: BaseException) -> str:
    """超时带秒数；空 message 时至少打出异常类型。"""
    name = type(exc).__name__
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return f"网络超时（HTTP {MIHOME_HTTP_API_TIMEOUT}s）: {name}"
    msg = str(exc).strip()
    if msg:
        return f"网络错误: {name}: {msg}"
    return f"网络错误: {name}"


def _is_auth_or_network(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code == MIoTErrorCode.CODE_HTTP_INVALID_ACCESS_TOKEN:
        return False
    return False

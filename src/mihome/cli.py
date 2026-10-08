"""mihome 入口。"""

from __future__ import annotations

import logging
import os
import sys

import click

from mihome import __version__
from mihome.commands.account import account_group
from mihome.commands.device import device_group
from mihome.commands.home import home_group
from mihome.commands.notify import notify_group
from mihome.commands.scene import scene_group
from mihome.errors import redact


@click.group()
@click.version_option(__version__, prog_name="mihome")
def cli() -> None:
    """mihome — 米家云端设备 CLI，无常驻后端。"""


cli.add_command(account_group)
cli.add_command(home_group)
cli.add_command(device_group)
cli.add_command(scene_group)
cli.add_command(notify_group)


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), ()
        return True


def _setup_logging() -> None:
    """_miot 出错时会把整个请求头（含 access token）写进 error 日志。
    没配 handler 时 Python 会把它直接打到 stderr，进而被调用方（如 Codex）读走，所以默认一律不输出；
    MIHOME_DEBUG=1 时脱敏后输出。报错信息照常由 CLI 自己脱敏后给出。"""
    root = logging.getLogger()
    if os.environ.get("MIHOME_DEBUG"):
        handler = logging.StreamHandler(sys.stderr)
        handler.addFilter(_RedactFilter())
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
    else:
        root.addHandler(logging.NullHandler())


def main() -> None:
    _setup_logging()
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    cli()

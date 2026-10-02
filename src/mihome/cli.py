"""mihome 入口。"""

from __future__ import annotations

import sys

import click

from mihome import __version__
from mihome.commands.account import account_group
from mihome.commands.device import device_group
from mihome.commands.home import home_group
from mihome.commands.notify import notify_group
from mihome.commands.scene import scene_group


@click.group()
@click.version_option(__version__, prog_name="mihome")
def cli() -> None:
    """mihome — 米家云端设备 CLI，无常驻后端。"""


cli.add_command(account_group)
cli.add_command(home_group)
cli.add_command(device_group)
cli.add_command(scene_group)
cli.add_command(notify_group)


def main() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    cli()
